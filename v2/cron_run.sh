#!/usr/bin/env bash
#
# Scheduled entry point for the v2 pipeline (one cycle, then exit).
#
# cron gives a job almost no environment: no venv, no PATH to the project, and
# it starts in $HOME rather than the repo. Everything here is therefore absolute
# and explicit — that is the whole reason this wrapper exists instead of putting
# the python command straight in the crontab.
#
# Install (every 3 hours):
#   crontab -e
#   0 */3 * * * /path/to/job-agent/v2/cron_run.sh
#
# Why 3 hours and not hourly: a full scrape of all 19 keywords across LinkedIn,
# Indeed, Remotive and Himalayas was MEASURED at ~150s per keyword — ~48 min —
# and the gate pass adds ~20 min on top. That does not fit in an hour. flock
# would silently drop the overlapping runs, which looks like a working schedule
# while half the ticks do nothing.
#
# Check it ran:     tail -f /path/to/job-agent/data/logs/cron.log
# Stop it:          crontab -e   (delete the line)

set -uo pipefail

# The repo is wherever this script lives; cron calls it by absolute path.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$REPO/.venv/bin/python"
LOG_DIR="$REPO/data/logs"
LOG="$LOG_DIR/cron.log"
LOCK="$LOG_DIR/.cron.lock"

# Gate at most this many jobs per run. At the measured 30s cooldown that is
# ~20 minutes of work, comfortably inside the hour, so a big backlog drains
# over days instead of one marathon run that overlaps the next tick and cooks
# someone else's laptop.
LIMIT=40
COOLDOWN=30

mkdir -p "$LOG_DIR"

# One run at a time. A slow gate pass must never overlap the next hour's tick;
# -n means "give up immediately" rather than queue a second run behind it.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date -Is)] SKIP — previous run still going" >> "$LOG"
  exit 0
fi

cd "$REPO" || { echo "[$(date -Is)] FATAL — repo missing: $REPO" >> "$LOG"; exit 2; }

if [[ ! -x "$PYTHON" ]]; then
  echo "[$(date -Is)] FATAL — venv python missing: $PYTHON" >> "$LOG"
  exit 2
fi

echo "[$(date -Is)] START (limit=$LIMIT cooldown=${COOLDOWN}s)" >> "$LOG"

"$PYTHON" v2/loop.py --once --limit "$LIMIT" --cooldown "$COOLDOWN" >> "$LOG" 2>&1
rc=$?

# 0 = cycle completed. 1 = the M4 was unreachable, so jobs were scraped and
# stored but not gated; they queue for the next run. Anything else is a crash.
case $rc in
  0) echo "[$(date -Is)] OK" >> "$LOG" ;;
  1) echo "[$(date -Is)] GATE DOWN — jobs stored ungated, will retry next run" >> "$LOG" ;;
  *) echo "[$(date -Is)] FAILED rc=$rc" >> "$LOG" ;;
esac

exit $rc
