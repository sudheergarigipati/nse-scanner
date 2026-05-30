#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  camarilla_monitor.py
#  Runs every 15 min during market hours (9:15 AM - 3:30 PM IST)
#  Checks WATCHING stocks in camarilla_watchlist against live price
#  Fires Telegram alert when L3 bounce (bull) or H3 fade (bear)
#  confirmed with candle + volume check
#
#  Entry logic (from Pivot Boss book):
#    BULLISH : live low <= L3 AND live close > L3 AND green candle
#    BEARISH : live high >= H3 AND live close < H3 AND red candle
#
#  One alert per symbol per direction per day (no duplicates)
# ═══════════════════════════════════════════════════════════════

import yfinance as yf
import sqlite3
import os
import json
import urllib.request
import urllib.parse
import concurrent.futures
from datetime import datetime, date, timedelta

# ══════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════
TELEGRAM_TOKEN   = '8788684553:AAHfZ_q0Hh2mdUNOwELu_PQPePpptKtixGM'
TELEGRAM_CHAT_ID = '-5282064943'

BASE_DIR      = os.path.expanduser('~/nse-scanner')
DB_PATH       = os.path.join(BASE_DIR, 'nse_data.db')
LOG_FILE      = os.path.join(BASE_DIR, 'logs/camarilla_monitor.log')
ALERTS_FILE   = os.path.join(BASE_DIR, 'camarilla_alerts_today.json')

MAX_WORKERS   = 15    # parallel yfinance fetches

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

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ══════════════════════════════════════════════════════════════
#  ALERT DEDUP — one alert per symbol+direction per day
#  Same pattern as ema_monitor.py (JSON file)
# ══════════════════════════════════════════════════════════════
def load_alerts_sent():
    """Load today's already-sent alerts as a set of 'SYMBOL_DIRECTION'."""
    today = str(date.today())
    if os.path.exists(ALERTS_FILE):
        try:
            with open(ALERTS_FILE) as f:
                data = json.load(f)
            if data.get('date') == today:
                return set(data.get('alerts', []))
        except Exception:
            pass
    return set()

def save_alert_sent(symbol, direction):
    """Mark symbol+direction as alerted today."""
    alerts = load_alerts_sent()
    alerts.add(f"{symbol}_{direction}")
    with open(ALERTS_FILE, 'w') as f:
        json.dump({
            'date':   str(date.today()),
            'alerts': list(alerts)
        }, f)

def already_alerted(symbol, direction, alerts_sent):
    return f"{symbol}_{direction}" in alerts_sent

# ══════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════
def send_telegram(message):
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            'chat_id': TELEGRAM_CHAT_ID,
            'text':    message,
        }).encode()
        req = urllib.request.Request(url, data=data, method='POST')
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            if result.get('ok'):
                log("  ✅ Telegram sent!")
                return True
            else:
                log(f"  ❌ Telegram failed: {result}")
    except Exception as e:
        log(f"  ❌ Telegram failed: {e}")
    return False


def build_trigger_message(sym, direction, trigger_type,
                           live, levels, avg_vol):
    ist_time  = get_ist().strftime('%d %b %Y %H:%M IST')

    if direction == 'BULLISH':
        entry  = levels['l3']
        sl     = levels['l4']
        t1     = levels['h3']
        t2     = levels['h4']
        t3     = levels['h5']
        emoji  = 'BUY'
        setup  = 'L3 Bounce'
        trend  = 'UPTREND'
    else:
        entry  = levels['h3']
        sl     = levels['h4']
        t1     = levels['l3']
        t2     = levels['l4']
        t3     = levels['l5']
        emoji  = 'SELL/SHORT'
        setup  = 'H3 Fade'
        trend  = 'DOWNTREND'

    risk      = abs(entry - sl)
    reward    = abs(t1 - entry)
    rr        = round(reward / risk, 1) if risk > 0 else 0
    sl_pct    = round(risk / entry * 100, 1)
    t1_pct    = round(reward / entry * 100, 1)
    vol_ratio = round(live['volume'] / avg_vol, 1) if avg_vol > 0 else 0
    vol_str   = f"{vol_ratio}x avg"

    msg = (
        f"CAMARILLA {emoji} -- {sym}\n"
        f"{ist_time}\n"
        f"\n"
        f"Entry    : {round(entry, 2)}\n"
        f"Stop Loss: {round(sl, 2)}  ({sl_pct}% risk)\n"
        f"Target 1 : {round(t1, 2)}  (+{t1_pct}%)\n"
        f"Target 2 : {round(t2, 2)}\n"
        f"Target 3 : {round(t3, 2)}  (extended)\n"
        f"R:R Ratio: 1:{rr}\n"
        f"\n"
        f"Setup  : {setup} (Weekly Camarilla)\n"
        f"Trend  : {trend}\n"
        f"Volume : {vol_str}\n"
        f"LTP    : {live['close']}\n"
        f"\n"
        f"Chart: https://www.tradingview.com/chart/?symbol=NSE:{sym}"
    )

    return msg, round(entry, 2), round(sl, 2), round(t1, 2), round(t2, 2), rr

# ══════════════════════════════════════════════════════════════
#  FETCH LIVE PRICE — same pattern as ema_monitor.py
# ══════════════════════════════════════════════════════════════
def fetch_live(symbol):
    """
    Fetch today's OHLCV via yfinance (15-min delayed).
    Uses period='1d' interval='1d' for EOD-style data
    which is sufficient for swing trade entry confirmation.
    """
    try:
        ticker = yf.Ticker(f"{symbol}.NS")
        hist   = ticker.history(period='2d', interval='1d')
        if hist is None or hist.empty:
            return None
        last = hist.iloc[-1]
        return {
            'open':   round(float(last['Open']),   2),
            'high':   round(float(last['High']),   2),
            'low':    round(float(last['Low']),    2),
            'close':  round(float(last['Close']),  2),
            'volume': int(last['Volume']),
            'date':   str(hist.index[-1].date()),
        }
    except Exception as e:
        log(f"  fetch_live {symbol}: {e}")
        return None

# ══════════════════════════════════════════════════════════════
#  GET AVERAGE VOLUME — from daily_prices table (no API needed)
# ══════════════════════════════════════════════════════════════
def get_avg_volumes(conn, symbols):
    """
    Compute 20-day average volume for each symbol
    directly from the daily_prices table.
    Much faster than fetching from yfinance.
    """
    if not symbols:
        return {}
    placeholders = ','.join('?' * len(symbols))
    c = conn.cursor()
    c.execute(f'''
        SELECT symbol, AVG(volume) as avg_vol
        FROM (
            SELECT symbol, volume
            FROM   daily_prices
            WHERE  symbol IN ({placeholders})
            AND    date < date('now')
            ORDER  BY date DESC
            LIMIT  20
        )
        GROUP BY symbol
    ''', symbols)
    # Note: LIMIT inside subquery applies per-symbol via window in SQLite
    # Better approach for per-symbol avg:
    avg = {}
    for sym in symbols:
        c.execute('''
            SELECT AVG(volume) as avg_vol
            FROM (
                SELECT volume FROM daily_prices
                WHERE  symbol = ? AND date < date('now')
                ORDER  BY date DESC LIMIT 20
            )
        ''', (sym,))
        row = c.fetchone()
        avg[sym] = float(row['avg_vol']) if row and row['avg_vol'] else 0
    return avg

# ══════════════════════════════════════════════════════════════
#  GET WATCHLIST — only WATCHING stocks for this week
# ══════════════════════════════════════════════════════════════
def get_watchlist(conn):
    """
    Return all WATCHING stocks for the current week.
    Returns list of dicts with symbol, direction and levels.
    """
    today      = date.today()
    week_start = (today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')
    c          = conn.cursor()

    # Add ema20w column if missing
    try:
        c.execute('ALTER TABLE camarilla_watchlist ADD COLUMN ema20w REAL DEFAULT 0')
        conn.commit()
        log("Added missing ema20w column")
    except Exception:
        pass  # column already exists

    c.execute('''
        SELECT symbol, direction,
               h3, h4, h5, l3, l4, l5,
               prev_close,
               COALESCE(ema20w, 0) as ema20w
        FROM   camarilla_watchlist
        WHERE  week_start = ? AND status = 'WATCHING'
        ORDER  BY symbol
    ''', (week_start,))

    rows = c.fetchall()
    log(f"Watchlist: {len(rows)} WATCHING stocks this week ({week_start})")
    return [dict(r) for r in rows]

# ══════════════════════════════════════════════════════════════
#  CHECK ENTRY CONDITIONS (Pivot Boss book logic)
# ══════════════════════════════════════════════════════════════
def check_entry(stock, live):
    """
    BULLISH trigger:
      - Today's low touched L3 (low <= L3)
      - Price recovered above L3 (close > L3)
      - Green candle (close > open) — buyers in control
      → L3_BOUNCE

    BEARISH trigger:
      - Today's high touched H3 (high >= H3)
      - Price faded back below H3 (close < H3)
      - Red candle (close < open) — sellers in control
      → H3_FADE

    Returns trigger_type string or None.
    """
    direction = stock['direction']
    l3 = stock['l3']
    h3 = stock['h3']

    if direction == 'BULLISH':
        touched_l3  = live['low']   <= l3
        above_l3    = live['close'] >  l3
        green_candle = live['close'] >  live['open']
        if touched_l3 and above_l3 and green_candle:
            return 'L3_BOUNCE'

    elif direction == 'BEARISH':
        touched_h3  = live['high']  >= h3
        below_h3    = live['close'] <  h3
        red_candle   = live['close'] <  live['open']
        if touched_h3 and below_h3 and red_candle:
            return 'H3_FADE'

    return None

# ══════════════════════════════════════════════════════════════
#  SAVE TRIGGER TO DB
# ══════════════════════════════════════════════════════════════
def save_trigger(conn, sym, direction, trigger_type,
                 entry, sl, t1, t2, rr):
    """Save fired entry signal to camarilla_triggers table."""
    ist  = get_ist()
    c    = conn.cursor()
    c.execute('''
        INSERT INTO camarilla_triggers
            (symbol, trigger_date, trigger_time, direction,
             trigger_type, entry_price, stop_loss,
             target1, target2, risk_reward)
        VALUES (?,?,?,?,?, ?,?,?,?,?)
    ''', (
        sym,
        ist.strftime('%Y-%m-%d'),
        ist.strftime('%H:%M:%S'),
        direction,
        trigger_type,
        entry, sl, t1, t2, rr
    ))

    # Mark watchlist entry as TRIGGERED
    week_start = (date.today() - timedelta(days=date.today().weekday())).strftime('%Y-%m-%d')
    c.execute('''
        UPDATE camarilla_watchlist
        SET    status = 'TRIGGERED', last_checked = ?
        WHERE  symbol = ? AND week_start = ? AND direction = ?
    ''', (ist.strftime('%Y-%m-%d %H:%M:%S'), sym, week_start, direction))

    conn.commit()

# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════
def main():
    log("=" * 55)
    log(f"Camarilla Monitor — {get_ist().strftime('%d %b %Y %H:%M IST')}")
    log("=" * 55)

    # Gate: only run during market hours
    if not is_market_hours():
        log("Market closed — skipping")
        return

    conn         = get_db()
    watchlist    = get_watchlist(conn)

    if not watchlist:
        log("Watchlist is empty — nothing to monitor")
        conn.close()
        return

    alerts_sent  = load_alerts_sent()
    log(f"Already alerted today: {alerts_sent}")

    symbols      = [s['symbol'] for s in watchlist]

    # Get 20-day avg volumes from DB (fast, no API call)
    log(f"Loading avg volumes for {len(symbols)} symbols...")
    avg_volumes  = get_avg_volumes(conn, symbols)

    # Fetch live prices in parallel (same as ema_monitor.py)
    log(f"Fetching live prices for {len(symbols)} symbols...")
    live_prices  = {}

    def fetch_and_store(sym):
        data = fetch_live(sym)
        if data:
            live_prices[sym] = data

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_and_store, s): s for s in symbols}
        concurrent.futures.wait(futures)

    log(f"Live prices fetched: {len(live_prices)}/{len(symbols)}")

    # Check entry conditions
    new_triggers = 0

    for stock in watchlist:
        sym       = stock['symbol']
        direction = stock['direction']

        # Skip if already alerted today
        if already_alerted(sym, direction, alerts_sent):
            log(f"  {sym:15} {direction:8} — already alerted today, skip")
            continue

        live = live_prices.get(sym)
        if not live:
            log(f"  {sym:15} — no live price, skip")
            continue

        # Log current status vs levels
        if direction == 'BULLISH':
            log(f"  {sym:15} BULL | price:{live['close']:8.2f} "
                f"low:{live['low']:8.2f} L3:{stock['l3']:8.2f} "
                f"L4:{stock['l4']:8.2f}")
        else:
            log(f"  {sym:15} BEAR | price:{live['close']:8.2f} "
                f"high:{live['high']:8.2f} H3:{stock['h3']:8.2f} "
                f"H4:{stock['h4']:8.2f}")

        # Check entry condition
        trigger_type = check_entry(stock, live)
        if not trigger_type:
            continue

        # ── Final trend safety check ─────────────────────────
        # Double check 20w EMA trend before firing alert
        # Prevents false alerts if weekly trend changed since scan
        ema20w = stock.get('ema20w', 0)
        if ema20w and ema20w > 0:
            if direction == 'BULLISH' and live['close'] < ema20w:
                log(f"  {sym} BULLISH blocked — price {live['close']} < 20w EMA {ema20w}")
                continue
            if direction == 'BEARISH' and live['close'] > ema20w:
                log(f"  {sym} BEARISH blocked — price {live['close']} > 20w EMA {ema20w}")
                continue

        # ── Entry condition met! ─────────────────────────────
        log(f"  🚨 TRIGGER: {sym} {direction} {trigger_type}")

        avg_vol = avg_volumes.get(sym, 0)

        # Build Telegram message
        msg, entry, sl, t1, t2, rr = build_trigger_message(
            sym, direction, trigger_type, live, stock, avg_vol
        )

        # Send Telegram
        if send_telegram(msg):
            # Save trigger to DB
            save_trigger(conn, sym, direction, trigger_type,
                         entry, sl, t1, t2, rr)
            # Mark as alerted so no duplicate today
            save_alert_sent(sym, direction)
            new_triggers += 1
            log(f"  ✅ {sym} {direction} trigger saved and alerted")
        else:
            log(f"  ❌ {sym} Telegram failed — will retry next run")

    log(f"Monitor complete — {new_triggers} new triggers this run")
    conn.close()


if __name__ == '__main__':
    main()
