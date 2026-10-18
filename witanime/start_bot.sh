#!/usr/bin/env bash
# Starts the WitAnime watcher daemon in the background. Safe to re-run (restarts it).
# Usage (inside the container):  bash /mnt/repo/witanime/start_bot.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "[!] WARNING: not running as root - writes to the repo mount may fail."
    echo "    Recommended: docker exec -u root <container> bash $0"
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="${WITANIME_WORK:-$HOME/witanime_work}"
LOG="$WORK_DIR/watcher.log"

mkdir -p "$WORK_DIR" "$REPO_DIR/witanime/jobs" "$REPO_DIR/witanime/output"

if [ -n "${GITHUB_TOKEN:-}" ]; then
    git config --global credential.https://github.com.helper \
        '!f() { echo username=x-access-token; echo "password=${GITHUB_TOKEN}"; }; f'
    echo "==> GitHub push credentials configured (GITHUB_TOKEN)"
else
    echo "[!] GITHUB_TOKEN is not set: the watcher can download and publish, but"
    echo "    pushing job-file removals to GitHub will fail. Start it like this:"
    echo "    docker exec -u root -e GITHUB_TOKEN=\"\$GITHUB_TOKEN\" <container> bash $0"
fi

echo "==> Stopping any previous watcher"
pkill -f "witanime_dl.py --watch" 2>/dev/null && sleep 1 || true

echo "==> Starting watcher"
cd "$REPO_DIR/witanime"
nohup python3 witanime_dl.py --watch --repo "$REPO_DIR" --work "$WORK_DIR" >> "$LOG" 2>&1 &
PID=$!
sleep 3

if kill -0 "$PID" 2>/dev/null; then
    echo "Watcher running (PID $PID)"
    echo "  repo: $REPO_DIR"
    echo "  log:  $LOG"
    echo "  follow the log with:  tail -f \"$LOG\""
else
    echo "[!] Watcher exited immediately. Last log lines:"
    tail -n 30 "$LOG" || true
    exit 1
fi
