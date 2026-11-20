#!/usr/bin/env python3
"""Market-session loop for the paper trader.

GitHub Actions heavily throttles `schedule:` triggers (often only 1-2 of the
requested runs per day land, usually after the close). Instead of relying on
one cron firing per bar, any scheduled trigger that lands starts this loop,
which runs trader.py every 30 minutes inside a single job until the US market
closes or the job's time budget runs out. Later triggers queue behind it via
the workflow's concurrency group and take over if the budget expires.

Outside market hours it runs trader.py once (dashboard refresh / off-hours
flatten/close commands) and exits.
"""
import os
import subprocess
import sys
import time
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SESSION_OPEN = dtime(9, 30)
SESSION_END = dtime(16, 5)          # one final run just after the 16:00 close
INTERVAL_MIN = int(os.environ.get("LOOP_INTERVAL_MIN", "30"))
OFFSET_MIN = int(os.environ.get("LOOP_OFFSET_MIN", "2"))   # run at :02 / :32 ET
MAX_MIN = float(os.environ.get("LOOP_MAX_MIN", "340"))     # stay under 6h job cap
START = time.monotonic()


def now_et():
    return datetime.now(ET)


def in_session(t):
    return t.weekday() < 5 and SESSION_OPEN <= t.time() <= SESSION_END


def sh(*cmd, check=True):
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=check)


def sync():
    """Pull latest (e.g. new commands.json pushed by Dan) before each run."""
    sh("git", "pull", "--rebase", "--quiet", check=False)


def commit_push():
    sh("git", "add", "paper_state.json", "dashboard.html", "trader.log", "commands.json", check=False)
    if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode == 0:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sh("git", "commit", "-q", "-m", f"auto: trade loop run {stamp}")
    for _ in range(3):
        if subprocess.run(["git", "push", "-q"]).returncode == 0:
            return
        sh("git", "pull", "--rebase", "--quiet", check=False)
    print("WARN: push failed after retries", flush=True)


def run_once():
    sync()
    r = subprocess.run([sys.executable, "trader.py"])
    if r.returncode != 0:
        print(f"WARN: trader.py exited {r.returncode}", flush=True)
    commit_push()


def next_slot(t):
    """Next HH:{OFFSET} or HH:{OFFSET+INTERVAL} ET after t."""
    base = t.replace(second=0, microsecond=0)
    for add in range(1, INTERVAL_MIN + 2):
        c = base + timedelta(minutes=add)
        if (c.minute - OFFSET_MIN) % INTERVAL_MIN == 0:
            return c
    return base + timedelta(minutes=INTERVAL_MIN)


def main():
    if not in_session(now_et()):
        print(f"Outside session ({now_et():%a %H:%M ET}); single run.", flush=True)
        run_once()
        return
    while True:
        run_once()
        t = now_et()
        nxt = next_slot(t)
        elapsed_min = (time.monotonic() - START) / 60
        wait_min = (nxt - t).total_seconds() / 60
        if not in_session(nxt):
            print("Session over; exiting.", flush=True)
            return
        if elapsed_min + wait_min + 5 > MAX_MIN:
            print("Job time budget reached; a queued run will continue.", flush=True)
            return
        print(f"Next run {nxt:%H:%M ET} (sleep {wait_min:.1f} min)", flush=True)
        time.sleep(max(0, (nxt - now_et()).total_seconds()))


if __name__ == "__main__":
    main()
