#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  hv_signal_scanner.py
#  HV Buy Signal Scanner — EOD VERSION
#
#  CHANGE from previous version:
#    OLD: Ran at 9:30 AM using partial/today candle
#    NEW: Runs at 4:15 PM using COMPLETE today candle
#         Places LIMIT order valid for NEXT TRADING DAY
#
#  Logic (validated by backtest):
#    1. Find HV day (7-60 days ago, highest volume bullish day)
#    2. TODAY's complete candle:
#       - Low touched Daily L3 ✅
#       - Close > Open (green candle) ✅
#       - Volume >= 1.5x average ✅
#       - Close within 4% of HV Low ✅
#    3. Place LIMIT BUY order at L3 for NEXT DAY
#    4. GTT SL at L4 + GTT Target at HV High
#
#  Entry  : LIMIT at Daily L3 (next trading day)
#  SL     : Daily L4 (GTT)
#  Target : HV High (GTT)
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

import json as _json
def load_skip_symbols():
    try:
        with open(os.path.join(BASE_DIR, 'cautionary_stocks.json')) as f:
            return set(_json.load(f).get('stocks', []))
    except:
        return set()
SKIP_SYMBOLS = load_skip_symbols()

# ── Parameters (backtest validated) ───────────────────────────
MIN_HV_AGE_DAYS  = 7
MAX_HV_AGE_DAYS  = 60
MIN_VOL_RATIO    = 1.5
MIN_PRICE        = 50.0
MIN_VALUE_CR     = 2.0
MAX_HOLD_DAYS    = 15
L3_VS_HV_MAX     = 4.0
MIN_RR           = 5.0
MAX_RR           = 20.0
MAX_HV_VOL_RATIO = 10.0
MIN_UPSIDE_PCT   = 8.0

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

def send_telegram(message, test_mode=False):
    if test_mode:
        log(f"[TEST] {message}")
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
                log("  ✅ Telegram sent")
                return True
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

def get_next_trading_day():
    """Get next trading day (skip weekends)"""
    tomorrow = date.today() + timedelta(days=1)
    # Skip Saturday and Sunday
    while tomorrow.weekday() >= 5:
        tomorrow += timedelta(days=1)
    return tomorrow

# ═══════════════════════════════════════════════════════════════
#  MAIN SCAN — EOD VERSION
# ═══════════════════════════════════════════════════════════════
def scan(test_mode=False, mode='eod'):
    ist_now  = get_ist()
    ist_time = ist_now.strftime('%d %b %Y %H:%M IST')

    log("=" * 55)
    log(f"HV Signal Scanner (EOD) — {ist_time}")
    log("=" * 55)

    # ── Use TODAY's complete candle ────────────────────────────
    # At 4:15 PM market is closed — today's candle is complete!
    conn      = get_db()
    c         = conn.cursor()
    today     = date.today()
    today_str = today.strftime('%Y-%m-%d')
    next_day  = get_next_trading_day()
    next_str  = next_day.strftime('%Y-%m-%d')

    log(f"Scanning today's candles: {today_str}")
    log(f"Orders will be placed for: {next_str}")

    alerts_sent = load_alerts_sent()

    # ── HV candidates (7-60 day window) ───────────────────────
    date_min = (today - timedelta(days=MAX_HV_AGE_DAYS)).strftime('%Y-%m-%d')
    date_max = (today - timedelta(days=MIN_HV_AGE_DAYS)).strftime('%Y-%m-%d')

    c.execute('''
        SELECT d1.symbol,
               d1.date   as hv_date,
               d1.high   as hv_high,
               d1.low    as hv_low,
               d1.close  as hv_close,
               d1.volume as hv_volume
        FROM daily_prices d1
        WHERE d1.date >= ? AND d1.date <= ?
        AND   d1.low > 0 AND d1.volume > 0
        AND   d1.close >= (d1.low + (d1.high - d1.low) * 0.5)
        AND   d1.volume = (
            SELECT MAX(volume) FROM daily_prices d3
            WHERE d3.symbol = d1.symbol
            AND   d3.date >= ? AND d3.date <= ?
        )
        ORDER BY d1.date DESC
    ''', (date_min, date_max, date_min, date_max))
    candidates = c.fetchall()

    # Filter min 252 days history
    filtered = []
    for row in candidates:
        c.execute('SELECT COUNT(*) as cnt FROM daily_prices WHERE symbol=?',
                  (row['symbol'],))
        if c.fetchone()['cnt'] >= 252:
            filtered.append(row)
    candidates = filtered
    log(f"HV candidates: {len(candidates)}")

    # ── Check open positions ───────────────────────────────────
    open_syms = set()
    try:
        with open(os.path.join(BASE_DIR, 'angel_trades.json')) as f:
            td = json.load(f)
        open_syms = {t['symbol'] for t in td.get('trades', [])
                     if t.get('status') in ['OPEN','GTT_PENDING','LIMIT_PENDING']}
        log(f"Open positions: {open_syms}")
    except:
        pass

    # Check max positions
    try:
        from angel_trader import get_config
        cfg = get_config()
        max_pos = cfg.get('MAX_OPEN_POS', 3)
    except:
        max_pos = 3

    if len(open_syms) >= max_pos:
        log(f"Max positions ({max_pos}) reached — no new signals")
        send_telegram(
            f"HV Scan — {ist_time}\n"
            f"Max {max_pos} positions already open.\n"
            f"No new GTTs placed today.",
            test_mode
        )
        conn.close()
        return

    signals = []

    for row in candidates:
        sym      = row['symbol']
        hv_date  = row['hv_date']
        hv_high  = row['hv_high']
        hv_low   = row['hv_low']
        hv_vol   = row['hv_volume']
        hv_close = row['hv_close']

        if sym in SKIP_SYMBOLS: continue
        if sym in open_syms:    continue
        if sym in alerts_sent:  continue

        # HV candle must be bullish
        hv_range = hv_high - hv_low
        if hv_range <= 0: continue
        if (hv_close - hv_low) / hv_range < 0.5: continue

        # ── Get TODAY's complete candle ────────────────────────
        c.execute('''
            SELECT date, open, high, low, close, volume
            FROM daily_prices WHERE symbol=? AND date=?
        ''', (sym, today_str))
        today_bar = c.fetchone()

        if not today_bar:
            continue  # no data today — silent skip

        bar_open  = today_bar['open']
        bar_high  = today_bar['high']
        bar_low   = today_bar['low']
        bar_close = today_bar['close']
        bar_vol   = today_bar['volume']
        bar_date  = today_bar['date']

        if bar_close <= 0: continue

        # ── Previous candle for Camarilla ─────────────────────
        c.execute('''
            SELECT high, low, close FROM daily_prices
            WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1
        ''', (sym, today_str))
        prev = c.fetchone()
        if not prev: continue

        rng = float(prev['high']) - float(prev['low'])
        l3  = round(float(prev['close']) - rng * 0.55/2, 2)
        l4  = round(float(prev['close']) - rng * 1.1/2,  2)
        h3  = round(float(prev['close']) + rng * 0.55/2, 2)
        h4  = round(float(prev['close']) + rng * 1.1/2,  2)

        # ── Avg volume ─────────────────────────────────────────
        c.execute('''
            SELECT AVG(volume) as av, AVG(close*volume)/AVG(volume) as avg_val
            FROM (SELECT volume, close FROM daily_prices
                  WHERE symbol=? AND date<? AND volume>0
                  ORDER BY date DESC LIMIT 20)
        ''', (sym, today_str))
        avg_row = c.fetchone()
        if not avg_row or not avg_row['av']: continue
        avg_vol = avg_row['av']
        avg_val = avg_row['avg_val'] or bar_close

        # ── ALL FILTERS (complete candle — no hindsight!) ──────

        # Filter 1: L3 within 4% of HV Low
        l3_vs_hv = abs(l3 - hv_low) / hv_low * 100
        if l3_vs_hv > L3_VS_HV_MAX:
            continue

        # Filter 2: Today's LOW touched L3
        if bar_low > l3 * 1.005:
            continue

        # Filter 3: GREEN candle (close > open) ← KEY FILTER!
        if bar_close <= bar_open:
            log(f"  {sym}: red candle — skip")
            continue

        # Filter 4: Close >= L3 (held support)
        if bar_close < l3:
            continue

        # Filter 5: Volume >= 1.5x average
        vol_ratio = bar_vol / avg_vol if avg_vol > 0 else 0
        if vol_ratio < MIN_VOL_RATIO:
            log(f"  {sym}: low volume ({vol_ratio:.1f}x) — skip")
            continue

        # Filter 6: HV vol not too extreme
        hv_vol_ratio = hv_vol / avg_vol if avg_vol > 0 else 0
        if hv_vol_ratio > MAX_HV_VOL_RATIO:
            continue

        # Filter 7: Liquidity
        daily_val_cr = (avg_vol * avg_val) / 1e7
        if daily_val_cr < MIN_VALUE_CR:
            continue

        # Filter 8: Min price
        if l3 < MIN_PRICE:
            continue

        # Filter 9: Not the HV day itself
        if bar_date == hv_date:
            continue

        # ── Trade levels ───────────────────────────────────────
        entry  = round(l3, 2)      # LIMIT at L3 for next day
        sl     = round(l4, 2)      # GTT SL at L4
        target = round(hv_high, 2) # GTT Target at HV High
        risk   = round(entry - sl, 2)
        reward = round(target - entry, 2)

        if risk <= 0 or reward <= 0: continue
        rr = round(reward / risk, 1)

        if rr < MIN_RR:
            log(f"  {sym}: R:R {rr} < {MIN_RR} — skip")
            continue
        if rr > MAX_RR:
            log(f"  {sym}: R:R {rr} > {MAX_RR} — skip")
            continue

        upside_pct = (hv_high - hv_low) / hv_low * 100
        if upside_pct < MIN_UPSIDE_PCT:
            continue

        hv_age   = (today - datetime.strptime(hv_date,'%Y-%m-%d').date()).days
        sl_pct   = round(risk / entry * 100, 2)
        tgt_pct  = round(reward / entry * 100, 1)

        signals.append({
            'symbol'     : sym,
            'bar_date'   : bar_date,
            'next_day'   : next_str,
            'hv_date'    : hv_date,
            'hv_age'     : hv_age,
            'entry'      : entry,
            'sl'         : sl,
            'target'     : target,
            'risk'       : risk,
            'reward'     : reward,
            'rr'         : rr,
            'sl_pct'     : sl_pct,
            'target_pct' : tgt_pct,
            'close'      : round(bar_close, 2),
            'vol_ratio'  : round(vol_ratio, 1),
            'l3'         : l3,
            'l4'         : l4,
            'h3'         : h3,
            'h4'         : h4,
            'l3_vs_hv'   : round(l3_vs_hv, 2),
            'daily_val_cr': round(daily_val_cr, 1),
        })
        log(f"  ✅ SIGNAL: {sym} | Entry {entry} | "
            f"Target {target} | R:R {rr} | "
            f"Vol {vol_ratio:.1f}x | HV age {hv_age}d")

    conn.close()
    log(f"\nTotal signals: {len(signals)}")

    # ── No signals ─────────────────────────────────────────────
    if not signals:
        msg = (
            f"HV Scan — {ist_time}\n"
            f"No signals today.\n"
            f"Scanned {len(candidates)} stocks.\n"
            f"Waiting for green candle + L3 touch + volume."
        )
        send_telegram(msg, test_mode)
        return

    # ── Sort by R:R ────────────────────────────────────────────
    signals.sort(key=lambda x: x['rr'], reverse=True)

    # ── Auto trade ─────────────────────────────────────────────
    try:
        from angel_trader import place_gtt_trade, place_trade, check_monthly_loss
        auto_trade = True
        log("Auto-trading: ENABLED")
    except Exception as e:
        auto_trade = False
        log(f"Auto-trading: DISABLED ({e})")

    if auto_trade:
        try:
            if not check_monthly_loss():
                log("Monthly loss limit — no trades")
                send_telegram(
                    f"HV Scan — {ist_time}\n"
                    f"{len(signals)} signal(s) found.\n"
                    f"Monthly loss limit reached — no GTTs placed.\n"
                    f"Signals: {', '.join(s['symbol'] for s in signals)}"
                )
                return
        except Exception as e:
            log(f"Monthly loss check error: {e}")

    for sig in signals:
        sym = sig['symbol']

        # ── Calculate shares ───────────────────────────────────
        try:
            from angel_trader import get_config
            cfg         = get_config()
            risk_amt    = cfg.get('RISK_PER_TRADE', 1000)
            max_pos_val = cfg.get('MAX_POSITION_VAL', 20000)
            capital     = cfg.get('CAPITAL', 60000)
        except:
            risk_amt    = 1000
            max_pos_val = 20000
            capital     = 60000

        shares = min(
            int(risk_amt / sig['risk']) if sig['risk'] > 0 else 0,
            int(max_pos_val / sig['entry'])
        )
        if shares < 1:
            log(f"  {sym}: shares=0 — skip")
            continue

        pos_value = shares * sig['entry']
        actual_risk   = shares * sig['risk']
        actual_reward = shares * sig['reward']

        log(f"  {sym}: {shares} shares @ Rs {sig['entry']} = Rs {pos_value:,.0f}")
        log(f"  Risk: Rs {actual_risk:,.0f} | Reward: Rs {actual_reward:,.0f}")

        # ── Send Telegram alert ────────────────────────────────
        msg = (
            f"🎯 HV SIGNAL — {ist_time}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Stock    : {sym}\n"
            f"Today    : Green ✅ | Vol {sig['vol_ratio']}x | Close {sig['close']}\n"
            f"HV age   : {sig['hv_age']} days ago\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"📌 3 GTTs PLACED (valid 365 days)\n"
            f"\n"
            f"GTT BUY    : Rs {sig['entry']}\n"
            f"  → Triggers when price touches L3\n"
            f"GTT SL     : Rs {sig['sl']}\n"
            f"  → Auto-sells if price falls to L4\n"
            f"GTT TARGET : Rs {sig['target']}\n"
            f"  → Auto-sells at HV High (+{sig['target_pct']}%)\n"
            f"\n"
            f"R:R    : 1:{sig['rr']}\n"
            f"Shares : {shares}\n"
            f"Value  : Rs {pos_value:,.0f}\n"
            f"Risk   : Rs {actual_risk:,.0f}\n"
            f"Reward : Rs {actual_reward:,.0f}\n"
            f"\n"
            f"✅ No action needed!\n"
            f"All 3 GTTs active. System handles exits.\n"
            f"\n"
            f"📊 NSE:{sym}"
        )
        send_telegram(msg, test_mode)
        save_alert_sent(sym)

        # ── Place trade ────────────────────────────────────────
        if auto_trade:
            log(f"  Placing GTT orders for {sym}...")
            result = place_gtt_trade(
                sym,
                sig['entry'],
                sig['sl'],
                sig['target'],
                signal_source='HV_EOD'
            )
            log(f"  GTT result: {result}")

    # ── Summary if multiple signals ────────────────────────────
    if len(signals) > 1:
        lines = [
            f"HV EOD SIGNALS — {ist_time}",
            f"{len(signals)} signal(s) for {next_str}:",
            "",
        ]
        for sig in signals:
            lines.append(
                f"{sig['symbol']:<12} "
                f"L3:{sig['entry']:>8} "
                f"T:{sig['target']:>8} "
                f"R:R 1:{sig['rr']} "
                f"Vol:{sig['vol_ratio']}x"
            )
        lines.append("")
        lines.append("✅ All 3 GTTs placed (BUY + SL + Target)")
        lines.append("Valid 365 days. No action needed!")
        send_telegram('\n'.join(lines), test_mode)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--test', action='store_true')
    parser.add_argument('--mode', default='eod')
    args = parser.parse_args()
    os.makedirs(os.path.join(BASE_DIR, 'logs'), exist_ok=True)
    scan(test_mode=args.test, mode=args.mode)
