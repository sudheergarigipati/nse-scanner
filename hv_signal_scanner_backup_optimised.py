#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  hv_signal_scanner.py
#  Automated HV Buy Signal Scanner
#  Based on backtest: 40.7% win rate | +2.96%/trade | R:R 4:1
#
#  Logic (validated by 2-year backtest on 794 NSE stocks):
#    1. Find 5-year HV day for each stock
#    2. HV day must be 7-90 days ago (sweet spot from backtest)
#    3. Today's candle LOW must touch HV Low
#    4. Today's candle must close ABOVE HV Low (no breakdown)
#    5. Green candle (close > open)
#    6. Volume >= 2x 20-day average
#    7. Stock price >= Rs 20 (no penny stocks)
#    8. Liquidity: avg daily value >= Rs 2 Crore
#
#  Entry  : Limit order at HV Low
#  SL     : 3% below HV Low
#  Target : HV High (avg 12% move)
#  Hold   : Max 15 trading days
#
#  Usage:
#    python3 hv_signal_scanner.py           (scan and alert)
#    python3 hv_signal_scanner.py --test    (test mode, no Telegram)
# ═══════════════════════════════════════════════════════════════

import sqlite3
import os
import json
import argparse
import urllib.request
import urllib.parse
from datetime import datetime, date, timedelta

TELEGRAM_TOKEN   = '8788684553:AAHfZ_q0Hh2mdUNOwELu_PQPePpptKtixGM'
TELEGRAM_CHAT_ID = '-5282064943'

BASE_DIR     = os.path.expanduser('~/nse-scanner')
DB_PATH      = os.path.join(BASE_DIR, 'nse_data.db')
LOG_FILE     = os.path.join(BASE_DIR, 'logs/hv_signal.log')
ALERTS_FILE  = os.path.join(BASE_DIR, 'hv_signal_alerts_today.json')

# ── Known cautionary/surveillance stocks — skip these ────────
# These stocks are blocked by Angel One (error AB4036)
# Updated manually when new stocks get blocked
import json as _json
def load_skip_symbols():
    try:
        with open(os.path.join(BASE_DIR, 'cautionary_stocks.json')) as f:
            return set(_json.load(f).get('stocks', []))
    except:
        return set()
SKIP_SYMBOLS = load_skip_symbols()

# ── Backtest-validated parameters ─────────────────────────────
MIN_HV_AGE_DAYS  = 7     # HV must be at least 7 days old
MAX_HV_AGE_DAYS  = 60    # HV must be within 60 days (optimised)
MIN_VOL_RATIO    = 1.5   # volume >= 1.5x average (optimised)
MIN_PRICE        = 20.0  # no penny stocks
MIN_VALUE_CR     = 2.0   # min avg daily value Rs 2 crore
SL_PCT           = 3.0   # SL = L4 (Camarilla) — avg loss only -0.91%
MAX_HOLD_DAYS    = 15    # exit after 15 days if target not hit
TOUCH_TOLERANCE  = 0.5   # price within 0.5% of HV Low counts as touch
L3_VS_HV_MAX     = 4.0   # Daily L3 must be within 4% of HV Low
MIN_RR           = 5.0   # minimum R:R ratio
MAX_RR           = 20.0  # maximum R:R ratio (optimised)
MAX_HV_VOL_RATIO = 10.0  # max HV day strength (optimised)
MIN_UPSIDE_PCT   = 8.0   # minimum upside % (HV High vs HV Low)

# ── Helpers ───────────────────────────────────────────────────
def log(msg):
    ts   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

def get_ist():
    return datetime.now()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def calc_camarilla(prev_high, prev_low, prev_close):
    rng = prev_high - prev_low
    h3  = prev_close + rng * 0.55 / 2
    h4  = prev_close + rng * 1.1  / 2
    l3  = prev_close - rng * 0.55 / 2
    l4  = prev_close - rng * 1.1  / 2
    return round(h3,2), round(h4,2), round(l3,2), round(l4,2)

def send_telegram(message, test_mode=False):
    if test_mode:
        log(f"[TEST MODE] Would send:\n{message}")
        return True
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
        log(f"  Telegram error: {e}")
    return False

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
        json.dump({'date': str(date.today()),
                   'symbols': list(alerts)}, f)

# ═══════════════════════════════════════════════════════════════
#  MAIN SCAN
# ═══════════════════════════════════════════════════════════════
def scan(test_mode=False):
    log("=" * 55)
    log(f"HV Signal Scanner — {get_ist().strftime('%d %b %Y %H:%M IST')}")
    log("=" * 55)

    conn        = get_db()
    c           = conn.cursor()
    today       = date.today()
    today_str   = today.strftime('%Y-%m-%d')
    alerts_sent = load_alerts_sent()
    signals     = []

    # Get all stocks with HV summary
    five_yr_ago = (today - timedelta(days=5*365)).strftime('%Y-%m-%d')

    c.execute('''
        SELECT h.symbol, h.hv_date, h.hv_volume,
               h.hv_high, h.hv_low, h.hv_close,
               h.latest_close, h.latest_date
        FROM   hv_summary h
        WHERE  h.hv_date >= ?
        AND    h.hv_date <= ?
        AND    h.hv_volume > 0
        AND    h.hv_low > 0
        AND    h.latest_close >= ?
        ORDER  BY h.hv_date DESC
    ''', (
        (today - timedelta(days=MAX_HV_AGE_DAYS)).strftime('%Y-%m-%d'),
        (today - timedelta(days=MIN_HV_AGE_DAYS)).strftime('%Y-%m-%d'),
        MIN_PRICE
    ))
    candidates = c.fetchall()
    log(f"Candidates (HV {MIN_HV_AGE_DAYS}-{MAX_HV_AGE_DAYS} days ago): {len(candidates)}")
    # Filter out stocks with less than 252 days (1 year) of history
    filtered = []
    for row in candidates:
        c.execute('SELECT COUNT(*) as cnt FROM daily_prices WHERE symbol=?', (row['symbol'],))
        days = c.fetchone()['cnt']
        if days >= 252:
            filtered.append(row)
        else:
            log(f"  SKIP {row['symbol']} - only {days} days history (need 252)")
    candidates = filtered
    log(f"After min-history filter: {len(candidates)} candidates")

    for row in candidates:
        sym      = row['symbol']
        hv_date  = row['hv_date']
        hv_high  = row['hv_high']
        hv_low   = row['hv_low']
        hv_vol   = row['hv_volume']
        hv_close = row['hv_close']

        # Skip if already alerted today
        if sym in SKIP_SYMBOLS:
            continue
        if sym in alerts_sent:
            continue

        # Skip if already in open positions
        try:
            with open(os.path.join(BASE_DIR, 'angel_trades.json')) as f:
                td = json.load(f)
            open_syms = [t['symbol'] for t in td.get('trades', [])
                        if t.get('status') in ['OPEN', 'GTT_PENDING']]
            if sym in open_syms:
                continue
        except:
            pass

        # ── Filter 0: HV candle must be BULLISH ───────────
        # Close must be in upper 50% of HV candle range
        # If close is near LOW = institutions were SELLING on HV day
        # If close is near HIGH = institutions were BUYING on HV day
        hv_range = hv_high - hv_low
        if hv_range <= 0:
            continue
        candle_position = (hv_close - hv_low) / hv_range
        if candle_position < 0.5:
            log(f"  {sym}: HV candle bearish (pos={candle_position:.2f}) — skip")
            continue

        # ── Get today's candle ─────────────────────────────
        c.execute('''
            SELECT date, open, high, low, close, volume
            FROM   daily_prices
            WHERE  symbol = ? AND date = ?
        ''', (sym, today_str))
        today_bar = c.fetchone()

        if not today_bar:
            # Try yesterday (for EOD scan)
            yesterday = (today - timedelta(days=1)).strftime('%Y-%m-%d')
            c.execute('''
                SELECT date, open, high, low, close, volume
                FROM   daily_prices
                WHERE  symbol = ? AND date = ?
            ''', (sym, yesterday))
            today_bar = c.fetchone()

        if not today_bar:
            continue

        bar_date = today_bar['date']
        bar_open = today_bar['open']
        bar_high = today_bar['high']
        bar_low  = today_bar['low']
        bar_close= today_bar['close']
        bar_vol  = today_bar['volume']

        if bar_close <= 0:
            continue



        # ── Get Daily Camarilla levels ────────────────────────
        c.execute('''
            SELECT open, high, low, close
            FROM daily_prices
            WHERE symbol = ? AND date < ?
            ORDER BY date DESC LIMIT 1
        ''', (sym, bar_date))
        prev = c.fetchone()
        if not prev:
            continue

        rng = float(prev['high']) - float(prev['low'])
        l3  = round(float(prev['close']) - rng * 0.55/2, 2)
        l4  = round(float(prev['close']) - rng * 1.1/2,  2)
        h3  = round(float(prev['close']) + rng * 0.55/2, 2)
        h4  = round(float(prev['close']) + rng * 1.1/2,  2)

        # Filter 1: Daily L3 must align with HV Low (within 4%)
        l3_vs_hv = abs(l3 - hv_low) / hv_low * 100
        if l3_vs_hv > L3_VS_HV_MAX:
            continue

        # Filter 2: Price must touch Daily L3
        if bar_low > l3 * 1.005:
            continue

        # Filter 3: Green candle
        if bar_close <= bar_open:
            continue

        # Filter 4: Price not more than 3% above HV Low
        pct_above = (bar_close - hv_low) / hv_low * 100
        if pct_above > 3.0:
            continue

        # Filter 5: Volume >= 1x average
        c.execute('''
            SELECT AVG(volume) as avg_vol, AVG(close) as avg_close
            FROM   daily_prices
            WHERE  symbol = ? AND date < ? AND volume > 0
            ORDER  BY date DESC LIMIT 20
        ''', (sym, bar_date))
        avg_row   = c.fetchone()
        if not avg_row or not avg_row['avg_vol']:
            continue
        avg_vol   = avg_row['avg_vol']
        avg_close = avg_row['avg_close'] or bar_close
        vol_ratio = bar_vol / avg_vol if avg_vol > 0 else 0
        if vol_ratio < MIN_VOL_RATIO:
            log(f'  {sym}: low volume ({vol_ratio:.1f}x) — skip')
            continue

                # Filter 6: Liquidity check
        daily_value_cr = (avg_vol * avg_close) / 1e7
        if daily_value_cr < MIN_VALUE_CR:
            continue

        # Filter 7: Not the HV day itself
        if bar_date == hv_date:
            continue

        # Compute trade levels
        entry      = round(l3, 2)       # Daily L3 = entry
        sl         = round(l4, 2)       # Daily L4 = SL
        target     = round(hv_high, 2)  # HV High = target
        target2    = round(hv_high, 2)  # same
        risk       = round(entry - sl, 2)
        reward     = round(target - entry, 2)
        rr         = round(reward / risk, 1) if risk > 0 else 0
        sl_pct     = round(risk / entry * 100, 2)
        target_pct = round(reward / entry * 100, 1)

        if rr < MIN_RR:
            log(f'  {sym}: R:R too low ({rr:.1f}) -- skip')
            continue

        upside_pct = (hv_high - hv_low) / hv_low * 100
        if upside_pct < MIN_UPSIDE_PCT:
            log(f'  {sym}: upside too small ({upside_pct:.1f}%) -- skip')
            continue

        hv_age = (today - datetime.strptime(hv_date, '%Y-%m-%d').date()).days

        signals.append({
            'symbol':      sym,
            'bar_date':    bar_date,
            'hv_date':     hv_date,
            'hv_age':      hv_age,
            'entry':       entry,
            'sl':          sl,
            'target':      target,
            'risk':        risk,
            'reward':      reward,
            'rr':          rr,
            'sl_pct':      sl_pct,
            'target_pct':  target_pct,
            'close':       round(bar_close, 2),
            'vol_ratio':   round(vol_ratio, 1),
            'hv_vol':      hv_vol,
            'l3':          round(l3, 2),
            'l4':          round(l4, 2),
            'h3':          round(h3, 2),
            'h4':          round(h4, 2),
            'target2':     round(target2, 2),
            'l3_vs_hv':    round(l3_vs_hv, 2),
            'daily_val_cr': round(daily_value_cr, 1),
        })
        log(f"  SIGNAL: {sym} | HV age {hv_age}d | "
            f"Entry {entry} | Target {target} | "
            f"Vol {vol_ratio:.1f}x | R:R 1:{rr}")

    conn.close()

    log(f"\nTotal signals: {len(signals)}")

    if not signals:
        # Send no-signal summary once per day
        ist_time = get_ist().strftime('%d %b %Y %H:%M IST')
        msg = (
            f"HV Scanner — {ist_time}\n"
            f"No buy signals today.\n"
            f"Scanned {len(candidates)} stocks with HV in last {MAX_HV_AGE_DAYS} days.\n"
            f"Waiting for price to touch HV Low with volume."
        )
        if not alerts_sent:  # only send if no alerts sent today
            send_telegram(msg, test_mode)
        return

    # Sort by R:R (best first)
    signals.sort(key=lambda x: x['rr'], reverse=True)

    # Send individual alerts and place trades
    ist_time = get_ist().strftime('%d %b %Y %H:%M IST')

    # Try to import trader
    try:
        from angel_trader import place_trade
        auto_trade = True
        log("Auto-trading enabled via Angel One")
    except Exception as e:
        auto_trade = False
        log(f"Auto-trading disabled: {e}")

    for sig in signals:
        sym = sig['symbol']

        # Place trade automatically
        if auto_trade:
            log(f"  Auto-trading {sym}...")
            result = place_trade(
                sym,
                sig['entry'],
                sig['sl'],
                sig['target'],
                signal_source='HV'
            )
            log(f"  Trade result: {result}")
        else:
            # Manual alert only
            leg1_rr = round((sig['w_h3']-sig['entry'])/(sig['entry']-sig['sl']),1)
            msg = (
                f"🎯 BUY SIGNAL: {sym}\n"
                f"{ist_time}\n"
                f"\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"HV + Daily Camarilla\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"HV age   : {sig['hv_age']} days\n"
                f"L3 vs HV : {sig['l3_vs_hv']}% aligned\n"
                f"Volume   : {sig['vol_ratio']}x\n"
                f"\n"
                f"📌 TRADE LEVELS\n"
                f"Entry  : Rs {sig['entry']} (Daily L3)\n"
                f"SL     : Rs {sig['sl']} (Daily L4)\n"
                f"Target : Rs {sig['target']} (HV High)\n"
                f"R:R    : 1:{sig['rr']}\n"
                f"Upside : {sig['target_pct']}%\n"
                f"\n"
                f"💰 POSITION (Rs 20,000)\n"
                f"Shares : {int(20000/sig['entry'])}\n"
                f"Risk   : Rs {round((sig['entry']-sig['sl'])*int(20000/sig['entry']),0):,.0f}\n"
                f"Reward : Rs {round((sig['target']-sig['entry'])*int(20000/sig['entry']),0):,.0f}\n"
                f"\n"
                f"✅ ACTION CHECKLIST\n"
                f"1. GTT BUY auto-placed @ Rs {sig['entry']}\n"
                f"2. GTT SL placed @ Rs {sig['sl']}\n"
                f"3. Auto-sells at Rs {sig['target']} (HV High)\n"
                f"4. Monitor Telegram for updates\n"
                f"\n"
                f"📊 BACKTEST (2021-2026)\n"
                f"Win rate: 12% | EV: Rs 411/trade\n"
                f"Avg win: +Rs 3,107 | Avg loss: -Rs 174\n"
                f"\n"
                f"Chart: tradingview.com/chart/?symbol=NSE:{sym}"
            )
            if send_telegram(msg, test_mode):
                save_alert_sent(sym)

    # Summary if multiple signals
    if len(signals) > 1:
        summary_lines = [
            f"HV SCAN SUMMARY — {ist_time}",
            f"{len(signals)} buy signals today:",
            "",
        ]
        for sig in signals:
            summary_lines.append(
                f"{sig['symbol']:<15} Entry:{sig['entry']:>8} "
                f"Target:{sig['target']:>8} "
                f"R:R 1:{sig['rr']} "
                f"Vol:{sig['vol_ratio']}x"
            )
        summary_lines.append("")
        summary_lines.append("All are limit orders at HV Low")
        summary_lines.append("Take the one with best R:R if too many signals")
        send_telegram('\n'.join(summary_lines), test_mode)

# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--test', action='store_true',
                        help='Test mode — print signals without sending Telegram')
    args = parser.parse_args()

    os.makedirs(os.path.join(BASE_DIR, 'logs'), exist_ok=True)
    scan(test_mode=args.test)
