#!/usr/bin/env python3
# scheduler.py — NSE Scanner + Angel One Auto Trader

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

def get_ist():
    return datetime.now()

def is_holiday():
    """Check if today is NSE holiday or weekend."""
    import json
    today = get_ist().strftime('%Y-%m-%d')
    try:
        hf = os.path.join(BASE_DIR, 'nse_holidays.json')
        with open(hf) as f:
            data = json.load(f)
        return today in data.get('all_closed_days', [])
    except:
        return get_ist().weekday() >= 5

def is_market_hours():
    ist = get_ist()
    if is_holiday():
        return False
    mins = ist.hour * 60 + ist.minute
    return (9 * 60 + 15) <= mins <= (15 * 60 + 30)

def is_weekday():
    return not is_holiday()

def run_script(script, args=None, timeout=600):
    cmd = [VENV_PYTHON, os.path.join(BASE_DIR, script)]
    if args:
        cmd.extend(args)
    try:
        result = subprocess.run(cmd, capture_output=True,
                                text=True, timeout=timeout)
        if result.stdout:
            log(result.stdout[-300:])
        if result.returncode != 0 and result.stderr:
            log(f"Error: {result.stderr[-200:]}")
    except subprocess.TimeoutExpired:
        log(f"TIMEOUT: {script}")
    except Exception as e:
        log(f"Error running {script}: {e}")

# ── Angel One Auth ────────────────────────────────────────────
def run_asm_update():
    """Every Monday 8:00 AM — update ASM cautionary list."""
    from datetime import date
    if date.today().weekday() != 0:  # 0 = Monday
        return
    log("=== ASM Update ===")
    run_script('update_asm.py', timeout=60)
    log("=== ASM Update Done ===")

def run_angel_auth():
    """8:30 AM — Login to Angel One, save token for day's trading."""
    if not is_weekday():
        return
    log("=== Angel One Login ===")
    run_script('angel_auth.py', timeout=60)
    log("=== Angel One Login done ===")

def run_position_check():
    """Every hour — check if targets hit, manage positions."""
    if not is_market_hours():
        return
    log("--- Position check ---")
    run_script('angel_trader.py', ['--positions'], timeout=60)
    log("--- Position check done ---")

def run_daily_summary():
    """3:30 PM — Send daily P&L summary via Telegram."""
    log("=== Daily Summary ===")
    run_script('angel_trader.py', ['--summary'], timeout=60)
    log("=== Daily Summary Done ===")

def run_eod_trader():
    """3:15 PM — EOD summary, cancel unfilled orders."""
    if not is_weekday():
        return
    log("=== EOD Trader check ===")
    run_script('angel_trader.py', ['--eod'], timeout=60)
    log("=== EOD Trader done ===")

# ── Volume Scanner ────────────────────────────────────────────
def run_db_update():
    if not is_market_hours():
        return
    log("=== 15min DB update ===")
    run_script('db_manager.py', ['--update'])
    log("=== DB update done ===")



def run_eod():
    log("=== EOD DB update ===")
    run_script('db_manager.py', ['--update'])
    log("=== EOD DB update done ===")

# ── Camarilla Scanner ─────────────────────────────────────────
def run_camarilla_morning():
    if not is_weekday():
        return
    log("=== Camarilla morning ===")
    run_script('camarilla_scanner.py', timeout=600)
    log("=== Camarilla morning done ===")

def run_camarilla_eod():
    if not is_weekday():
        return
    log("=== Camarilla EOD ===")
    run_script('camarilla_scanner.py', timeout=600)
    log("=== Camarilla EOD done ===")



# ── Index Options ─────────────────────────────────────────────
def run_index_options_morning():
    if not is_weekday():
        return
    log("=== Index options morning ===")
    run_script('index_options_scanner.py', ['--mode', 'morning'], timeout=120)
    log("=== Index options morning done ===")

def run_index_options_monitor():
    if not is_market_hours():
        return
    log("--- Index options monitor ---")
    run_script('index_options_scanner.py', ['--mode', 'monitor'], timeout=60)
    log("--- Index options monitor done ---")

# ── HV Signal Scanner ─────────────────────────────────────────
def run_hv_signal_morning():
    # DISABLED — using EOD scan only (4:15 PM)
    log("HV signal morning scan disabled — using EOD scan at 4:15 PM")
    pass
    if not is_weekday():
        return
    log("=== HV Signal morning ===")
    run_script('hv_signal_scanner.py', timeout=120)
    log("=== HV Signal morning done ===")

def run_hv_signal_eod():
    if not is_weekday():
        return
    log("=== HV Signal EOD ===")
    run_script('hv_signal_scanner.py', timeout=120)
    log("=== HV Signal EOD done ===")

# ── Schedule ──────────────────────────────────────────────────

# Angel One Auth
schedule.every().day.at("08:00").do(run_asm_update)        # 8:00 AM IST Monday
schedule.every().day.at("08:30").do(run_angel_auth)       # 8:30 AM IST
schedule.every(15).minutes.do(run_position_check)          # check positions every 15 min
schedule.every().day.at("15:15").do(run_eod_trader)        # 3:15 PM IST
schedule.every().day.at("15:30").do(run_daily_summary)     # 3:30 PM IST

# Volume Scanner
schedule.every(15).minutes.do(run_db_update)              # every 15 min during market
schedule.every().day.at("16:00").do(run_eod)               # 4:00 PM IST


# Camarilla
schedule.every().day.at("09:30").do(run_camarilla_morning) # 9:30 AM IST
schedule.every().day.at("16:15").do(run_camarilla_eod)     # 4:15 PM IST


# Index Options
schedule.every().day.at("09:30").do(run_index_options_morning)
schedule.every(15).minutes.do(run_index_options_monitor)

# HV Signal Scanner
# HV signal morning scan REMOVED — using EOD scan only (4:15 PM)
schedule.every().day.at("16:15").do(run_hv_signal_eod)     # 4:15 PM IST

# ── Startup ───────────────────────────────────────────────────
log("=" * 58)
log("  NSE Scanner + Angel One Auto Trader")
log("=" * 58)
log("ANGEL ONE   : Login 8:30AM | Positions hourly | EOD 3:15PM")
log("HV SCANNER  : 4:15PM EOD only (complete candle — no hindsight!)")
log("CAMARILLA   : 9:30AM + 4:15PM + 15min monitor")
log("INDEX OPT   : 9:30AM + 15min monitor")
log("VOLUME      : DB updates every 15min")
log("=" * 58)

log("Startup: Angel One login...")
run_angel_auth()

log("Startup: DB update...")
run_db_update()

log("Startup: Morning scans...")
run_camarilla_morning()
run_index_options_morning()
# run_hv_signal_morning() — REMOVED (using EOD scan only)

while True:
    schedule.run_pending()
    time.sleep(60)
