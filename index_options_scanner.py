#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  index_options_scanner.py
#  Weekly Camarilla L3/H3 scanner for BankNifty and Nifty 50
#  Monitors live price every 15 min during market hours
#  Fires Telegram when L3 bounce (CALL) or H3 fade (PUT) triggers
#
#  Setup (from backtest — 24 months, +₹5,263/lot expected):
#    CALL: Price touches L3, closes above L3, green candle
#          → Buy ATM CALL, Target H3, SL L4
#    PUT : Price touches H3, closes below H3, red candle
#          → Buy ATM PUT, Target L3, SL H4
#
#  Two modes:
#    --mode morning  : compute weekly levels, check watchlist (8AM/9:30AM)
#    --mode monitor  : live 15-min check during market hours
#
#  Usage:
#    python3 index_options_scanner.py --mode morning
#    python3 index_options_scanner.py --mode monitor
# ═══════════════════════════════════════════════════════════════

import yfinance as yf
import sqlite3
import os
import json
import argparse
import urllib.request
import urllib.parse
from datetime import datetime, date, timedelta
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────
TELEGRAM_TOKEN   = '8788684553:AAHfZ_q0Hh2mdUNOwELu_PQPePpptKtixGM'
TELEGRAM_CHAT_ID = '-5282064943'

BASE_DIR       = os.path.expanduser('~/nse-scanner')
DB_PATH        = os.path.join(BASE_DIR, 'nse_data.db')
LOG_FILE       = os.path.join(BASE_DIR, 'logs/index_options.log')
ALERTS_FILE    = os.path.join(BASE_DIR, 'index_options_alerts_today.json')
LEVELS_FILE    = os.path.join(BASE_DIR, 'index_weekly_levels.json')

# Index definitions
INDICES = {
    'BANKNIFTY': {
        'yf_symbol':  '^NSEBANK',
        'lot_size':   15,
        'atm_pct':    0.50,    # ATM premium ≈ 0.5% of index
        'strike_gap': 100,     # BankNifty strike gap = 100
        'name':       'Bank Nifty',
        'db_symbol':  'BANKNIFTY',
    },
    'NIFTY50': {
        'yf_symbol':  '^NSEI',
        'lot_size':   75,
        'atm_pct':    0.45,    # ATM premium ≈ 0.45% of index
        'strike_gap': 50,      # Nifty strike gap = 50
        'name':       'Nifty 50',
        'db_symbol':  'NIFTY50',
    }
}

# Scanner parameters (validated by 24-month backtest)
MIN_RANGE_PCT   = 1.0    # weekly range >= 1% of close
MIN_REWARD_PCT  = 1.0    # H3 must be 1.0%+ above L3
MAX_SL_PCT      = 5.0    # SL within 5% of entry
MAX_DIST_PCT    = 0.5    # price within 0.5% of L3/H3
EMA_PERIOD      = 100    # 100-day EMA ≈ 20-week EMA
MAX_HOLD_DAYS   = 3      # exit by day 3 regardless

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

def is_market_hours():
    ist = get_ist()
    if ist.weekday() >= 5:
        return False
    mins = ist.hour * 60 + ist.minute
    return (9 * 60 + 15) <= mins <= (15 * 60 + 30)

def get_week_start():
    d = date.today()
    return (d - timedelta(days=d.weekday())).strftime('%Y-%m-%d')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def send_telegram(message):
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        # Remove HTML tags for plain text sending
        import re
        plain = re.sub(r'<[^>]+>', '', message)
        data = urllib.parse.urlencode({
            'chat_id': TELEGRAM_CHAT_ID,
            'text':    plain,
        }).encode()
        req  = urllib.request.Request(url, data=data, method='POST')
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            if result.get('ok'):
                log("  ✅ Telegram sent")
                return True
            else:
                log(f"  ❌ Telegram failed: {result}")
    except Exception as e:
        log(f"  ❌ Telegram error: {e}")
    return False

# ── Alert dedup ────────────────────────────────────────────────
def load_alerts_sent():
    today = str(date.today())
    if os.path.exists(ALERTS_FILE):
        try:
            with open(ALERTS_FILE) as f:
                data = json.load(f)
            if data.get('date') == today:
                return set(data.get('alerts', []))
        except:
            pass
    return set()

def save_alert_sent(sym, direction):
    alerts = load_alerts_sent()
    alerts.add(f"{sym}_{direction}")
    with open(ALERTS_FILE, 'w') as f:
        json.dump({'date': str(date.today()), 'alerts': list(alerts)}, f)

def already_alerted(sym, direction):
    return f"{sym}_{direction}" in load_alerts_sent()

# ── Camarilla formula ──────────────────────────────────────────
def compute_camarilla(high, low, close):
    rng = high - low
    h3  = close + rng * 1.1 / 4
    h4  = close + rng * 1.1 / 2
    h5  = (high / low) * close if low > 0 else 0
    l3  = close - rng * 1.1 / 4
    l4  = close - rng * 1.1 / 2
    l5  = close - (h5 - close) if h5 > 0 else 0
    return {
        'h3': round(h3, 2), 'h4': round(h4, 2), 'h5': round(h5, 2),
        'l3': round(l3, 2), 'l4': round(l4, 2), 'l5': round(l5, 2),
        'range': round(rng, 2)
    }

def get_atm_strike(price, strike_gap):
    """Round price to nearest strike gap."""
    return round(price / strike_gap) * strike_gap

def get_expiry_guidance(sym):
    """Return which expiry to use based on days remaining."""
    today  = date.today()
    # BankNifty expires Wednesday, Nifty expires Thursday
    days_map = {'BANKNIFTY': 2, 'NIFTY50': 3}  # weekday number
    target_weekday = days_map.get(sym, 3)

    # Find next expiry
    days_ahead = (target_weekday - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    next_expiry = today + timedelta(days=days_ahead)
    days_to_exp = (next_expiry - today).days

    # On expiry day itself — use next week
    if days_ahead == 0 or days_to_exp <= 1:
        next_expiry += timedelta(days=7)
        days_to_exp += 7
        note = "⚠️ Expiry day — using NEXT week"
    elif days_to_exp <= 2:
        next_expiry += timedelta(days=7)
        days_to_exp += 7
        note = "⚠️ Near expiry — using NEXT week"
    else:
        note = f"✅ {days_to_exp} days to expiry"

    return next_expiry.strftime('%d %b %Y'), note

# ═══════════════════════════════════════════════════════════════
#  COMPUTE WEEKLY LEVELS
# ═══════════════════════════════════════════════════════════════
def compute_weekly_levels(conn):
    """
    Compute weekly Camarilla levels from last week's OHLCV.
    Saves to JSON file for use by monitor.
    Returns dict of levels per index.
    """
    today      = date.today()
    week_start = get_week_start()
    prev_mon   = (today - timedelta(days=today.weekday() + 7)).strftime('%Y-%m-%d')
    prev_fri   = (today - timedelta(days=today.weekday() + 3)).strftime('%Y-%m-%d')

    log(f"Computing weekly levels for week {week_start}")
    log(f"Using previous week: {prev_mon} to {prev_fri}")

    c      = conn.cursor()
    levels = {}

    for sym, info in INDICES.items():
        # Get last week's OHLCV from DB
        c.execute('''
            SELECT MAX(high) as wh, MIN(low) as wl,
                   (SELECT close FROM daily_prices d2
                    WHERE  d2.symbol = ? AND d2.date <= ?
                    ORDER  BY d2.date DESC LIMIT 1) as wc,
                   COUNT(*) as days
            FROM   daily_prices
            WHERE  symbol = ? AND date BETWEEN ? AND ?
        ''', (sym, prev_fri, sym, prev_mon, prev_fri))
        r = c.fetchone()

        if not r or not r['wc'] or r['days'] < 3:
            log(f"  {sym}: insufficient last week data")
            continue

        prev_high  = r['wh']
        prev_low   = r['wl']
        prev_close = r['wc']
        range_pct  = (prev_high - prev_low) / prev_close * 100

        if range_pct < MIN_RANGE_PCT:
            log(f"  {sym}: range {range_pct:.2f}% too narrow — skip")
            continue

        cam = compute_camarilla(prev_high, prev_low, prev_close)

        # Check reward quality
        reward_pct = (cam['h3'] - cam['l3']) / cam['l3'] * 100
        if reward_pct < MIN_REWARD_PCT:
            log(f"  {sym}: reward {reward_pct:.2f}% too narrow — skip")
            continue

        # 20-week EMA from daily_prices
        c.execute('''
            SELECT close FROM daily_prices
            WHERE  symbol = ? AND date < ?
            ORDER  BY date DESC LIMIT 100
        ''', (sym, week_start))
        closes = [row['close'] for row in c.fetchall()]
        ema20w = None
        if len(closes) >= 20:
            closes  = list(reversed(closes))
            k       = 2 / (EMA_PERIOD + 1)
            ema     = closes[0]
            for p in closes[1:]:
                ema = p * k + ema * (1 - k)
            ema20w = round(ema, 2)

        # Get today's close for context
        c.execute('''
            SELECT close FROM daily_prices
            WHERE  symbol = ? ORDER BY date DESC LIMIT 1
        ''', (sym,))
        latest = c.fetchone()
        latest_close = latest['close'] if latest else prev_close

        sl_pct = (cam['l3'] - cam['l4']) / cam['l3'] * 100

        levels[sym] = {
            'sym':         sym,
            'name':        info['name'],
            'week_start':  week_start,
            'prev_high':   round(prev_high, 2),
            'prev_low':    round(prev_low, 2),
            'prev_close':  round(prev_close, 2),
            'range_pct':   round(range_pct, 2),
            'reward_pct':  round(reward_pct, 2),
            'sl_pct':      round(sl_pct, 2),
            'ema20w':      ema20w,
            'latest_close': round(latest_close, 2),
            **cam,
            'lot_size':    info['lot_size'],
            'atm_pct':     info['atm_pct'],
            'strike_gap':  info['strike_gap'],
        }
        log(f"  {sym}: L3={cam['l3']} H3={cam['h3']} L4={cam['l4']} H4={cam['h4']}"
            f" | Range:{range_pct:.1f}% | EMA:{ema20w}")

    # Save levels to JSON for monitor to use
    with open(LEVELS_FILE, 'w') as f:
        json.dump({'week_start': week_start, 'levels': levels,
                   'computed_at': get_ist().strftime('%Y-%m-%d %H:%M:%S')}, f)

    log(f"Levels saved for {len(levels)} indices")
    return levels

# ═══════════════════════════════════════════════════════════════
#  MORNING SCAN — ONE clear trade to watch today
# ═══════════════════════════════════════════════════════════════
def pick_best_setup(levels):
    """
    Pick the single best setup to watch today.
    Logic:
      1. Check trend for each index (BULLISH = above 20w EMA)
      2. If BULLISH → consider CALL (L3 bounce)
         If BEARISH → consider PUT (H3 fade)
      3. Pick the setup where price is CLOSEST to the level
         (highest probability of triggering today)
      4. Return one setup dict
    """
    candidates = []

    for sym, lv in levels.items():
        price    = lv['latest_close']
        dist_l3  = abs(price - lv['l3']) / price * 100
        dist_h3  = abs(price - lv['h3']) / price * 100
        # CALL setup — L3 bounce
        candidates.append({
            'sym':       sym,
            'direction': 'CALL',
            'dist_pct':  dist_l3,
            'level':     lv['l3'],
            'lv':        lv,
        })
        # PUT setup — H3 fade
        candidates.append({
            'sym':       sym,
            'direction': 'PUT',
            'dist_pct':  dist_h3,
            'level':     lv['h3'],
            'lv':        lv,
        })

    if not candidates:
        return None

    # Pick the one closest to its trigger level
    return sorted(candidates, key=lambda x: x['dist_pct'])[0]


def morning_scan(conn):
    """Compute levels and send ONE clear trade to watch today."""
    ist_time = get_ist().strftime('%d %b %Y %H:%M IST')
    week     = get_week_start()

    # Load or compute levels
    today_weekday = date.today().weekday()
    if today_weekday == 0 or not os.path.exists(LEVELS_FILE):
        levels = compute_weekly_levels(conn)
    else:
        try:
            with open(LEVELS_FILE) as f:
                saved = json.load(f)
            if saved.get('week_start') == week:
                levels = saved['levels']
                log("Loaded existing weekly levels")
            else:
                levels = compute_weekly_levels(conn)
        except:
            levels = compute_weekly_levels(conn)

    if not levels:
        send_telegram(
            f"📊 <b>INDEX OPTIONS SCANNER</b>\n"
            f"⏰ {ist_time}\n\n"
            f"⚠️ No valid levels this week — low range market."
        )
        return

    # Pick the single best setup
    best = pick_best_setup(levels)
    if not best:
        send_telegram(
            f"📊 <b>INDEX OPTIONS — {ist_time}</b>\n\n"
            f"⚠️ No clear setup today. Stay out."
        )
        return

    sym        = best['sym']
    direction  = best['direction']
    lv         = best['lv']
    dist_pct   = best['dist_pct']
    info       = INDICES[sym]

    # Trade details
    if direction == 'CALL':
        entry    = lv['l3']
        sl       = lv['l4']
        target   = lv['h3']
        emoji    = '🟢'
        action   = 'BUY CALL (CE)'
        trigger  = f"index LOW touches ₹{entry:,.0f}"
        candle   = "green candle (close > open)"
        opt_type = 'CE'
    else:
        entry    = lv['h3']
        sl       = lv['h4']
        target   = lv['l3']
        emoji    = '🔴'
        action   = 'BUY PUT (PE)'
        trigger  = f"index HIGH touches ₹{entry:,.0f}"
        candle   = "red candle (close < open)"
        opt_type = 'PE'

    # Options calc
    atm_strike  = get_atm_strike(entry, info['strike_gap'])
    atm_premium = round(entry * info['atm_pct'] / 100, 0)
    lot_cost    = atm_premium * info['lot_size']
    expiry, exp_note = get_expiry_guidance(sym)
    risk        = abs(entry - sl)
    reward      = abs(target - entry)
    rr          = round(reward / risk, 1) if risk > 0 else 2
    sl_pct      = round(risk / entry * 100, 1)
    t1_pct      = round(reward / entry * 100, 1)
    opt_win_est = round(reward * 0.65 / atm_premium * 100, 0) if atm_premium else 0
    opt_win_rs  = round(lot_cost * opt_win_est / 100, 0)
    opt_los_rs  = round(lot_cost * 0.40, 0)
    lots_20k    = max(1, int(20000 / lot_cost))
    lots_30k    = max(1, int(30000 / lot_cost))

    # Context for the other index
    other_sym  = [s for s in levels if s != sym]
    other_line = ""
    if other_sym:
        o     = levels[other_sym[0]]
        o_ltp = o['latest_close']
        other_line = (f"\n📌 {INDICES[other_sym[0]]['name']} LTP: "
                      f"₹{o_ltp:,.0f} (watching but secondary today)")

    msg = f"""{emoji} <b>TODAY'S TRADE — {lv['name']}</b>
⏰ {ist_time}  |  Week: {week}

<b>ACTION: {action}</b>
Trigger: {trigger}
Confirm: {candle}

<b>WHAT TO DO WHEN IT TRIGGERS:</b>
1. Check candle closed {candle}
2. Open Zerodha — Options — {lv['name']}
3. Expiry: {expiry}  {exp_note}
4. Buy <b>{atm_strike} {opt_type}</b>
5. Set alert if option loses 30%

<b>LEVELS</b>
Entry  : {entry:,.0f}
SL     : {sl:,.0f}  ({sl_pct}% away)
Target : {target:,.0f}  ({t1_pct}% move)
R:R    : 1:{rr}

<b>MONEY (1 lot)</b>
Lot cost : Rs {lot_cost:,.0f}
If WIN   : +Rs {opt_win_rs:,.0f}  (~{opt_win_est:.0f}% gain)
If LOSS  : -Rs {opt_los_rs:,.0f}  (~40% loss)

<b>LOTS TO BUY</b>
Rs 20k budget — {lots_20k} lot
Rs 30k budget — {lots_30k} lots

<b>EXIT RULES</b>
Exit by 3:15 PM today (hard rule)
No trigger today — hold till Day 3 max
Option drops 30% from buy — exit early
Target hit — book profit immediately
{other_line}

Scanner monitors every 15 min
Entry alert fires automatically when triggered"""

    send_telegram(msg)
    log(f"Morning briefing sent: {sym} {direction} | "
        f"Level: {entry:,.0f} | Dist: {dist_pct:.2f}%")


# ═══════════════════════════════════════════════════════════════
#  LIVE MONITOR — 15-min trigger check
# ═══════════════════════════════════════════════════════════════
def fetch_live_ohlcv(yf_symbol):
    """Fetch today's live OHLCV from yfinance."""
    try:
        ticker = yf.Ticker(yf_symbol)
        hist   = ticker.history(period='2d', interval='1d')
        if hist.empty:
            return None
        last = hist.iloc[-1]
        return {
            'open':  round(float(last['Open']),  2),
            'high':  round(float(last['High']),  2),
            'low':   round(float(last['Low']),   2),
            'close': round(float(last['Close']), 2),
            'date':  str(hist.index[-1].date()),
        }
    except Exception as e:
        log(f"  fetch error {yf_symbol}: {e}")
        return None

def build_trigger_message(sym, direction, live, lv):
    """Build the options entry Telegram message."""
    ist_time  = get_ist().strftime('%d %b %Y %H:%M IST')
    info      = INDICES[sym]
    price     = live['close']

    if direction == 'CALL':
        entry     = lv['l3']
        sl        = lv['l4']
        target1   = lv['h3']
        target2   = lv['h4']
        emoji     = '🟢'
        action    = 'BUY CALL'
        setup     = 'L3 Bounce'
        atm_str   = 'CE'
    else:
        entry     = lv['h3']
        sl        = lv['h4']
        target1   = lv['l3']
        target2   = lv['l4']
        emoji     = '🔴'
        action    = 'BUY PUT'
        setup     = 'H3 Fade'
        atm_str   = 'PE'

    # Options details
    atm_strike  = get_atm_strike(entry, info['strike_gap'])
    atm_premium = round(entry * info['atm_pct'] / 100, 0)
    lot_cost    = atm_premium * info['lot_size']
    expiry, exp_note = get_expiry_guidance(sym)

    # P&L estimates
    risk        = abs(entry - sl)
    reward      = abs(target1 - entry)
    rr          = round(reward / risk, 1) if risk > 0 else 0
    sl_pct      = round(risk / entry * 100, 1)
    t1_pct      = round(reward / entry * 100, 1)

    # Option P&L estimate
    opt_win_est = round(reward * 0.65 / atm_premium * 100, 0) if atm_premium > 0 else 0
    opt_win_rs  = round(lot_cost * opt_win_est / 100, 0)
    opt_los_rs  = round(lot_cost * 0.40, 0)

    # Lots suggestion for ₹20-30k capital
    lots_20k = max(1, int(20000 / lot_cost))
    lots_30k = max(1, int(30000 / lot_cost))

    msg = f"""{emoji} <b>INDEX OPTIONS SIGNAL — {action}</b>
⏰ {ist_time}

<b>{info['name']} — {setup}</b>

<b>OPTIONS TRADE</b>
Strike   : {atm_strike} {atm_str}
Expiry   : {expiry}
{exp_note}
Premium  : Rs {atm_premium:.0f} per share
Lot size : {info['lot_size']} shares
Lot cost : Rs {lot_cost:,.0f}

<b>LEVELS</b>
Entry    : {entry:,.0f}
SL       : {sl:,.0f}  ({sl_pct}% risk)
Target 1 : {target1:,.0f}  (+{t1_pct}%)
Target 2 : {target2:,.0f}
R:R      : 1:{rr}

<b>P&L ESTIMATE (1 lot)</b>
Win  : +Rs {opt_win_rs:,.0f}  (~{opt_win_est:.0f}% gain)
Loss : -Rs {opt_los_rs:,.0f}  (~40% if SL hit)

<b>POSITION SIZE</b>
Rs 20k — {lots_20k} lot
Rs 30k — {lots_30k} lots

<b>LIVE DATA</b>
Open : {live['open']:,.0f}
High : {live['high']:,.0f}
Low  : {live['low']:,.0f}
LTP  : {live['close']:,.0f}

Exit by 3:15 PM — no overnight holding
Exit early if option loses 30% from buy price"""

    return msg

def monitor():
    """15-min live monitor — check if L3/H3 touched."""
    if not is_market_hours():
        log("Market closed — skipping monitor")
        return

    # Time filter: full market hours 9:15 AM to 3:30 PM IST
    ist = get_ist()
    ist_mins = ist.hour * 60 + ist.minute
    if ist_mins < 9*60+15 or ist_mins > 15*60+30:
        log(f"Outside trading window (9:15-3:30 PM) — skipping monitor")
        return

    # Thursday — trade until 3:00 PM only (expiry day)
    is_thursday = ist.weekday() == 3
    if is_thursday and ist_mins > 15*60:
        log("Thursday after 3 PM — skipping (expiry day)")
        return

    # Load weekly levels
    if not os.path.exists(LEVELS_FILE):
        log("No levels file — run morning scan first")
        return

    try:
        with open(LEVELS_FILE) as f:
            saved = json.load(f)
        week   = get_week_start()
        if saved.get('week_start') != week:
            log("Levels are from previous week — need morning scan")
            return
        levels = saved['levels']
    except Exception as e:
        log(f"Error loading levels: {e}")
        return

    if not levels:
        log("No valid levels this week")
        return

    log(f"Monitoring {len(levels)} indices...")

    # Load previous candle for false breakout detection
    prev_file = os.path.join(BASE_DIR, 'index_prev_candle.json')
    try:
        with open(prev_file) as f:
            prev_candles = json.load(f)
    except:
        prev_candles = {}
    new_prev = {}

    for sym, lv in levels.items():
        info       = INDICES[sym]
        live       = fetch_live_ohlcv(info['yf_symbol'])
        if not live:
            log(f"  {sym}: no live data")
            continue

        l3    = lv['l3']
        h3    = lv['h3']
        l4    = lv['l4']
        h4    = lv['h4']
        close = live['close']
        high  = live['high']
        low   = live['low']
        op    = live['open']
        prev_close = prev_candles.get(sym, {}).get('close', close)
        ist_time   = get_ist().strftime('%d %b %Y %H:%M IST')

        log(f"  {sym}: LTP={close:,.0f} | L3={l3:,.0f} | H3={h3:,.0f} | L4={l4:,.0f} | H4={h4:,.0f}")

        # Save for next scan
        new_prev[sym] = {'close': close, 'high': high, 'low': low}

        # ── PIVOT BOSS CAMARILLA RULES ────────────────────────
        # Rule 1: Only alert ONCE per level per day (no spam)
        # Rule 2: Price must be within 0.5% of level at signal time
        # Rule 3: Entry valid only within 30 mins of first rejection
        # Rule 4: Reset if price returns above/below level then rejects again

        dist_h3 = abs(close - h3) / h3 * 100
        dist_h4 = abs(close - h4) / h4 * 100
        dist_l3 = abs(close - l3) / l3 * 100
        dist_l4 = abs(close - l4) / l4 * 100

        MAX_DIST = 0.8  # max 0.8% away from level to be valid

        # ── PUT SIGNALS ───────────────────────────────────────

        # Signal 1: H3 rejection
        # Conditions: touched H3, closed below H3, red candle, within 0.8%
        if (high >= h3 * 0.998
                and close < h3
                and close < op
                and dist_h3 <= MAX_DIST):
            key = f"{sym}_PUT_H3"
            if not already_alerted(sym, 'PUT_H3'):
                log(f"  PUT S1: H3 rejection | dist={dist_h3:.2f}%")
                save_alert_sent(sym, 'PUT_H3')
                send_telegram(
                    f"PUT SIGNAL: {info['name']}\n"
                    f"{ist_time}\n"
                    f"H3 Rejection ({h3:,.0f})\n"
                    f"Candle closed below H3\n"
                    f"\n"
                    f"Entry  : {close:,.0f}\n"
                    f"SL     : {h4:,.0f} (above H4)\n"
                    f"Target1: {round(h3-(h4-h3)):,.0f} (H3 mirror)\n"
                    f"Target2: {l3:,.0f} (W-L3)\n"
                    f"\n"
                    f"Distance from H3: {dist_h3:.1f}%\n"
                    f"Exit by 3:15 PM"
                )
            else:
                log(f"  PUT H3 already alerted today — skip")

        # Signal 2: H4 rejection (stronger signal)
        elif (high >= h4 * 0.998
                and close < h4
                and close < op
                and dist_h4 <= MAX_DIST):
            if not already_alerted(sym, 'PUT_H4'):
                log(f"  PUT S2: H4 rejection | dist={dist_h4:.2f}%")
                save_alert_sent(sym, 'PUT_H4')
                send_telegram(
                    f"STRONG PUT: {info['name']}\n"
                    f"{ist_time}\n"
                    f"H4 Rejection ({h4:,.0f}) — Strong!\n"
                    f"Candle closed below H4\n"
                    f"\n"
                    f"Entry  : {close:,.0f}\n"
                    f"SL     : above H4 spike\n"
                    f"Target1: {h3:,.0f} (W-H3)\n"
                    f"Target2: {l3:,.0f} (W-L3)\n"
                    f"\n"
                    f"Distance from H4: {dist_h4:.1f}%\n"
                    f"Exit by 3:15 PM"
                )
            else:
                log(f"  PUT H4 already alerted today — skip")

        # Signal 3: H4 false breakout (price was above H4, now back below)
        elif (prev_close > h4
                and close < h4
                and close < op
                and dist_h4 <= MAX_DIST * 2):
            if not already_alerted(sym, 'PUT_H4_FALSE'):
                log(f"  PUT S3: H4 false breakout")
                save_alert_sent(sym, 'PUT_H4_FALSE')
                send_telegram(
                    f"FALSE BREAKOUT PUT: {info['name']}\n"
                    f"{ist_time}\n"
                    f"Was above H4 ({h4:,.0f})\n"
                    f"Closed back below — highest prob setup!\n"
                    f"\n"
                    f"Entry  : {close:,.0f}\n"
                    f"SL     : {h4:,.0f}\n"
                    f"Target1: {h3:,.0f} (W-H3)\n"
                    f"Target2: {l3:,.0f} (W-L3)\n"
                    f"\n"
                    f"Exit by 3:15 PM"
                )
            else:
                log(f"  PUT H4 false breakout already alerted — skip")

        # Signal 4: H4 false breakout
        elif (prev_close > h4 and close < h4 and close < op):
            log(f"  PUT S4: H4 false breakout")
            send_telegram(
                f"🔴 FALSE BREAKOUT PUT: {info['name']}\n"
                f"{ist_time}\n"
                f"FALSE BREAKOUT CONFIRMED!\n"
                f"Was above H4 ({h4:,.0f})\n"
                f"Closed back below — red candle\n"
                f"Entry  : {close:,.0f}\n"
                f"SL     : {h4:,.0f}\n"
                f"Target1: {h3:,.0f} (H3)\n"
                f"Target2: {l3:,.0f} (L3)\n"
                f"HIGHEST PROBABILITY PUT SETUP!"
            )

        # ── CALL SIGNALS ──────────────────────────────────────
        # Signal 5: L3 bounce — one alert per day, within 0.8% of L3
        if (low <= l3 * 1.002
                and close > l3
                and close > op
                and dist_l3 <= MAX_DIST):
            if not already_alerted(sym, 'CALL_L3'):
                log(f"  CALL S5: L3 bounce | dist={dist_l3:.2f}%")
                save_alert_sent(sym, 'CALL_L3')
                send_telegram(
                    f"CALL SIGNAL: {info['name']}\n"
                    f"{ist_time}\n"
                    f"L3 Bounce ({l3:,.0f})\n"
                    f"Candle closed above L3\n"
                    f"\n"
                    f"Entry  : {close:,.0f}\n"
                    f"SL     : {l4:,.0f} (below L4)\n"
                    f"Target1: {h3:,.0f} (W-H3)\n"
                    f"Target2: {h4:,.0f} (W-H4)\n"
                    f"\n"
                    f"Distance from L3: {dist_l3:.1f}%\n"
                    f"Exit by 3:15 PM"
                )
            else:
                log(f"  CALL L3 already alerted today — skip")
        # Signal 6: L4 bounce — stronger signal
        elif (low <= l4 * 1.002
                and close > l4
                and close > op
                and dist_l4 <= MAX_DIST):
            if not already_alerted(sym, 'CALL_L4'):
                log(f"  CALL S6: L4 bounce | dist={dist_l4:.2f}%")
                save_alert_sent(sym, 'CALL_L4')
                send_telegram(
                    f"STRONG CALL: {info['name']}\n"
                    f"{ist_time}\n"
                    f"L4 Bounce ({l4:,.0f}) — Strong!\n"
                    f"Candle closed above L4\n"
                    f"\n"
                    f"Entry  : {close:,.0f}\n"
                    f"SL     : below L4 spike\n"
                    f"Target1: {l3:,.0f} (W-L3)\n"
                    f"Target2: {h3:,.0f} (W-H3)\n"
                    f"\n"
                    f"Distance from L4: {dist_l4:.1f}%\n"
                    f"Exit by 3:15 PM"
                )
            else:
                log(f"  CALL L4 already alerted today — skip")
        # Signal 7: L4 breakdown WARNING
        elif (close < l4 and prev_close >= l4):
            log(f"  WARNING: broke below L4")
            send_telegram(
                f"BREAKDOWN WARNING: {info['name']}\n"
                f"{ist_time}\n"
                f"Broke BELOW L4 ({l4:,.0f})\n"
                f"Current: {close:,.0f}\n"
                f"NO TRADE YET — watch for recovery\n"
                f"False breakdown = Strong CALL"
            )

        # Signal 8: L4 false breakdown
        elif (prev_close < l4 and close > l4 and close > op):
            log(f"  CALL S8: L4 false breakdown")
            send_telegram(
                f"🟢 FALSE BREAKDOWN CALL: {info['name']}\n"
                f"{ist_time}\n"
                f"FALSE BREAKDOWN CONFIRMED!\n"
                f"Was below L4 ({l4:,.0f})\n"
                f"Closed back above — green candle\n"
                f"Entry  : {close:,.0f}\n"
                f"SL     : {l4:,.0f}\n"
                f"Target1: {l3:,.0f} (L3)\n"
                f"Target2: {h3:,.0f} (H3)\n"
                f"HIGHEST PROBABILITY CALL SETUP!"
            )

    # Save current candle as previous
    with open(prev_file, 'w') as f:
        json.dump(new_prev, f)

    log("Monitor check complete")

# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description='Index Options Scanner')
    parser.add_argument('--mode', choices=['morning', 'monitor', 'levels'],
                        default='monitor')
    args = parser.parse_args()

    log("=" * 55)
    log(f"  Index Options Scanner — {get_ist().strftime('%d %b %Y %H:%M IST')}")
    log(f"  Mode: {args.mode}")
    log("=" * 55)

    conn = get_db()

    if args.mode == 'morning':
        morning_scan(conn)
    elif args.mode == 'monitor':
        monitor()
    elif args.mode == 'levels':
        levels = compute_weekly_levels(conn)
        for sym, lv in levels.items():
            print(f"\n{sym}:")
            for k, v in lv.items():
                print(f"  {k}: {v}")

    conn.close()
    log("Done.")

if __name__ == '__main__':
    main()
