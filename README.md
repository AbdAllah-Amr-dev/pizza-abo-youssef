# pizza-abo-youssef — WitAnime Downloader Bot

Automated, split-machine downloader for [witanime.site](https://witanime.site) anime episodes.

The idea: your **PC resolves download links** (it has the browser-grade session/network the site likes), then hands the work
to a **Codespace watcher** through the git repo. The watcher downloads everything, builds **one flat zip per anime**, and
publishes it to `witanime/output/` — no interaction needed.

```
 PC (interactive)                       GitHub repo                       Codespace (daemon)
 ─────────────────                      ───────────                       ──────────────────
 python witanime_dl.py                  witanime/jobs/*.json    ──pull──▶ witanime_dl.py --watch
  paste ep.1 URL / quality    ──push──▶ (job file)                        downloads episodes
  resolve mediafire/workupload                                              extracts (or keeps raw mp4)
  build job JSON                                                            builds "<Anime Title>.zip"
                                                                            moves zip to witanime/output/
                                                                            deletes job file + commits removal
```

---

## Repo layout

| Path | What it is |
| --- | --- |
| `witanime/witanime_dl.py` | The whole bot: resolver, downloader, watcher daemon (single file, stdlib + `curl_cffi`) |
| `witanime/jobs/` | Job queue. PC drops `<slug>__<timestamp>.json` here; watcher deletes them when done |
| `witanime/output/` | Published results: `<Anime Title>.zip` + `<Anime Title>.txt` summary (git-ignored) |
| `witanime/setup_codespace.sh` | One-time container setup: installs python deps (`curl_cffi`, git, zip/unzip) |
| `witanime/start_bot.sh` | Starts/restarts the watcher daemon in the background (`nohup`, logs to `~/witanime_work/watcher.log`) |
| `run` | Codespace one-shot: creates the Docker desktop container, installs Firefox, runs setup + start_bot |
| `stay up` | Keeps the Codespace alive for 12 h (`timeout 12h` keepalive loop) — **burns Codespaces quota**, use sparingly |
| `index.html` | Static Arabic (RTL) menu/ordering page for "أبو يوسف" — the restaurant the repo is named after; unrelated to the bot |

## The container

`run` starts a headless-desktop container from `accetto/ubuntu-vnc-xfce-g3`:

- name `xfce-desktop`, KasmVNC desktop exposed on **port 6901**
- repo mounted at `/mnt/repo` (`-v /workspaces/pizza-abo-youssef:/mnt/repo`)
- installs a real **Firefox** (Mozilla apt repo) so you can log in / solve challenges manually if ever needed
- then runs `witanime/setup_codespace.sh` and `witanime/start_bot.sh` (as root)

Everything that writes to the repo mount must run as root:
`docker exec -u root xfce-desktop bash /mnt/repo/witanime/start_bot.sh`

---

## Daily usage

### 1. On the PC — create a job

```bash
python witanime/witanime_dl.py
```

- paste the **first episode URL** (e.g. `https://witanime.site/watch/black-torch/1`)
- answer the quality prompt (`FHD` / `HD` / `SD`, default `FHD`)
- the bot discovers all episodes, resolves a download link for each one (mediafire preferred, **workupload fallback**),
  writes `witanime/jobs/<slug>__<timestamp>.json` and **git commits + pushes it**
- `--no-push` skips the push; `--episodes "1-5,8"` selects episodes non-interactively

### 2. In the Codespace — the watcher (normally already running)

```bash
bash /mnt/repo/witanime/setup_codespace.sh   # first time only
bash /mnt/repo/witanime/start_bot.sh         # start / restart
tail -f ~/witanime_work/watcher.log          # watch it work
```

The watcher loop (every `--interval` seconds, default 45):

1. `git pull`
2. for each `witanime/jobs/*.json`: download → extract → publish zip → **delete the job file and push the removal**
3. failures are retried after 5 / 15 / 30 min; after that it publishes whatever succeeded as `partial`

### 3. Grab the result

`witanime/output/<Anime Title>.zip` (when using the codespace: pull it from the repo or download it in the GitHub UI).
Next to it is `<Anime Title>.txt` with a per-anime summary (episodes extracted/missing, final zip name).

---

## What the output looks like

- **one folder level, flat**: `BLACK TORCH.zip` contains `BLACK TORCH/[Witanime.com] BT EP 01 SD.mp4`, `... EP 02 ...` — all episodes side by side
- mediafire mirrors usually serve episode **zip archives** → the watcher extracts them flat, deletes the archive, then zips everything into the final zip
- workupload mirrors serve **raw video files** → they are kept as-is and zipped directly
- the final zip is **moved** (atomic `os.replace`) into `output/` — never copied, no leftovers
- re-running the same job does nothing if the output zip already matches (entry count + ≥95 % of expected bytes)

## Download servers

| Server | Role | How it's resolved |
| --- | --- | --- |
| MediaFire | primary | `resolve_mediafire()` via the witanime download-gate → `mediafire_direct()` extracts the CDN link from the file page |
| WorkUpload | fallback when a quality has no mediafire entry | SHA-256 proof-of-work puzzle (`/puzzle` → solve with `hashlib` → `POST /captcha`) → `/start/<id>` → `/api/file/getDownloadServer/<id>` returns the session-bound CDN URL |

Stored mediafire links expire. When the watcher detects an expired/broken link it re-resolves automatically — for
workupload it solves the puzzle again with its own session (the CDN URL is session-bound, so it must be fetched at download time).
Each job episode records which server was used (`"server"` field); old jobs without the field are treated as mediafire.

## Job file format (v2)

```json
{
  "version": 2,
  "anime": "BLACK TORCH",
  "slug": "black-torch",
  "quality": "SD",
  "created": "2026-10-18T19:06:45",
  "episodes": [
    {
      "episode": 1,
      "quality": "SD",
      "label": "SD",
      "server": "workupload",
      "filename": "[Witanime.com] BT EP 01 SD.mp4",
      "size": 65846910,
      "page": "https://workupload.com/file/XXXXXXXX",
      "url": "https://f69.workupload.com/download/XXXXXXXX"
    }
  ]
}
```

`page` is used as the Referer and for refresh; `url` is the (possibly stale) direct link. Download-time validation:
size must match `size` (± the site's reported value) and the content must not be an HTML page.

## CLI reference

```
python witanime_dl.py                          # interactive: resolve links on PC, push job
python witanime_dl.py --watch [--repo R] [--work W] [--interval 45]   # codespace watcher

# legacy / manual modes
python witanime_dl.py <watch-url|anime-url|slug> [options]      # direct download here
python witanime_dl.py <target> --export-links links.json        # resolve links only
python witanime_dl.py --from-file links.json                    # download only
python witanime_dl.py --from-file job.json --process            # full pipeline once (local test)
```

| Option | Meaning |
| --- | --- |
| `--from N` / `--to N` / `--episodes "1-5,8"` | episode selection |
| `--quality FHD\|HD\|SD` | preferred quality (falls back down the quality order) |
| `--server mediafire` | preferred server (mediafire gets the workupload fallback) |
| `--out DIR` | output directory override |
| `--delay S` / `--retries N` | pacing and retry counts |
| `--dry-run` / `--list` | resolve/list without downloading |
| `--proxy URL` / `--cookie "cf_clearance=..."` / `--user-agent UA` | Cloudflare workarounds for the PC side |
| `--no-push` | interactive mode: create the job but don't push it |
| `--refresh` | with `--export-links`: re-resolve existing entries |

## Reliability details

- **Disk preflight** before downloading and before publishing (`free < needed` → clear error, no half-broken state) — the codespace disk is ~32 GB, keep an eye on big seasons
- downloads go to `<file>.part` and are renamed on completion; stale `*.zip.tmp` from a crashed run is cleaned at job start
- the watcher state lives in `~/witanime_work/watcher_state.json` (`done` / `retry` maps); deleting it simply lets the watcher re-process any jobs still on disk
- work directory is wiped per job (`~/witanime_work/<slug>/`) — only the final zip is kept
- the watcher pulls with `git pull --rebase --autostash`, so local job-removal commits that couldn't be pushed are replayed on top of new commits automatically (no more "Not possible to fast-forward" deadlock)
- job removal is committed by the watcher as `witanime-bot` and pushed with a `--rebase --autostash` retry; if the push still fails, remove the file on GitHub manually

### Git credentials inside the container

The watcher runs in the `xfce-desktop` container, whose git does **not** inherit the codespace's
GitHub login. Pass the codespace token when starting it so removals can be pushed:

```bash
docker exec -u root -e GITHUB_TOKEN="$GITHUB_TOKEN" xfce-desktop bash /mnt/repo/witanime/start_bot.sh
```

`start_bot.sh` installs a `credential.https://github.com.helper` that reads `$GITHUB_TOKEN` at push time
and warns loudly if the token is missing (downloads still work, only pushes fail).

### Running on a fork (another account)

If the codespace lives on a fork, the watcher's pushes (job removals) go to the **fork**, while the
original repo keeps its copy of the job files. To get updates into the fork/codespace:

```bash
cd /workspaces/pizza-abo-youssef
git remote add upstream https://github.com/<original-owner>/pizza-abo-youssef.git   # once
git pull --rebase --autostash upstream main
git push
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Cloudflare is challenging this IP` on the PC | normal network/VPN issue — use a residential connection or `--proxy`, or pass a browser `--cookie "cf_clearance=..."` + `--user-agent` |
| watcher says `cannot write to .../jobs` | run it as root: `docker exec -u root xfce-desktop bash .../start_bot.sh` |
| `could not read Username for 'https://github.com'` when removing a job | the watcher was started without `GITHUB_TOKEN` — restart it with `docker exec -u root -e GITHUB_TOKEN="$GITHUB_TOKEN" xfce-desktop bash .../start_bot.sh` |
| `git pull failed: Not possible to fast-forward` | local unpushed job-removal commits diverged from origin. Fix credentials as above (new code rebases automatically); one-time recovery on the codespace host: `cd /workspaces/pizza-abo-youssef && git pull --rebase --autostash && git push` |
| `insufficient disk space ...` | free space in the codespace (delete old `witanime_work` dirs / outputs) or use a bigger machine |
| Codespaces says *out of monthly free usage* | Free plan = 120 core-hours/month (2-core = 60 h, 4-core = 30 h). Stop the codespace when idle; avoid leaving `stay up` running |
| job stuck in `retry` | check `~/witanime_work/watcher.log` — it logs the exact error per attempt |

## Requirements

- PC: Python 3.9+ with `curl_cffi`, git, and push access to this repo
- Codespace/container: Debian-ish base with `python3-pip`, `git`, `zip`, `unzip` — `setup_codespace.sh` installs everything else
- No server-side credentials are stored anywhere: the witanime session, mediafire links and workupload puzzle/session are all ephemeral
