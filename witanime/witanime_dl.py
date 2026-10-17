#!/usr/bin/env python3
"""
WitAnime Downloader Bot (CLI)

PC (resolve links -> push job to GitHub):
    python witanime_dl.py
        -> interactive: paste the first-episode URL, answer the quality prompt
        -> writes witanime/jobs/<slug>__<timestamp>.json and pushes it

Codespace (downloads jobs from GitHub automatically - no interaction):
    python3 witanime_dl.py --watch
        -> loop: git pull -> download new jobs -> extract flat -> build
           <Anime Title>.zip in witanime/output/ -> cleanup

Manual / legacy modes:
    python witanime_dl.py <watch-url | anime-url | slug> [options]   # direct download here
    python witanime_dl.py <url|slug> --export-links links.json       # old link export
    python witanime_dl.py --from-file links.json                     # download only
    python witanime_dl.py --from-file links.json --process           # full pipeline once
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from html import unescape as html_unescape
from pathlib import Path
from urllib.parse import unquote, urlparse

from curl_cffi import requests

BASE = "https://witanime.site"
QUALITY_ORDER = ["FHD", "HD", "SD", "4K"]
CHUNK = 262144
PROGRESS = True
WATCH_RETRY_DELAYS = [300, 900, 1800]
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv", ".ts", ".mpg", ".mpeg"}

CLOUDFLARE_MSG = """[!] Cloudflare is challenging this IP (datacenter/VPN), the site returned "Just a moment...".
    This should only be a problem on the PC side (the resolver). Fixes:
      1. Run from a normal/residential network, or
      2. Use a proxy:  --proxy http://user:pass@host:port
      3. Reuse a real browser's clearance cookie:
         python witanime_dl.py <url> --cookie "cf_clearance=PASTE" --user-agent "PASTE_UA" """


def log(msg):
    print(msg, flush=True)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def job_timestamp():
    return time.strftime("%Y%m%d-%H%M%S")


def human(n):
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024


def bar_str(pct, width=24):
    filled = int(width * pct / 100)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def sanitize(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "download"


class WitAnime:
    def __init__(self, timeout=30, proxy=None, user_agent=None, cookie=None):
        self.timeout = timeout
        self.s = requests.Session(impersonate="chrome")
        self.csrf = None
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        if user_agent:
            self.s.headers["User-Agent"] = user_agent
        if cookie:
            for part in cookie.split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    try:
                        self.s.cookies.set(k.strip(), v.strip(), domain=".witanime.site")
                    except Exception:
                        pass

    @staticmethod
    def challenge(text, status):
        head = text[:3000] if text else ""
        return (
            status in (403, 503)
            or "Just a moment" in head
            or "Attention Required" in head
        )

    def _request(self, method, url, retries=4, **kw):
        last = None
        for attempt in range(1, retries + 1):
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
            except Exception as e:
                last = e
                if attempt < retries:
                    time.sleep(2 * attempt)
                continue
            if r.status_code == 429:
                ra = (r.headers.get("Retry-After") or "").strip()
                wait = int(ra) if ra.isdigit() else min(60, 10 * 2 ** (attempt - 1))
                last = RuntimeError(f"HTTP 429 rate limited (waited {wait}s)")
                if attempt < retries:
                    time.sleep(wait)
                continue
            if r.status_code in (500, 502, 503, 504):
                last = RuntimeError(f"HTTP {r.status_code}")
                if attempt < retries:
                    time.sleep(2 * attempt)
                continue
            return r
        raise RuntimeError(f"request failed: {method} {url}: {last}")

    def get(self, url, **kw):
        return self._request("GET", url, **kw)

    def post(self, url, **kw):
        return self._request("POST", url, **kw)

    def _extract_csrf(self, html):
        m = re.search(r'name="csrf-token" content="([^"]+)"', html)
        if m:
            self.csrf = m.group(1)
        return self.csrf

    def watch_html(self, slug, ep):
        r = self.get(f"{BASE}/watch/{slug}/{ep}", headers={"Referer": BASE})
        if r.status_code == 404:
            return None
        if self.challenge(r.text, r.status_code):
            raise SystemExit(CLOUDFLARE_MSG)
        r.raise_for_status()
        self._extract_csrf(r.text)
        return r.text

    def discover_episodes(self, slug):
        for url in (f"{BASE}/anime/{slug}", f"{BASE}/watch/{slug}/1"):
            r = self.get(url, headers={"Referer": BASE})
            if self.challenge(r.text, r.status_code):
                raise SystemExit(CLOUDFLARE_MSG)
            if r.status_code != 200:
                continue
            self._extract_csrf(r.text)
            text = r.text
            eps = sorted({int(x) for x in re.findall(rf"/watch/{re.escape(slug)}/(\d+)(?:\D|$)", text)})
            if eps:
                self.anime_title = extract_anime_title(text, slug)
                return eps
        return []

    def downloads(self, slug, ep):
        url = f"{BASE}/watch/{slug}/{ep}/sources"
        r = self.post(
            url,
            headers={
                "Accept": "application/json",
                "X-CSRF-TOKEN": self.csrf or "",
                "Referer": f"{BASE}/watch/{slug}/{ep}",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        if r.status_code != 200:
            raise RuntimeError(f"sources request failed (HTTP {r.status_code})")
        return r.json().get("downloads") or {}

    def resolve_mediafire(self, slug, ep, token):
        headers = {
            "Accept": "application/json",
            "X-CSRF-TOKEN": self.csrf or "",
            "Referer": f"{BASE}/watch/{slug}/{ep}",
            "X-Requested-With": "XMLHttpRequest",
        }
        r = self.post(f"{BASE}/watch/download-source/{token}", headers=headers)
        if r.status_code != 200:
            raise RuntimeError(f"download-source failed (HTTP {r.status_code})")

        r = self.get(
            f"{BASE}/watch/download-gate/{token}",
            headers={"Referer": f"{BASE}/watch/{slug}/{ep}"},
            allow_redirects=False,
        )
        loc = r.headers.get("location")
        if not loc and r.status_code == 200:
            m = re.search(r"url=['\"]([^'\"]+)['\"]", r.text)
            loc = m.group(1) if m else None
        if not loc:
            raise RuntimeError(f"no redirect from download-gate (HTTP {r.status_code})")
        return loc

    def mediafire_direct(self, mf_url):
        r = self.get(mf_url, headers={"Referer": BASE})
        r.raise_for_status()
        html = r.text

        direct = None
        tag = re.search(r"<a[^>]*id=\"downloadButton\"[^>]*>", html)
        if tag:
            h = re.search(r'href="([^"]+)"', tag.group(0))
            if h:
                direct = h.group(1)
            if not direct:
                sc = re.search(r'data-scrambled-url="([^"]+)"', tag.group(0))
                if sc:
                    import base64

                    direct = base64.b64decode(sc.group(1)).decode()
        if not direct:
            m = re.search(r"https://download\d+\.mediafire\.com/[^\"'\\\s]+", html)
            direct = m.group(0) if m else None
        if not direct:
            raise RuntimeError("could not find direct MediaFire link (file may be removed)")

        r2 = self.s.get(
            direct,
            headers={"Referer": mf_url, "Range": "bytes=0-1"},
            stream=True,
            timeout=self.timeout,
        )
        try:
            cd = r2.headers.get("content-disposition", "")
            fname = None
            m = re.search(r'filename="?([^";]+)"?', cd)
            if m:
                fname = m.group(1)
            if not fname:
                fname = unquote(direct.split("?")[0].rstrip("/").split("/")[-1])
            total = r2.headers.get("content-length")
            cr = r2.headers.get("content-range", "")
            m = re.search(r"/(\d+)$", cr)
            if m:
                total = m.group(1)
        finally:
            r2.close()
        return direct, sanitize(fname), int(total) if total and total.isdigit() else None

    def _workupload_solve(self, ref_url):
        r = self.s.get(
            "https://workupload.com/puzzle",
            headers={"Referer": ref_url, "X-Requested-With": "XMLHttpRequest"},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(f"workupload puzzle failed (HTTP {r.status_code})")
        data = r.json()
        data = data.get("data") or data
        puzzle = data.get("puzzle")
        find = data.get("find")
        rng = int(data.get("range") or 0)
        if not puzzle or not find or rng <= 0:
            raise RuntimeError("workupload puzzle response incomplete")
        if isinstance(find, str):
            targets = [find[i : i + 64] for i in range(0, len(find), 64)]
        else:
            targets = list(find)
        solutions = {}
        for i in range(rng):
            h = hashlib.sha256((puzzle + str(i)).encode()).hexdigest()
            if h in targets:
                solutions[h] = i
        if any(t not in solutions for t in targets):
            raise RuntimeError("workupload puzzle not solvable")
        captcha = " ".join(str(solutions[t]) for t in targets) + " "
        r = self.s.post(
            "https://workupload.com/captcha",
            data={"captcha": captcha},
            headers={"Referer": ref_url, "X-Requested-With": "XMLHttpRequest"},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(f"workupload captcha failed (HTTP {r.status_code})")

    @staticmethod
    def _parse_workupload_page(html):
        name = None
        m = re.search(r"Filename:&nbsp;</td><td[^>]*>(.*?)</td>", html, re.S)
        if m:
            name = html_unescape(m.group(1)).strip()
        if not name:
            m = re.search(r"<title>(.*?)</title>", html, re.S)
            if m:
                name = html_unescape(m.group(1)).strip()
                name = re.sub(r"\s*[-|]\s*workupload(?:\.com)?\s*$", "", name, flags=re.I)
        size = None
        m = re.search(r"Filesize:&nbsp;</td><td>\s*(\d+)\s*\(Byte\)", html)
        if m:
            size = int(m.group(1))
        return name, size

    def workupload_direct(self, wu_url):
        m = re.search(r"workupload\.com/file/([A-Za-z0-9]+)", wu_url)
        if not m:
            raise RuntimeError(f"not a workupload file link: {wu_url}")
        fid = m.group(1)
        fname = size = None
        real = False
        for attempt in range(1, 4):
            r = self.s.get(wu_url, headers={"Referer": BASE}, timeout=self.timeout)
            r.raise_for_status()
            if "/start/" in r.text or "Filesize:" in r.text:
                fname, size = self._parse_workupload_page(r.text)
                real = True
                break
            log(f"    workupload: solving human-verification puzzle (attempt {attempt}/3) ...")
            self._workupload_solve(wu_url)
        if not real:
            raise RuntimeError("workupload: could not pass human verification")
        self.s.get(
            f"https://workupload.com/start/{fid}",
            headers={"Referer": wu_url},
            timeout=self.timeout,
        )
        api = self.s.get(
            f"https://workupload.com/api/file/getDownloadServer/{fid}",
            headers={"Referer": wu_url, "X-Requested-With": "XMLHttpRequest"},
            timeout=self.timeout,
        )
        direct = None
        try:
            direct = (api.json().get("data") or {}).get("url")
        except Exception:
            pass
        if not direct:
            raise RuntimeError(f"workupload gave no download url (HTTP {api.status_code})")
        return direct, sanitize(fname or fid), size

    def download(self, url, referer, dest, retries=3):
        part = dest + ".part"
        for attempt in range(1, retries + 1):
            try:
                r = self.s.get(
                    url,
                    headers={"Referer": referer},
                    stream=True,
                    timeout=(30, 120),
                )
                if r.status_code not in (200, 206):
                    raise RuntimeError(f"HTTP {r.status_code}")
                total = int(r.headers.get("content-length", 0))
                done = 0
                started = time.time()
                last_print = 0.0
                with open(part, "wb") as f:
                    for chunk in r.iter_content(CHUNK):
                        if not chunk:
                            continue
                        f.write(chunk)
                        done += len(chunk)
                        if PROGRESS:
                            now = time.time()
                            if now - last_print >= 0.2 or done == total:
                                last_print = now
                                speed = done / max(now - started, 0.001)
                                if total:
                                    pct = done * 100 / total
                                    bar = bar_str(pct)
                                    sys.stdout.write(
                                        f"\r    {bar} {pct:5.1f}%  {human(done)}/{human(total)}  {human(speed)}/s   "
                                    )
                                else:
                                    sys.stdout.write(f"\r    {human(done)}  {human(speed)}/s   ")
                                sys.stdout.flush()
                if PROGRESS:
                    sys.stdout.write("\n")
                os.replace(part, dest)
                return done
            except KeyboardInterrupt:
                if os.path.exists(part):
                    os.remove(part)
                raise
            except Exception as e:
                if os.path.exists(part):
                    os.remove(part)
                if attempt < retries:
                    log(f"    retry {attempt}/{retries - 1} after error: {e}")
                    time.sleep(3 * attempt)
                else:
                    raise RuntimeError(f"download failed: {e}")
        return 0


def extract_anime_title(html, slug):
    m = re.search(r'"partOfSeries"\s*:\s*\{\s*[^{}]*?"name"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    if not m:
        m = re.search(r'"@type"\s*:\s*"TVSeries"\s*,\s*"name"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    if m:
        try:
            return json.loads(f'"{m.group(1)}"')
        except Exception:
            return m.group(1)
    return slug


def match_server_entries(entries, server):
    out = [
        e
        for e in entries
        if str(e.get("label", "")).lower().startswith(server)
        and e.get("version", "sub") in ("sub", None, "")
    ]
    if not out:
        out = [e for e in entries if str(e.get("label", "")).lower().startswith(server)]
    if server == "mediafire" and not out:
        out = [e for e in entries if "mediafire" in str(e.get("label", "")).lower()]
    return out


def pick_downloads(downloads, quality, server):
    """Return (quality, entries, server_used) matching `server`, preferring `quality`,
    then other qualities in QUALITY_ORDER.
    When `server` is mediafire, workupload is used as fallback."""
    server = server.lower()
    qualities = [q for q in ([quality] if quality else []) if q and q in downloads]
    qualities += [q for q in QUALITY_ORDER if q in downloads and q not in qualities]
    servers = [server]
    if server == "mediafire":
        servers.append("workupload")
    for q in qualities:
        for srv in servers:
            entries = match_server_entries(downloads[q], srv)
            if entries:
                return q, entries, srv
    return None, [], server


def parse_target(target):
    target = target.strip()
    if target.startswith("http"):
        path = urlparse(target).path.strip("/")
    else:
        path = target.strip("/")
    parts = [p for p in path.split("/") if p]
    slug = None
    ep = None
    if len(parts) >= 2 and parts[0] in ("watch", "anime"):
        slug = parts[1]
        if parts[0] == "watch" and len(parts) >= 3 and parts[2].isdigit():
            ep = int(parts[2])
    elif len(parts) == 1:
        slug = parts[0]
    if not slug:
        raise SystemExit(f"could not parse target: {target}")
    return slug, ep


def parse_episodes(spec):
    eps = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            eps.update(range(int(a), int(b) + 1))
        else:
            eps.add(int(part))
    return eps


def save_links(path, exported):
    records = [exported[k] for k in sorted(exported, key=lambda k: (k[0] if k[0] is not None else 0, str(k[1])))]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)


def save_job(path, job):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_job(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return {"version": 1, "anime": None, "slug": Path(path).stem.split("__")[0], "episodes": data}
    if isinstance(data, dict) and isinstance(data.get("episodes"), list):
        return data
    raise ValueError(f"unrecognized job format: {path}")


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict):
            state.setdefault("done", {})
            state.setdefault("retry", {})
            return state
    except Exception:
        pass
    return {"done": {}, "retry": {}}


def save_state(path, state):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def find_repo_root():
    candidates = []
    try:
        candidates.append(Path(__file__).resolve().parent)
    except Exception:
        pass
    candidates.append(Path.cwd())
    for base in candidates:
        p = base
        for _ in range(5):
            if (p / ".git").exists() and (p / "witanime").is_dir():
                return p
            if p.parent == p:
                break
            p = p.parent
    if Path("/mnt/repo/witanime").is_dir():
        return Path("/mnt/repo")
    return None


def git_run(repo, *args, timeout=180):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def git_pull(repo):
    before = git_run(repo, "rev-parse", "HEAD").stdout.strip()
    r = git_run(repo, "pull", "--ff-only", "--quiet")
    after = git_run(repo, "rev-parse", "HEAD").stdout.strip()
    err = (r.stderr or r.stdout or "").strip().splitlines()
    return r.returncode == 0, (before != after), (err[-1] if err else "")


def git_commit_push(repo, job_path, message):
    git_pull(repo)
    git_run(repo, "add", "--", str(job_path))
    c = git_run(repo, "commit", "-m", message)
    if c.returncode != 0 and "nothing to commit" not in (c.stdout + c.stderr):
        return False, (c.stdout + c.stderr).strip()
    p = git_run(repo, "push")
    ok = p.returncode == 0
    return ok, (p.stdout + p.stderr).strip()


def git_commit_job_removal(repo, job_path):
    try:
        rel = Path(job_path).resolve().relative_to(Path(repo).resolve()).as_posix()
    except Exception:
        return False, "job file is outside the repo"
    rm = git_run(repo, "rm", "-q", "--ignore-unmatch", "--", rel)
    if rm.returncode != 0:
        return False, (rm.stdout + rm.stderr).strip()
    Path(job_path).parent.mkdir(parents=True, exist_ok=True)
    c = git_run(
        repo, "-c", "user.name=witanime-bot", "-c", "user.email=witanime-bot@local",
        "commit", "-m", f"job done: remove {Path(rel).name}",
    )
    c_out = (c.stdout + c.stderr).strip()
    if c.returncode != 0 and "nothing to commit" not in c_out:
        return False, c_out
    if "nothing to commit" in c_out:
        return True, "already committed"
    p = git_run(repo, "push")
    if p.returncode != 0:
        git_run(repo, "pull", "--rebase", "--autostash", timeout=300)
        p = git_run(repo, "push", timeout=300)
    p_out = (p.stdout + p.stderr).strip()
    if p.returncode != 0:
        return False, p_out.splitlines()[-1] if p_out else "push failed"
    return True, "pushed"


def remove_done_job(repo, job_path, name):
    try:
        job_path.unlink(missing_ok=True)
        log(f"[watcher] {name}: job file removed from jobs/")
    except Exception as e:
        log(f"[watcher] {name}: could not remove job file: {e}")
        return
    if repo and (Path(repo) / ".git").is_dir():
        try:
            ok, out = git_commit_job_removal(repo, job_path)
        except Exception as e:
            ok, out = False, str(e)
        if ok:
            log(f"[watcher] {name}: job file removal committed to git")
        else:
            log(f"[watcher] {name}: job file removal not pushed ({out}) - remove it manually on GitHub")


def extract_zip_flat(zip_path, dest_dir, ep_num=None):
    out_files = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            base = sanitize(os.path.basename(info.filename))
            if not base:
                continue
            target = dest_dir / base
            if target.exists():
                stem, ext = os.path.splitext(base)
                tag = f" EP{int(ep_num):02d}" if isinstance(ep_num, int) else ""
                target = dest_dir / f"{stem}{tag}{ext}"
                n = 2
                while target.exists():
                    target = dest_dir / f"{stem}{tag} ({n}){ext}"
                    n += 1
            part = dest_dir / (target.name + ".part")
            with z.open(info) as src, open(part, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            os.replace(part, target)
            out_files.append(target)
    if not any(f.suffix.lower() in VIDEO_EXTS for f in out_files):
        raise RuntimeError("zip contains no video file")
    return out_files


def build_final_zip(folder, out_zip):
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    if not files:
        raise RuntimeError("nothing to zip (empty folder)")
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for p in files:
            z.write(p, arcname=f"{folder.name}/{p.relative_to(folder).as_posix()}")
    with zipfile.ZipFile(out_zip) as z:
        infos = z.infolist()
        total = sum(i.file_size for i in infos)
    expected = sum(p.stat().st_size for p in files)
    if len(infos) != len(files) or total != expected:
        out_zip.unlink(missing_ok=True)
        raise RuntimeError(f"zip verify failed: {len(infos)}/{len(files)} entries, {total}/{expected} bytes")
    return out_zip


def is_html_file(path, n=512):
    try:
        with open(path, "rb") as f:
            head = f.read(n).lstrip().lower()
        return head.startswith(b"<!doctype") or head.startswith(b"<html")
    except Exception:
        return False


def zip_matches_job(zip_path, episodes, expected=0):
    try:
        with zipfile.ZipFile(zip_path) as z:
            infos = z.infolist()
    except Exception:
        return False
    if len(infos) < len(episodes):
        return False
    if expected and sum(i.file_size for i in infos) < expected * 0.95:
        return False
    return True


def check_disk_space(path, needed, label):
    free = shutil.disk_usage(path).free
    if free < needed:
        raise RuntimeError(
            f"insufficient disk space for {label}: free {human(free)}, need ~{human(needed)} "
            f"(free up space or use a codespace with a bigger disk)"
        )


def job_fname(rec, ep):
    url = rec.get("url") or ""
    return sanitize(
        rec.get("filename") or (unquote(url.split("?")[0].rstrip("/").split("/")[-1]) if url else f"ep{ep:02d}.zip")
    )


def write_summary(path, job, total, extracted, missing, zip_path, note=None):
    try:
        lines = [
            f"Anime: {job.get('anime') or job.get('slug')}",
            f"Slug: {job.get('slug')}",
            f"Quality: {job.get('quality')}",
            f"Episodes requested: {total}",
            f"Episodes extracted: {len(extracted)}",
            f"Missing episodes: {', '.join(str(e) for e in sorted(missing)) or 'none'}",
            f"Final zip: {zip_path or 'none'}",
        ]
        if note:
            lines.append(f"Note: {note}")
        lines.append(f"Finished: {now_iso()}")
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def process_job(job_path, repo, work_root, args, finalize_allowed=False):
    """Download -> extract flat into one folder -> single zip -> move to output. Safe to re-run."""
    job_path = Path(job_path)
    job = load_job(job_path)
    slug = job.get("slug") or job_path.stem.split("__")[0]
    title = sanitize(str(job.get("anime") or slug))
    episodes = job.get("episodes") or []
    if not episodes:
        log(f"[job] {title}: no episodes in job file")
        return {"finalized": True, "status": "failed", "missing": [], "anime": title, "zip": None}

    ws = Path(work_root) / slug
    zips_dir = ws / "zips"
    zips_dir.mkdir(parents=True, exist_ok=True)
    if getattr(args, "out", None):
        outdir = Path(args.out).expanduser()
    elif repo:
        outdir = Path(repo) / "witanime" / "output"
    else:
        outdir = ws / "output"
    outdir.mkdir(parents=True, exist_ok=True)
    final_zip = outdir / f"{title}.zip"
    tmp_zip = outdir / f"{title}.zip.tmp"
    tmp_zip.unlink(missing_ok=True)
    client = WitAnime(proxy=args.proxy)

    total = len(episodes)
    expected = sum(rec.get("size") or 0 for rec in episodes)
    if zip_matches_job(final_zip, episodes, expected):
        log(f"[job] {title}: output zip already exists and matches this job, skipping download")
        write_summary(
            outdir / f"{title}.txt", job, total,
            [rec.get("episode") for rec in episodes], [], final_zip,
            note="final zip already existed; reused without re-downloading",
        )
        shutil.rmtree(ws, ignore_errors=True)
        return {
            "finalized": True,
            "status": "ok",
            "missing": [],
            "anime": title,
            "zip": str(final_zip),
            "total": total,
            "extracted": total,
        }

    if expected:
        max_ep = max((rec.get("size") or 0 for rec in episodes), default=0)
        check_disk_space(ws, int(expected * 1.05) + max_ep + (512 << 20), f"{title} downloads")

    missing = []
    done_count = 0
    for rec in episodes:
        ep = rec.get("episode", "?")
        url = rec.get("url")
        if not url:
            missing.append(ep)
            continue
        fname = job_fname(rec, ep if isinstance(ep, int) else 0)
        dest = zips_dir / fname
        exp = rec.get("size")
        if dest.exists():
            if not exp or dest.stat().st_size == exp:
                done_count += 1
                continue
            log(f"[job] {title}: episode {ep} size mismatch, re-downloading")
            dest.unlink()
        page = rec.get("page") or BASE
        try:
            log(f"[job] {title}: downloading episode {ep} -> {fname}")
            err = None
            try:
                written = client.download(url, page, str(dest), retries=args.retries)
            except Exception as e:
                err = e
                written = 0
            if err or not written or is_html_file(dest) or (exp and written != exp):
                log(f"[job] {title}: episode {ep}: stored link failed, re-resolving fresh link ...")
                if dest.exists():
                    dest.unlink()
                server = str(rec.get("server") or "mediafire").lower()
                if server == "workupload":
                    direct, _fresh_name, fresh_size = client.workupload_direct(page)
                else:
                    direct, _fresh_name, fresh_size = client.mediafire_direct(page)
                if not exp:
                    exp = fresh_size
                written = client.download(direct, page, str(dest), retries=args.retries)
                if not written or is_html_file(dest) or (exp and written != exp):
                    raise RuntimeError(f"size mismatch after refresh (got {written}, expected {exp})")
            done_count += 1
            log(f"[job] {title}: episode {ep} downloaded ({human(written)})")
            time.sleep(args.delay)
        except KeyboardInterrupt:
            raise
        except Exception as ex:
            log(f"[job] {title}: episode {ep} FAILED -> {ex}")
            if dest.exists():
                dest.unlink()
            missing.append(ep)

    log(f"[job] {title}: downloaded {done_count}/{total}" + (f"  missing: {missing}" if missing else ""))
    if missing and not finalize_allowed:
        return {"finalized": False, "missing": missing, "anime": title}

    folder = ws / title
    folder.mkdir(parents=True, exist_ok=True)
    extracted = []
    for rec in episodes:
        ep = rec.get("episode", "?")
        if ep in missing:
            continue
        zpath = zips_dir / job_fname(rec, ep if isinstance(ep, int) else 0)
        if not zpath.exists():
            if ep not in missing:
                missing.append(ep)
            continue
        try:
            if zipfile.is_zipfile(zpath):
                files = extract_zip_flat(zpath, folder, ep)
                zpath.unlink()
                log(f"[job] {title}: episode {ep} extracted ({len(files)} file(s))")
            else:
                if is_html_file(zpath):
                    raise RuntimeError("downloaded file is HTML (challenge/error page), not media")
                fname = sanitize(zpath.name)
                target = folder / fname
                if target.exists():
                    target.unlink()
                os.replace(zpath, target)
                log(f"[job] {title}: episode {ep} kept as-is (not an archive) -> {fname}")
            extracted.append(ep)
        except Exception as ex:
            log(f"[job] {title}: episode {ep} extract FAILED -> {ex}")
            if ep not in missing:
                missing.append(ep)

    zip_published = None
    if extracted:
        folder_bytes = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
        check_disk_space(outdir, int(folder_bytes * 1.05) + (512 << 20), f"{title}.zip in output")
        log(f"[job] {title}: all episodes extracted, building {title}.zip ({len(extracted)} episode(s)) ...")
        build_final_zip(folder, tmp_zip)
        log(f"[job] {title}: zip built ({human(tmp_zip.stat().st_size)}), moving to output ...")
        os.replace(tmp_zip, final_zip)
        zip_published = final_zip
        status = "partial" if missing else "ok"
        log(f"[job] {title}: published {final_zip.name}")
    else:
        status = "failed"
        log(f"[job] {title}: no episodes available, nothing to zip")

    write_summary(outdir / f"{title}.txt", job, total, extracted, missing, zip_published)
    shutil.rmtree(ws, ignore_errors=True)
    return {
        "finalized": True,
        "status": status,
        "missing": sorted(missing, key=str),
        "anime": title,
        "zip": str(zip_published) if zip_published else None,
        "total": total,
        "extracted": len(extracted),
    }


def watch_mode(args):
    global PROGRESS
    PROGRESS = False

    repo = Path(args.repo).expanduser().resolve() if args.repo else find_repo_root()
    if not repo or not (Path(repo) / "witanime").is_dir():
        raise SystemExit("--repo required: path to the repo root containing witanime/")
    work = Path(args.work).expanduser() if args.work else Path.home() / "witanime_work"
    work.mkdir(parents=True, exist_ok=True)
    state_path = work / "watcher_state.json"
    state = load_state(state_path)
    jobs_dir = Path(repo) / "witanime" / "jobs"
    try:
        jobs_dir.mkdir(parents=True, exist_ok=True)
        probe = jobs_dir / ".write_test"
        probe.touch()
        probe.unlink()
    except Exception as e:
        raise SystemExit(
            f"[watcher] cannot write to {jobs_dir} ({e}).\n"
            "    Start the watcher as root, e.g.:\n"
            f"    docker exec -u root <container> bash {Path(__file__).resolve().parent}/start_bot.sh"
        )

    log(f"[watcher] started  repo={repo}  work={work}  interval={int(args.interval)}s")
    log(f"[watcher] watching {jobs_dir}")

    while True:
        try:
            try:
                ok, changed, pull_err = git_pull(repo)
                if changed:
                    log("[watcher] repo updated (git pull)")
                elif not ok and pull_err:
                    log(f"[watcher] git pull failed: {pull_err}")
            except Exception as e:
                log(f"[watcher] git pull failed: {e}")

            for job_path in sorted(jobs_dir.glob("*.json")):
                name = job_path.name
                if name in state.get("done", {}):
                    if job_path.exists():
                        log(f"[watcher] {name}: already done earlier, removing re-added job file")
                        remove_done_job(repo, job_path, name)
                    continue
                rinfo = state.get("retry", {}).get(name)
                if rinfo and time.time() < rinfo.get("next", 0):
                    continue
                rounds = rinfo.get("round", 0) if rinfo else 0
                finalize_allowed = rounds >= len(WATCH_RETRY_DELAYS)
                log(f"[watcher] processing {name} (attempt {rounds + 1})")
                try:
                    res = process_job(job_path, repo, work, args, finalize_allowed=finalize_allowed)
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    log(f"[watcher] {name} crashed: {e}")
                    res = {"finalized": False, "missing": [], "anime": name, "error": str(e)}

                if res.get("finalized"):
                    state.setdefault("done", {})[name] = {
                        "at": now_iso(),
                        "status": res.get("status"),
                        "missing": res.get("missing", []),
                        "zip": res.get("zip"),
                    }
                    state.get("retry", {}).pop(name, None)
                    log(f"[watcher] {name} DONE: status={res.get('status')} zip={res.get('zip')}")
                    remove_done_job(repo, job_path, name)
                else:
                    delay = WATCH_RETRY_DELAYS[min(rounds, len(WATCH_RETRY_DELAYS) - 1)]
                    err = res.get("error")
                    state.setdefault("retry", {})[name] = {
                        "round": rounds + 1,
                        "next": time.time() + delay,
                        "missing": res.get("missing", []),
                        "error": err,
                    }
                    if err:
                        log(f"[watcher] {name}: {err}  (retrying in {delay // 60} min)")
                    else:
                        log(
                            f"[watcher] {name}: {len(res.get('missing', []))} episode(s) missing, "
                            f"retry in {delay // 60} min"
                        )
                save_state(state_path, state)
        except KeyboardInterrupt:
            log("[watcher] stopped by user")
            return
        except Exception as e:
            log(f"[watcher] error: {e}")
        time.sleep(max(5.0, args.interval))


def interactive(args):
    log("=" * 62)
    log("  WitAnime Downloader - resolve links  (run this on your PC)")
    log("=" * 62)
    try:
        target = input("Paste the link of the first episode: ").strip()
    except EOFError:
        raise SystemExit("no input")
    if not target:
        raise SystemExit("no link provided")

    quality = args.quality
    if not quality:
        try:
            quality = input("Quality [FHD]: ").strip().upper() or "FHD"
        except EOFError:
            quality = "FHD"

    slug, ep = parse_target(target)
    client = WitAnime(proxy=args.proxy, user_agent=args.user_agent, cookie=args.cookie)

    log(f"Anime slug: {slug}")
    all_eps = client.discover_episodes(slug)
    if not all_eps:
        raise SystemExit("could not find any episodes for this anime")

    if args.episodes:
        chosen = sorted(parse_episodes(args.episodes))
        chosen = [e for e in chosen if e in all_eps] or chosen
    else:
        start = args.ep_from if args.ep_from is not None else (ep if ep else all_eps[0])
        end = args.ep_to if args.ep_to is not None else all_eps[-1]
        chosen = [e for e in all_eps if start <= e <= end]
    if not chosen:
        raise SystemExit("no episodes selected")

    anime_title = getattr(client, "anime_title", None) or slug
    log(f"Title: {anime_title}")
    log(f"Episodes: {len(chosen)}  |  Quality: {quality}  |  Server: mediafire (workupload fallback)")
    log("-" * 62)

    exported = {}
    ok = failed = 0
    for i, e in enumerate(chosen, 1):
        prefix = f"[{i}/{len(chosen)}] Episode {e}"
        try:
            html = client.watch_html(slug, e)
            if not html:
                log(f"{prefix}: not found on site, skipping")
                failed += 1
                continue
            downloads = client.downloads(slug, e)
            quality_used, entries, server_used = pick_downloads(downloads, quality, "mediafire")
            if not entries:
                log(f"{prefix}: no mediafire/workupload link available, skipping")
                failed += 1
                continue
            for entry in entries:
                label = entry.get("label", "file")
                src_url = client.resolve_mediafire(slug, e, entry["token"])
                if server_used == "workupload":
                    direct, fname, size = client.workupload_direct(src_url)
                else:
                    direct, fname, size = client.mediafire_direct(src_url)
                exported[(e, label)] = {
                    "episode": e,
                    "quality": quality_used,
                    "label": label,
                    "server": server_used,
                    "filename": fname,
                    "size": size,
                    "page": src_url,
                    "url": direct,
                }
                log(f"{prefix} ({quality_used} {label} [{server_used}]): link saved ({human(size)}) -> {fname}")
                ok += 1
            time.sleep(args.delay)
        except KeyboardInterrupt:
            log("\nInterrupted by user.")
            break
        except SystemExit:
            raise
        except Exception as ex:
            log(f"{prefix}: FAILED -> {ex}")
            failed += 1

    log("-" * 62)
    if not exported:
        raise SystemExit("no links resolved - job not created")

    job = {
        "version": 2,
        "anime": anime_title,
        "slug": slug,
        "quality": quality,
        "created": now_iso(),
        "episodes": [
            exported[k] for k in sorted(exported, key=lambda k: (k[0], str(k[1])))
        ],
    }

    repo = find_repo_root()
    if repo:
        jobs_dir = Path(repo) / "witanime" / "jobs"
    else:
        jobs_dir = Path.cwd()
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_path = jobs_dir / f"{slug}__{job_timestamp()}.json"
    save_job(job_path, job)
    log(f"Job saved: {job_path}  ({ok} link(s), {failed} failed)")

    if repo and not args.no_push:
        pushed, out = git_commit_push(repo, job_path, f"job: {anime_title}")
        if pushed:
            log("=" * 62)
            log("  PUSHED to GitHub. The Codespace watcher will pick it up")
            log("  within a minute and download everything automatically.")
            log("=" * 62)
        else:
            log("[!] git push failed - run these manually in the repo folder:")
            log(f'    git add "{job_path}" && git commit -m "job: {anime_title}" && git push')
            if out:
                log(f"    ({out.splitlines()[-1] if out.splitlines() else ''})")
    else:
        log("Job not pushed (--no-push or no repo found).")
        if repo:
            log(f'To push manually: git -C "{repo}" add . && git -C "{repo}" commit -m "job: {anime_title}" && git -C "{repo}" push')


def run_from_file(args):
    with open(args.from_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    records = data.get("episodes") if isinstance(data, dict) else data
    if not records:
        raise SystemExit("link file is empty")

    outdir = args.out or "downloads"
    os.makedirs(outdir, exist_ok=True)
    log(f"Links: {len(records)}  |  Output: {os.path.abspath(outdir)}")
    log("-" * 60)

    client = WitAnime(proxy=args.proxy, user_agent=args.user_agent)
    ok = skipped = failed = 0
    for i, rec in enumerate(records, 1):
        url = rec.get("url")
        ep = rec.get("episode", "?")
        fname = job_fname(rec, ep if isinstance(ep, int) else 0)
        prefix = f"[{i}/{len(records)}] Episode {ep} ({rec.get('quality', '?')} {rec.get('label', '?')})"
        if not url:
            log(f"{prefix}: record has no url, skipping")
            failed += 1
            continue
        dest = os.path.join(outdir, fname)
        if os.path.exists(dest):
            log(f"{prefix}: already downloaded, skipping -> {fname}")
            skipped += 1
            continue
        if args.dry_run:
            log(f"{prefix}: would download {fname} ({human(rec.get('size'))})")
            continue
        try:
            log(f"{prefix}: {fname} ({human(rec.get('size'))})")
            written = client.download(url, rec.get("page") or BASE, dest, retries=args.retries)
            log(f"    done: {fname} ({human(written)})")
            ok += 1
            time.sleep(1.0)
        except KeyboardInterrupt:
            log("\nInterrupted by user.")
            break
        except Exception as ex:
            log(f"{prefix}: FAILED -> {ex}")
            failed += 1

    log("-" * 60)
    log(f"Finished. Downloaded: {ok}  Skipped: {skipped}  Failed: {failed}")


def main():
    ap = argparse.ArgumentParser(
        prog="witanime_dl",
        description="WitAnime Downloader Bot: resolve links on PC, auto-download on Codespace.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("target", nargs="?", default=None, help="watch URL, anime URL or slug (legacy modes)")
    ap.add_argument("--from", dest="ep_from", type=int, default=None, help="first episode")
    ap.add_argument("--to", dest="ep_to", type=int, default=None, help="last episode")
    ap.add_argument("--episodes", default=None, help='specific episodes, e.g. "1-5,8,10"')
    ap.add_argument("--quality", default=None, help="preferred quality: FHD, HD, SD (interactive: prompts; default FHD)")
    ap.add_argument("--server", default="mediafire", help="download server label (default: mediafire)")
    ap.add_argument("--out", default=None, help="output directory override")
    ap.add_argument("--delay", type=float, default=3.0, help="seconds between episodes while resolving (default: 3)")
    ap.add_argument("--retries", type=int, default=3, help="retries per request/download (default: 3)")
    ap.add_argument("--list", action="store_true", help="only list episodes and exit")
    ap.add_argument("--dry-run", action="store_true", help="resolve everything but do not download files")
    ap.add_argument("--proxy", default=None, help="HTTP(S) proxy URL, e.g. http://user:pass@host:port")
    ap.add_argument("--user-agent", default=None, help="custom User-Agent (required with --cookie)")
    ap.add_argument("--cookie", default=None, help='raw cookie string, e.g. "cf_clearance=abc123"')
    ap.add_argument("--export-links", nargs="?", const="", default=None, metavar="FILE",
                    help="legacy: resolve links only and save to JSON")
    ap.add_argument("--refresh", action="store_true", help="with --export-links: re-resolve existing entries")
    ap.add_argument("--from-file", default=None, metavar="FILE",
                    help="JSON file (job or legacy link list) to download/process")
    ap.add_argument("--process", action="store_true",
                    help="with --from-file: run full pipeline (download+extract+single folder+zip+output)")
    ap.add_argument("--no-push", action="store_true", help="interactive mode: do not git commit/push the job")
    ap.add_argument("--watch", action="store_true", help="Codespace daemon: watch repo jobs and process automatically")
    ap.add_argument("--repo", default=None, help="repo root for --watch (default: auto-detect)")
    ap.add_argument("--work", default=None, help="work directory (default: ~/witanime_work)")
    ap.add_argument("--interval", type=float, default=45.0, help="watcher poll interval seconds (default: 45)")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.watch:
        return watch_mode(args)

    if args.from_file:
        if args.process:
            repo = find_repo_root()
            work = Path(args.work).expanduser() if args.work else Path.home() / "witanime_work"
            work.mkdir(parents=True, exist_ok=True)
            res = process_job(Path(args.from_file), repo, work, args, finalize_allowed=True)
            log("-" * 60)
            log(f"Result: status={res.get('status')}  zip={res.get('zip')}  missing={res.get('missing')}")
            return
        return run_from_file(args)

    if not args.target:
        return interactive(args)

    # ---- legacy direct-download / export modes ----
    args.quality = args.quality or "FHD"
    slug, ep = parse_target(args.target)
    client = WitAnime(proxy=args.proxy, user_agent=args.user_agent, cookie=args.cookie)

    log(f"Anime: {slug}")
    all_eps = client.discover_episodes(slug)
    if not all_eps:
        raise SystemExit("could not find any episodes for this anime")

    if args.episodes:
        chosen = sorted(parse_episodes(args.episodes))
        chosen = [e for e in chosen if e in all_eps] or sorted(parse_episodes(args.episodes))
    else:
        start = args.ep_from if args.ep_from is not None else (ep if ep else all_eps[0])
        end = args.ep_to if args.ep_to is not None else all_eps[-1]
        chosen = [e for e in all_eps if start <= e <= end]

    if not chosen:
        raise SystemExit("no episodes selected")

    if args.list:
        log(f"Episodes available: {len(all_eps)} (min {all_eps[0]}, max {all_eps[-1]})")
        log("Selected: " + ", ".join(str(e) for e in chosen))
        return

    outdir = args.out or os.path.join("downloads", slug)
    export_path = None
    exported = {}
    if args.export_links is not None:
        export_path = args.export_links or f"links_{slug}.json"
        if os.path.exists(export_path):
            try:
                with open(export_path, "r", encoding="utf-8") as f:
                    for r in json.load(f):
                        exported[(r.get("episode"), r.get("label"))] = r
            except Exception:
                pass
        log(f"Mode: export links  ->  {export_path}")
    else:
        os.makedirs(outdir, exist_ok=True)
        log(f"Output: {os.path.abspath(outdir)}")
    log(f"Episodes: {len(chosen)}  |  Quality: {args.quality}  |  Server: {args.server}")
    log("-" * 60)

    ok = skipped = failed = 0
    for i, e in enumerate(chosen, 1):
        prefix = f"[{i}/{len(chosen)}] Episode {e}"
        try:
            if not client.watch_html(slug, e):
                log(f"{prefix}: not found on site, skipping")
                failed += 1
                continue

            downloads = client.downloads(slug, e)
            quality_used, entries, server_used = pick_downloads(downloads, args.quality, args.server)
            if not entries:
                log(f"{prefix}: no '{args.server}' link available, skipping")
                failed += 1
                continue

            for entry in entries:
                part_label = entry.get("label", "file")
                src_url = client.resolve_mediafire(slug, e, entry["token"])
                if server_used == "workupload":
                    direct, fname, size = client.workupload_direct(src_url)
                else:
                    direct, fname, size = client.mediafire_direct(src_url)
                dest = os.path.join(outdir, fname)

                if export_path:
                    key = (e, part_label)
                    if key in exported and not args.refresh:
                        log(f"{prefix} ({quality_used} {part_label}): already in link file, skipping")
                        skipped += 1
                        continue
                    exported[key] = {
                        "episode": e,
                        "quality": quality_used,
                        "label": part_label,
                        "server": server_used,
                        "filename": fname,
                        "size": size,
                        "page": src_url,
                        "url": direct,
                    }
                    save_links(export_path, exported)
                    log(f"{prefix} ({quality_used} {part_label} [{server_used}]): link saved ({human(size)}) -> {fname}")
                    ok += 1
                    continue

                if os.path.exists(dest):
                    log(f"{prefix} ({quality_used} {part_label}): already downloaded, skipping -> {fname}")
                    skipped += 1
                    continue

                if args.dry_run:
                    log(f"{prefix} ({quality_used} {part_label}): would download {fname} ({human(size)})")
                    continue

                log(f"{prefix} ({quality_used} {part_label}): {fname} ({human(size)})")
                written = client.download(direct, src_url, dest, retries=args.retries)
                log(f"    done: {fname} ({human(written)})")
                ok += 1
                time.sleep(1.0)

            time.sleep(args.delay)
        except KeyboardInterrupt:
            log("\nInterrupted by user.")
            break
        except Exception as ex:
            log(f"{prefix}: FAILED -> {ex}")
            failed += 1

    log("-" * 60)
    if export_path:
        log(f"Finished. Links saved: {ok}  Skipped: {skipped}  Failed: {failed}  ->  {export_path}")
    else:
        log(f"Finished. Downloaded: {ok}  Skipped: {skipped}  Failed: {failed}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
