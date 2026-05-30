#!/usr/bin/env python3
"""
update_asm.py — Weekly ASM reminder + self-learning cautionary list
Runs every Monday 8 AM
"""
import json, os, urllib.request, urllib.parse
from datetime import datetime

BASE_DIR         = os.path.expanduser('~/nse-scanner')
CAUTIONARY       = os.path.join(BASE_DIR, 'cautionary_stocks.json')
TELEGRAM_TOKEN   = '8788684553:AAHfZ_q0Hh2mdUNOwELu_PQPePpptKtixGM'
TELEGRAM_CHAT_ID = '-5282064943'
LOG_FILE         = os.path.join(BASE_DIR, 'logs/update_asm.log')

def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

def send_telegram(msg):
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({'chat_id': TELEGRAM_CHAT_ID, 'text': msg}).encode()
        req  = urllib.request.Request(url, data=data, method='POST')
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get('ok', False)
    except Exception as e:
        log(f"Telegram error: {e}")
        return False

def update_asm():
    log("=== Weekly ASM Reminder ===")
    try:
        with open(CAUTIONARY) as f:
            data = json.load(f)
        stocks = data.get('stocks', [])
    except:
        stocks = []

    log(f"Current blocked stocks: {len(stocks)}")
    send_telegram(
        f"📋 WEEKLY ASM REMINDER\n"
        f"Currently blocked: {len(stocks)} stocks\n"
        f"{', '.join(stocks[:10])}\n"
        f"\n"
        f"New stocks auto-added when Angel One rejects (AB4036)\n"
        f"Check NSE manually if needed:\n"
        f"nseindia.com → Market Data → Surveillance"
    )
    log("=== ASM Reminder Done ===")

if __name__ == '__main__':
    update_asm()
