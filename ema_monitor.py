#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  ema_monitor.py
#  Runs every 15 min during market hours (9:15 AM - 3:30 PM IST)
#  Checks 15-min EMA alignment for Bullish Scanner stocks
#  Sends Telegram alert immediately when condition triggers
#
#  Entry condition (all must be true):
#    1. Stock in HV watchlist
#    2. Price between HV Low and HV High
#    3. Close > EMA21 > EMA50 > EMA100 > EMA200 (aligned)
#    4. Green candle (close > open) — confirms buying pressure
#    5. Not already alerted today (no duplicates)
#
#  Fixes applied:
#    - Telegram plain text (no HTML) — no more 400 errors
#    - Green candle confirmation added
#    - Alert dedup works correctly
# ═══════════════════════════════════════════════════════════════

import yfinance as yf
import pandas as pd
import sqlite3
import json
import os
import urllib.request
import urllib.parse
from datetime import datetime, date, timedelta
import concurrent.futures

# ══════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════
TELEGRAM_TOKEN   = '8788684553:AAHfZ_q0Hh2mdUNOwELu_PQPePpptKtixGM'
TELEGRAM_CHAT_ID = '-5282064943'

BASE_DIR      = os.path.expanduser('~/nse-scanner')
DB_PATH       = os.path.join(BASE_DIR, 'nse_data.db')
ALERTS_FILE   = os.path.join(BASE_DIR, 'ema_alerts_today.json')
LOG_FILE      = os.path.join(BASE_DIR, 'logs/ema_monitor.log')

DAYS_LOOKBACK = 180
BULL_BUFFER   = 15.0
MIN_SCORE     = 50

# ══════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════
def log(msg):
    ts   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

def get_ist():
    return datetime.now()

def is_market_hours():
    ist = get_ist()
    if ist.weekday() >= 5:
        return False
    mins = ist.hour * 60 + ist.minute
    return (9 * 60 + 15) <= mins <= (15 * 60 + 30)

def load_alerts_sent():
    today = str(date.today())
    if os.path.exists(ALERTS_FILE):
        try:
            with open(ALERTS_FILE) as f:
                data = json.load(f)
            if data.get('date') == today:
                return set(data.get('symbols', []))
        except:
            pass
    return set()

def save_alert_sent(symbol):
    alerts = load_alerts_sent()
    alerts.add(symbol)
    with open(ALERTS_FILE, 'w') as f:
        json.dump({'date': str(date.today()), 'symbols': list(alerts)}, f)

# ══════════════════════════════════════════════════════════════
#  TELEGRAM — plain text, no HTML, no special chars
# ══════════════════════════════════════════════════════════════
def send_telegram(message):
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            'chat_id': TELEGRAM_CHAT_ID,
            'text':    message,
        }).encode()
        req  = urllib.request.Request(url, data=data, method='POST')
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            if result.get('ok'):
                log("  Telegram sent!")
                return True
            else:
                log(f"  Telegram failed: {result}")
    except Exception as e:
        log(f"  Telegram failed: {e}")
    return False

def build_telegram_message(stock, ema):
    ist_time   = get_ist().strftime('%d %b %Y %H:%M IST')
    upside     = round((stock['hv_high'] - ema['close']) / ema['close'] * 100, 1)
    candle_str = "GREEN (buying pressure confirmed)" if ema.get('green_candle') else "confirmed"

    msg = (
        f"ENTRY SIGNAL: {stock['symbol']}\n"
        f"{ist_time}\n"
        f"\n"
        f"15-Min EMA Alignment - ALL 4 ALIGNED\n"
        f"Close   : Rs {ema['close']}\n"
        f"EMA 21  : Rs {ema['ema21']}\n"
        f"EMA 50  : Rs {ema['ema50']}\n"
        f"EMA 100 : Rs {ema['ema100']}\n"
        f"EMA 200 : Rs {ema['ema200']}\n"
        f"Candle  : {candle_str}\n"
        f"\n"
        f"Volume Setup\n"
        f"Score     : {stock['score']}/100 ({stock['grade']})\n"
        f"HV Date   : {stock['hv_date']} ({stock['days_since']}d ago)\n"
        f"Support   : Rs {stock['hv_low']}\n"
        f"Target    : Rs {stock['hv_high']} (+{upside}%)\n"
        f"Stop Loss : Rs {stock['stop_loss']}\n"
        f"Above Sup : {stock['pct_above']}%\n"
        f"\n"
        f"Chart: https://www.tradingview.com/chart/?symbol=NSE:{stock['symbol']}"
    )
    return msg

# ══════════════════════════════════════════════════════════════
#  GET BULLISH STOCKS FROM DB
# ══════════════════════════════════════════════════════════════
def get_bull_stocks():
    if not os.path.exists(DB_PATH):
        log("DB not found")
        return []

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c    = conn.cursor()
    cutoff = (date.today() - timedelta(days=DAYS_LOOKBACK)).strftime('%Y-%m-%d')

    c.execute('''SELECT symbol, hv_date, hv_volume, hv_high, hv_low, hv_close,
                        latest_close
                 FROM hv_summary
                 WHERE hv_date >= ? AND hv_volume > 0 AND latest_close > 0''',
              (cutoff,))
    rows = c.fetchall()
    conn.close()

    stocks = []
    now    = datetime.now()

    for r in rows:
        try:
            hv_date    = datetime.strptime(r['hv_date'], '%Y-%m-%d')
            days_since = (now - hv_date).days
            price      = r['latest_close']
            hv_low     = r['hv_low']
            hv_high    = r['hv_high']
            hv_close   = r['hv_close']
            hv_vol     = r['hv_volume']

            if hv_low <= 0:
                continue
            pct_above = (price - hv_low) / hv_low * 100
            if pct_above < -1 or pct_above > BULL_BUFFER:
                continue

            # Score
            sc = 0
            hr = hv_high - hv_low
            cp = (hv_close - hv_low) / hr if hr > 0 else 0.5
            if cp >= 0.5:          sc += 15
            if price >= hv_low:    sc += 20
            if pct_above <= 3:     sc += 20
            elif pct_above <= 7:   sc += 12
            elif pct_above <= 12:  sc += 6
            if days_since <= 7:    sc += 20
            elif days_since <= 30: sc += 14
            elif days_since <= 90: sc += 8
            else:                  sc += 3
            if hv_vol > 100_000_000:  sc += 10
            elif hv_vol > 50_000_000: sc += 7
            elif hv_vol > 10_000_000: sc += 4
            rng = hr / hv_low * 100
            if rng > 10:  sc += 10
            elif rng > 5: sc += 5
            sc = min(sc, 100)
            if sc < MIN_SCORE:
                continue

            stocks.append({
                'symbol':     r['symbol'],
                'score':      sc,
                'grade':      'BUY' if sc >= 65 and price >= hv_low * 0.99 else 'WATCH',
                'hv_date':    r['hv_date'],
                'days_since': days_since,
                'hv_low':     hv_low,
                'hv_high':    hv_high,
                'price':      price,
                'pct_above':  round(pct_above, 2),
                'stop_loss':  round(hv_low * 0.98, 2),
            })
        except:
            continue

    stocks.sort(key=lambda x: x['score'], reverse=True)
    log(f"Bull stocks: {len(stocks)}")
    return stocks

# ══════════════════════════════════════════════════════════════
#  EMA CHECK — with green candle confirmation
# ══════════════════════════════════════════════════════════════
def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def check_ema(symbol):
    try:
        ticker = yf.Ticker(f"{symbol}.NS")
        df     = ticker.history(period='15d', interval='15m')
        if df is None or df.empty or len(df) < 200:
            return None

        df = df.sort_index()
        df['ema21']  = calc_ema(df['Close'], 21)
        df['ema50']  = calc_ema(df['Close'], 50)
        df['ema100'] = calc_ema(df['Close'], 100)
        df['ema200'] = calc_ema(df['Close'], 200)

        last   = df.iloc[-1]
        close  = round(float(last['Close']),  2)
        open_  = round(float(last['Open']),   2)
        ema21  = round(float(last['ema21']),  2)
        ema50  = round(float(last['ema50']),  2)
        ema100 = round(float(last['ema100']), 2)
        ema200 = round(float(last['ema200']), 2)

        above_count  = sum([close > ema21, close > ema50,
                            close > ema100, close > ema200])
        ema_aligned  = ema21 > ema50 > ema100 > ema200
        green_candle = close > open_   # NEW: buying pressure confirmation

        # Entry signal requires:
        # 1. All 4 EMAs aligned bullishly
        # 2. Close above all EMAs
        # 3. Green candle (close > open)
        entry_signal = (
            close > ema21
            and close > ema50
            and close > ema100
            and close > ema200
            and ema_aligned
            and green_candle    # NEW filter
        )

        return {
            'symbol':       symbol,
            'close':        close,
            'open':         open_,
            'ema21':        ema21,
            'ema50':        ema50,
            'ema100':       ema100,
            'ema200':       ema200,
            'above_count':  above_count,
            'ema_aligned':  ema_aligned,
            'green_candle': green_candle,
            'entry_signal': entry_signal,
            'candle_time':  str(df.index[-1]),
        }
    except Exception as e:
        return None

# ══════════════════════════════════════════════════════════════
#  SAVE EMA STATUS TO DB
# ══════════════════════════════════════════════════════════════
def save_ema_status(results):
    if not os.path.exists(DB_PATH):
        return
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS ema_status (
        symbol TEXT PRIMARY KEY, check_time TEXT,
        close REAL, ema21 REAL, ema50 REAL, ema100 REAL, ema200 REAL,
        above_count INTEGER, entry_signal INTEGER,
        ema_aligned INTEGER, candle_time TEXT)''')
    for r in results:
        if not r:
            continue
        c.execute('''INSERT OR REPLACE INTO ema_status
            (symbol, check_time, close, ema21, ema50, ema100, ema200,
             above_count, entry_signal, ema_aligned, candle_time)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (r['symbol'],
             datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             r['close'], r['ema21'], r['ema50'], r['ema100'], r['ema200'],
             r['above_count'], int(r['entry_signal']),
             int(r['ema_aligned']), r['candle_time']))
    conn.commit()
    conn.close()

# ══════════════════════════════════════════════════════════════
#  GET WATCHLIST FROM DB
# ══════════════════════════════════════════════════════════════
def get_watchlist_stocks():
    if not os.path.exists(DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c    = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS watchlist (
            symbol TEXT PRIMARY KEY, hv_low REAL, hv_high REAL,
            hv_date TEXT, score INTEGER, grade TEXT,
            stop_loss REAL, added TEXT)''')
        c.execute('SELECT * FROM watchlist')
        rows = c.fetchall()
        conn.close()
        return {r['symbol']: dict(r) for r in rows}
    except:
        return {}

# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════
def main():
    log("=" * 55)
    log(f"EMA Monitor — {get_ist().strftime('%d %b %Y %H:%M IST')}")
    log("=" * 55)

    if not is_market_hours():
        log("Market closed — skipping")
        return

    # Load watchlist stocks — these are the ones to monitor
    wl_stocks = get_watchlist_stocks()
    if not wl_stocks:
        log("Watchlist is empty — nothing to monitor")
        return

    # Also load bull_stocks for score/grade info (optional enrichment)
    bull_stocks = get_bull_stocks()
    stock_map   = {s['symbol']: s for s in bull_stocks}

    alerts_sent  = load_alerts_sent()
    log(f"Already alerted today: {alerts_sent}")
    log(f"Watchlist stocks to check: {list(wl_stocks.keys())}")

    # Check EMAs for ALL watchlist stocks directly
    results    = []
    wl_symbols = list(wl_stocks.keys())

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(check_ema, sym): sym for sym in wl_symbols}
        for future in concurrent.futures.as_completed(futures):
            r = future.result()
            if r:
                results.append(r)
                green = "green" if r.get('green_candle') else "red"
                log(f"  {r['symbol']:15} "
                    f"Close:{r['close']:8.2f} | "
                    f"EMAs:{r['above_count']}/4 | "
                    f"Candle:{green} | "
                    f"Signal:{'YES' if r['entry_signal'] else 'no'}")

    save_ema_status(results)
    log(f"EMA status saved: {len(results)} stocks")

    new_signals = []
    for r in results:
        sym = r['symbol']

        if not r['entry_signal']:
            continue

        if sym in alerts_sent:
            log(f"  {sym}: already alerted today — skip")
            continue

        # Check price in HV range from watchlist
        wl      = wl_stocks[sym]
        hv_low  = float(wl.get('hv_low',  0))
        hv_high = float(wl.get('hv_high', 999999))
        price   = r['close']

        if price < hv_low:
            log(f"  {sym}: price {price} below HV Low {hv_low} — skip")
            continue
        if price > hv_high:
            log(f"  {sym}: price {price} above HV High {hv_high} — skip")
            continue

        log(f"  {sym}: VALID ENTRY — price {price} in range "
            f"{hv_low}-{hv_high} | green candle confirmed")
        new_signals.append(r)

    log(f"New entry signals: {len(new_signals)}")

    for r in new_signals:
        sym = r['symbol']
        # Use bull_stock info if available, else use watchlist info
        if sym in stock_map:
            stock = stock_map[sym]
        else:
            wl    = wl_stocks[sym]
            stock = {
                'symbol':     sym,
                'score':      wl.get('score', 0),
                'grade':      wl.get('grade', 'WATCH'),
                'hv_date':    wl.get('hv_date', ''),
                'days_since': 0,
                'hv_low':     float(wl.get('hv_low', 0)),
                'hv_high':    float(wl.get('hv_high', 0)),
                'price':      r['close'],
                'pct_above':  0,
                'stop_loss':  float(wl.get('stop_loss', 0)),
            }
        msg = build_telegram_message(stock, r)
        log(f"  Sending Telegram for {sym}...")
        if send_telegram(msg):
            save_alert_sent(sym)
            log(f"  {sym} alert saved — will not repeat today")
        else:
            log(f"  {sym} Telegram failed — will retry next run")

    if not new_signals:
        log("No new signals this run")

    log("EMA Monitor complete")


if __name__ == '__main__':
    main()
