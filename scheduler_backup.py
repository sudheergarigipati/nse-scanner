#!/usr/bin/env python3
# scheduler.py — runs DB updates + EMA monitor on schedule

import schedule
import time
import subprocess
import os
from datetime import datetime, timedelta

BASE_DIR    = os.path.expanduser('~/nse-scanner')
VENV_PYTHON = os.path.join(BASE_DIR, 'venv/bin/python3')
LOG_FILE    = os.path.join(BASE_DIR, 'logs/scheduler.log')

def log(msg):
    ts   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

def is_market_hours():
    utc_now  = datetime.utcnow()
    ist_now  = utc_now + timedelta(hours=5, minutes=30)
    if ist_now.weekday() >= 5: return False
    ist_mins = ist_now.hour * 60 + ist_now.minute
    return (9 * 60 + 15) <= ist_mins <= (15 * 60 + 30)

def run_script(script, args=None, timeout=600):
    cmd = [VENV_PYTHON, os.path.join(BASE_DIR, script)]
    if args: cmd.extend(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.stdout: log(result.stdout[-300:])
        if result.returncode != 0 and result.stderr:
            log(f"Error: {result.stderr[-200:]}")
    except subprocess.TimeoutExpired:
        log(f"TIMEOUT: {script}")
    except Exception as e:
        log(f"Error running {script}: {e}")

def run_db_update():
    log("=== Hourly DB update ===")
    run_script('db_manager.py', ['--update'])
    log("=== DB update done ===")

def run_ema_monitor():
    """Run every 15 min during market hours only"""
    if not is_market_hours():
        return
    log("--- EMA monitor check ---")
    run_script('ema_monitor.py', timeout=300)
    log("--- EMA monitor done ---")

def run_eod():
    log("=== End of day update ===")
    run_script('db_manager.py', ['--update'])
    log("=== EOD done ===")

# ── Schedule ──
# DB update every hour
schedule.every().hour.at(":00").do(run_db_update)

# Pre-market update 8 AM IST = 2:30 UTC
schedule.every().day.at("02:30").do(run_db_update)

# End of day 4 PM IST = 10:30 UTC
schedule.every().day.at("10:30").do(run_eod)

# EMA monitor every 15 min
schedule.every(15).minutes.do(run_ema_monitor)

log("Scheduler started")
log("DB update: every hour + 8AM IST + 4PM IST")
log("EMA monitor: every 15 min (market hours only)")

# Run immediately on start
log("Initial DB update...")
run_db_update()

while True:
    schedule.run_pending()
    time.sleep(60)
