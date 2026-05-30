#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  camarilla_scanner.py
#  Computes Weekly Camarilla levels from daily_prices table
#  Builds + refreshes camarilla_watchlist automatically
#  Sends Telegram:
#    - Monday : full weekly watchlist
#    - Tue-Fri: only additions / removals
#
#  Called by scheduler.py:
#    - Monday    02:30 UTC (08:00 IST) — full weekly build
#    - Tue-Fri   02:30 UTC (08:00 IST) — daily refresh
#
#  No yfinance needed — all data from daily_prices table
# ═══════════════════════════════════════════════════════════════

import sqlite3
import os
import json
import urllib.request
import urllib.parse
from datetime import datetime, date, timedelta

# ══════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════
TELEGRAM_TOKEN   = '8788684553:AAHfZ_q0Hh2mdUNOwELu_PQPePpptKtixGM'
TELEGRAM_CHAT_ID = '-5282064943'

BASE_DIR = os.path.expanduser('~/nse-scanner')
DB_PATH  = os.path.join(BASE_DIR, 'nse_data.db')
LOG_FILE = os.path.join(BASE_DIR, 'logs/camarilla_scanner.log')

# ── Noise filters (from Pivot Boss book) ──────────────────────
MIN_RANGE_PCT   = 2.0    # Last week range must be >= 2% of close
                         # Avoids stocks with too-tight Cam levels
MAX_DIST_PCT    = 1.5    # Price must be within 1.5% of L3 or H3
                         # Avoids stocks too far from entry zone
MIN_CLOSE_PRICE = 20.0   # Skip penny stocks below Rs.20

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
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def send_telegram(message):
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            'chat_id':    TELEGRAM_CHAT_ID,
            'text':       message,
            'parse_mode': 'HTML',
        }).encode()
        req = urllib.request.Request(url, data=data, method='POST')
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            if result.get('ok'):
                log("  Telegram sent!")
                return True
    except Exception as e:
        log(f"  Telegram failed: {e}")
    return False

# ══════════════════════════════════════════════════════════════
#  SCHEMA — create new tables if not exist
#  Never touches existing tables (daily_prices, hv_summary etc)
# ══════════════════════════════════════════════════════════════
def init_tables(conn):
    c = conn.cursor()

    # Weekly Camarilla levels — computed once per week per stock
    c.execute('''
        CREATE TABLE IF NOT EXISTS weekly_camarilla (
            symbol      TEXT NOT NULL,
            week_start  TEXT NOT NULL,   -- Monday date YYYY-MM-DD
            prev_high   REAL,            -- last week highest high
            prev_low    REAL,            -- last week lowest low
            prev_close  REAL,            -- last Friday close
            h1 REAL, h2 REAL, h3 REAL, h4 REAL, h5 REAL,
            l1 REAL, l2 REAL, l3 REAL, l4 REAL, l5 REAL,
            range_pct   REAL,            -- (prev_high-prev_low)/prev_close * 100
            created_at  TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (symbol, week_start)
        )
    ''')

    # Camarilla watchlist — auto-managed, separate from HV watchlist
    c.execute('''
        CREATE TABLE IF NOT EXISTS camarilla_watchlist (
            symbol          TEXT NOT NULL,
            week_start      TEXT NOT NULL,   -- which week's levels
            direction       TEXT NOT NULL,   -- BULLISH / BEARISH
            h3 REAL, h4 REAL, h5 REAL,
            l3 REAL, l4 REAL, l5 REAL,
            prev_close      REAL,
            added_date      TEXT,
            status          TEXT DEFAULT 'WATCHING',
            -- WATCHING / TRIGGERED / EXPIRED
            last_checked    TEXT,
            PRIMARY KEY (symbol, week_start, direction)
        )
    ''')

    # Camarilla triggers — entry signals fired during market hours
    c.execute('''
        CREATE TABLE IF NOT EXISTS camarilla_triggers (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol        TEXT NOT NULL,
            trigger_date  TEXT NOT NULL,
            trigger_time  TEXT NOT NULL,
            direction     TEXT NOT NULL,   -- BULLISH / BEARISH
            trigger_type  TEXT NOT NULL,   -- L3_BOUNCE / H3_FADE
            entry_price   REAL,
            stop_loss     REAL,
            target1       REAL,            -- H3 (bull) / L3 (bear)
            target2       REAL,            -- H4 (bull) / L4 (bear)
            risk_reward   REAL,
            alerted_at    TEXT DEFAULT (datetime('now'))
        )
    ''')

    # Telegram log — prevent duplicate alerts
    c.execute('''
        CREATE TABLE IF NOT EXISTS camarilla_telegram_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at     TEXT DEFAULT (datetime('now')),
            alert_type  TEXT,   -- WATCHLIST / TRIGGER / SUMMARY
            symbol      TEXT,
            direction   TEXT,
            alert_date  TEXT,
            status      TEXT DEFAULT 'SENT'
        )
    ''')

    conn.commit()
    log("Tables verified/created.")

# ══════════════════════════════════════════════════════════════
#  WEEK HELPERS
# ══════════════════════════════════════════════════════════════
def get_week_start(for_date=None):
    """Return the Monday of the current (or given) week."""
    d = for_date or date.today()
    return (d - timedelta(days=d.weekday())).strftime('%Y-%m-%d')

def get_prev_week_bounds(week_start_str):
    """
    Given this week's Monday, return last week's
    Monday and Sunday (Friday is last trading day).
    """
    this_monday = datetime.strptime(week_start_str, '%Y-%m-%d').date()
    prev_monday = this_monday - timedelta(days=7)
    prev_sunday = this_monday - timedelta(days=1)
    return prev_monday.strftime('%Y-%m-%d'), prev_sunday.strftime('%Y-%m-%d')

# ══════════════════════════════════════════════════════════════
#  CAMARILLA FORMULA (from Pivot Boss book)
# ══════════════════════════════════════════════════════════════
def compute_camarilla(high, low, close):
    """
    Standard + Expanded Camarilla Equation.
    H5/L5 added as extended breakout targets.
    """
    rng  = high - low
    h1   = close + rng * 1.1 / 12
    h2   = close + rng * 1.1 / 6
    h3   = close + rng * 1.1 / 4
    h4   = close + rng * 1.1 / 2
    h5   = (high / low) * close if low > 0 else 0
    l1   = close - rng * 1.1 / 12
    l2   = close - rng * 1.1 / 6
    l3   = close - rng * 1.1 / 4
    l4   = close - rng * 1.1 / 2
    l5   = close - (h5 - close) if h5 > 0 else 0
    return dict(h1=h1, h2=h2, h3=h3, h4=h4, h5=h5,
                l1=l1, l2=l2, l3=l3, l4=l4, l5=l5)

# ══════════════════════════════════════════════════════════════
#  STEP 1 — COMPUTE WEEKLY LEVELS FOR ALL SYMBOLS
# ══════════════════════════════════════════════════════════════
def compute_weekly_levels(conn, week_start):
    """
    For every symbol in daily_prices:
      - Aggregate last week's H/L/C
      - Compute Camarilla levels
      - Save to weekly_camarilla table
    Returns dict: symbol → cam levels
    """
    c = conn.cursor()
    prev_mon, prev_sun = get_prev_week_bounds(week_start)

    log(f"Computing weekly levels for week starting {week_start}")
    log(f"Using last week: {prev_mon} to {prev_sun}")

    # Get all symbols with last week data
    c.execute('''
        SELECT
            symbol,
            MAX(high)   AS week_high,
            MIN(low)    AS week_low,
            -- Friday close = last trading day of prev week
            (SELECT close FROM daily_prices d2
             WHERE d2.symbol = d1.symbol
             AND   d2.date BETWEEN ? AND ?
             ORDER BY d2.date DESC LIMIT 1) AS week_close,
            MAX(date)   AS last_date
        FROM daily_prices d1
        WHERE date BETWEEN ? AND ?
        AND   close > 0
        GROUP BY symbol
        HAVING week_close IS NOT NULL
    ''', (prev_mon, prev_sun, prev_mon, prev_sun))

    rows     = c.fetchall()
    levels   = {}
    inserted = 0
    skipped  = 0

    for r in rows:
        sym        = r['symbol']
        prev_high  = r['week_high']
        prev_low   = r['week_low']
        prev_close = r['week_close']

        if not all([prev_high, prev_low, prev_close]):
            skipped += 1
            continue
        if prev_low <= 0 or prev_close <= 0:
            skipped += 1
            continue

        cam       = compute_camarilla(prev_high, prev_low, prev_close)
        range_pct = round((prev_high - prev_low) / prev_close * 100, 4)

        c.execute('''
            INSERT OR REPLACE INTO weekly_camarilla
                (symbol, week_start, prev_high, prev_low, prev_close,
                 h1, h2, h3, h4, h5,
                 l1, l2, l3, l4, l5,
                 range_pct)
            VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?)
        ''', (
            sym, week_start, prev_high, prev_low, prev_close,
            round(cam['h1'], 4), round(cam['h2'], 4),
            round(cam['h3'], 4), round(cam['h4'], 4),
            round(cam['h5'], 4),
            round(cam['l1'], 4), round(cam['l2'], 4),
            round(cam['l3'], 4), round(cam['l4'], 4),
            round(cam['l5'], 4),
            range_pct
        ))
        levels[sym] = {**cam,
                       'prev_high': prev_high,
                       'prev_low':  prev_low,
                       'prev_close': prev_close,
                       'range_pct': range_pct}
        inserted += 1

    conn.commit()
    log(f"Weekly levels computed: {inserted} stocks | skipped: {skipped}")
    return levels

# ══════════════════════════════════════════════════════════════
#  STEP 2 — APPLY FILTERS & BUILD WATCHLIST
# ══════════════════════════════════════════════════════════════
def apply_filters(conn, week_start, levels):
    """
    Filter 1 — Range quality: last week range >= MIN_RANGE_PCT
    Filter 2 — Price proximity: today's close within MAX_DIST_PCT of L3 or H3
    Filter 3 — L4/H4 not broken: price hasn't closed beyond stop levels this week
    Filter 4 — Minimum price: skip penny stocks

    Returns two lists: bullish_candidates, bearish_candidates
    """
    c = conn.cursor()

    # Get today's latest close for all symbols from daily_prices
    today_str = date.today().strftime('%Y-%m-%d')
    c.execute('''
        SELECT symbol, close
        FROM   daily_prices
        WHERE  date = (SELECT MAX(date) FROM daily_prices WHERE symbol = daily_prices.symbol
                       AND date <= ?)
        AND    close > 0
    ''', (today_str,))
    latest = {r['symbol']: r['close'] for r in c.fetchall()}

    # Get this week's price action (Mon to today) to check if L4/H4 broken
    week_start_str = week_start
    c.execute('''
        SELECT symbol, MIN(low) AS week_low, MAX(high) AS week_high,
               (SELECT close FROM daily_prices d2
                WHERE  d2.symbol = d1.symbol
                AND    d2.date >= ? AND d2.date <= ?
                ORDER  BY d2.date DESC LIMIT 1) AS latest_close
        FROM   daily_prices d1
        WHERE  date >= ? AND date <= ?
        AND    close > 0
        GROUP  BY symbol
    ''', (week_start_str, today_str, week_start_str, today_str))
    week_action = {r['symbol']: dict(r) for r in c.fetchall()}

    bullish = []
    bearish = []

    for sym, cam in levels.items():
        price = latest.get(sym)
        if not price or price < MIN_CLOSE_PRICE:
            continue

        # Filter 1 — Range quality
        if cam['range_pct'] < MIN_RANGE_PCT:
            continue

        h3  = cam['h3']
        h4  = cam['h4']
        h5  = cam['h5']
        l3  = cam['l3']
        l4  = cam['l4']
        l5  = cam['l5']

        # Filter 3 — L4/H4 not broken this week
        wa = week_action.get(sym, {})
        week_low  = wa.get('week_low',  price)
        week_high = wa.get('week_high', price)

        bull_l4_broken = week_low  < l4  # price dipped below stop level
        bear_h4_broken = week_high > h4  # price broke above stop level

        # Filter 2 — Proximity to L3 (bullish) or H3 (bearish)
        dist_to_l3 = abs(price - l3) / price * 100
        dist_to_h3 = abs(price - h3) / price * 100

        # ── BULLISH candidate ──────────────────────────────────
        # Price near L3, L4 not broken, recovering above L3
        if (not bull_l4_broken
                and dist_to_l3 <= MAX_DIST_PCT
                and price >= l3):           # price at or just above L3
            bullish.append({
                'symbol':     sym,
                'direction':  'BULLISH',
                'price':      round(price, 2),
                'l3':         round(l3,    2),
                'l4':         round(l4,    2),
                'l5':         round(l5,    2),
                'h3':         round(h3,    2),
                'h4':         round(h4,    2),
                'h5':         round(h5,    2),
                'prev_close': round(cam['prev_close'], 2),
                'dist_pct':   round(dist_to_l3, 2),
                'range_pct':  round(cam['range_pct'], 2),
            })

        # ── BEARISH candidate ──────────────────────────────────
        # Price near H3, H4 not broken, fading below H3
        if (not bear_h4_broken
                and dist_to_h3 <= MAX_DIST_PCT
                and price <= h3):           # price at or just below H3
            bearish.append({
                'symbol':     sym,
                'direction':  'BEARISH',
                'price':      round(price, 2),
                'h3':         round(h3,    2),
                'h4':         round(h4,    2),
                'h5':         round(h5,    2),
                'l3':         round(l3,    2),
                'l4':         round(l4,    2),
                'l5':         round(l5,    2),
                'prev_close': round(cam['prev_close'], 2),
                'dist_pct':   round(dist_to_h3, 2),
                'range_pct':  round(cam['range_pct'], 2),
            })

    # Sort by closest to entry level first
    bullish.sort(key=lambda x: x['dist_pct'])
    bearish.sort(key=lambda x: x['dist_pct'])

    log(f"Filters applied → Bullish: {len(bullish)} | Bearish: {len(bearish)}")
    return bullish, bearish

# ══════════════════════════════════════════════════════════════
#  STEP 3 — SAVE WATCHLIST
# ══════════════════════════════════════════════════════════════
def save_watchlist(conn, candidates, week_start):
    """
    Insert new candidates into camarilla_watchlist.
    Uses INSERT OR IGNORE so existing entries are untouched.
    Returns count of newly added stocks.
    """
    c       = conn.cursor()
    today   = date.today().strftime('%Y-%m-%d')
    added   = 0

    for s in candidates:
        try:
            c.execute('''
                INSERT OR IGNORE INTO camarilla_watchlist
                    (symbol, week_start, direction,
                     h3, h4, h5, l3, l4, l5,
                     prev_close, added_date, status)
                VALUES (?,?,?, ?,?,?,?,?,?, ?,?,?)
            ''', (
                s['symbol'], week_start, s['direction'],
                s['h3'], s['h4'], s['h5'],
                s['l3'], s['l4'], s['l5'],
                s['prev_close'], today, 'WATCHING'
            ))
            if conn.execute('SELECT changes()').fetchone()[0] > 0:
                added += 1
        except Exception as e:
            log(f"  Save error {s['symbol']}: {e}")

    conn.commit()
    log(f"Watchlist: {added} new stocks added.")
    return added

# ══════════════════════════════════════════════════════════════
#  STEP 4 — DAILY REFRESH (Tue-Fri)
#  Remove failed setups, mark expired
# ══════════════════════════════════════════════════════════════
def refresh_watchlist(conn, week_start):
    """
    Check existing WATCHING stocks daily:
    - If price closed below L4 → BULLISH setup EXPIRED
    - If price closed above H4 → BEARISH setup EXPIRED
    - If status already TRIGGERED → leave it
    Returns list of expired symbols for Telegram notification.
    """
    c       = conn.cursor()
    today   = date.today().strftime('%Y-%m-%d')
    expired = []

    # Get all WATCHING stocks for current week
    c.execute('''
        SELECT symbol, direction, l4, h4
        FROM   camarilla_watchlist
        WHERE  week_start = ? AND status = 'WATCHING'
    ''', (week_start,))
    watching = c.fetchall()

    for w in watching:
        sym  = w['symbol']
        dir_ = w['direction']
        l4   = w['l4']
        h4   = w['h4']

        # Get today's close from daily_prices
        c.execute('''
            SELECT close FROM daily_prices
            WHERE  symbol = ? AND date = ?
        ''', (sym, today))
        row = c.fetchone()
        if not row:
            continue
        close = row['close']

        # BULLISH setup fails if price closes below L4
        if dir_ == 'BULLISH' and close < l4:
            c.execute('''
                UPDATE camarilla_watchlist
                SET    status = 'EXPIRED', last_checked = ?
                WHERE  symbol = ? AND week_start = ? AND direction = ?
            ''', (today, sym, week_start, dir_))
            expired.append(f"{sym} (BULL — closed below L4 ₹{l4})")
            log(f"  EXPIRED: {sym} BULLISH — close ₹{close} < L4 ₹{l4}")

        # BEARISH setup fails if price closes above H4
        elif dir_ == 'BEARISH' and close > h4:
            c.execute('''
                UPDATE camarilla_watchlist
                SET    status = 'EXPIRED', last_checked = ?
                WHERE  symbol = ? AND week_start = ? AND direction = ?
            ''', (today, sym, week_start, dir_))
            expired.append(f"{sym} (BEAR — closed above H4 ₹{h4})")
            log(f"  EXPIRED: {sym} BEARISH — close ₹{close} > H4 ₹{h4}")

    conn.commit()
    return expired

# ══════════════════════════════════════════════════════════════
#  STEP 5 — EXPIRE LAST WEEK'S WATCHLIST
# ══════════════════════════════════════════════════════════════
def expire_old_watchlist(conn, week_start):
    """
    On Monday, mark all WATCHING entries from previous week as EXPIRED.
    Fresh week = fresh levels = old setups no longer valid.
    """
    c = conn.cursor()
    c.execute('''
        UPDATE camarilla_watchlist
        SET    status = 'EXPIRED'
        WHERE  week_start != ? AND status = 'WATCHING'
    ''', (week_start,))
    expired_count = conn.execute('SELECT changes()').fetchone()[0]
    conn.commit()
    if expired_count:
        log(f"Expired {expired_count} entries from previous week.")

# ══════════════════════════════════════════════════════════════
#  TELEGRAM MESSAGES
# ══════════════════════════════════════════════════════════════
def send_weekly_watchlist(bullish, bearish, week_start):
    """Monday morning — full watchlist message."""
    ist_time  = get_ist().strftime('%d %b %Y %H:%M IST')
    bull_lines = '\n'.join(
        [f"  {s['symbol']:15} L3:₹{s['l3']} | L4(SL):₹{s['l4']} | H3(T1):₹{s['h3']}"
         for s in bullish[:20]]  # cap at 20 to avoid Telegram length limit
    ) or '  None'
    bear_lines = '\n'.join(
        [f"  {s['symbol']:15} H3:₹{s['h3']} | H4(SL):₹{s['h4']} | L3(T1):₹{s['l3']}"
         for s in bearish[:20]]
    ) or '  None'

    msg = f"""📊 <b>CAMARILLA WEEKLY WATCHLIST</b>
📅 Week: {week_start}  |  {ist_time}

🟢 <b>BULLISH SETUPS ({len(bullish)} stocks)</b>
<code>{bull_lines}</code>

🔴 <b>BEARISH SETUPS ({len(bearish)} stocks)</b>
<code>{bear_lines}</code>

⚡ Monitoring live every 15 min
🚨 Entry alert fires when L3/H3 touched + candle confirms
📖 Strategy: Camarilla Weekly (Pivot Boss)"""
    return send_telegram(msg)


def send_daily_update(new_bull, new_bear, expired, is_monday=False):
    """Tue-Fri — only send if there are changes."""
    if not new_bull and not new_bear and not expired:
        log("No watchlist changes today — no Telegram needed.")
        return

    ist_time = get_ist().strftime('%d %b %Y %H:%M IST')
    lines    = []

    if new_bull:
        lines.append(f"🟢 <b>New BULLISH ({len(new_bull)})</b>")
        for s in new_bull:
            lines.append(f"  {s['symbol']} — L3:₹{s['l3']} | SL:₹{s['l4']}")

    if new_bear:
        lines.append(f"\n🔴 <b>New BEARISH ({len(new_bear)})</b>")
        for s in new_bear:
            lines.append(f"  {s['symbol']} — H3:₹{s['h3']} | SL:₹{s['h4']}")

    if expired:
        lines.append(f"\n❌ <b>Expired ({len(expired)})</b>")
        for e in expired:
            lines.append(f"  {e}")

    msg = f"""📋 <b>CAMARILLA WATCHLIST UPDATE</b>
⏰ {ist_time}

{''.join(lines)}"""
    send_telegram(msg)


def send_watchlist_summary(conn, week_start):
    """Send current WATCHING count as part of morning briefing."""
    c = conn.cursor()
    c.execute('''
        SELECT direction, COUNT(*) as cnt
        FROM   camarilla_watchlist
        WHERE  week_start = ? AND status = 'WATCHING'
        GROUP  BY direction
    ''', (week_start,))
    rows   = {r['direction']: r['cnt'] for r in c.fetchall()}
    bull_n = rows.get('BULLISH', 0)
    bear_n = rows.get('BEARISH', 0)
    ist    = get_ist().strftime('%d %b %Y %H:%M IST')

    msg = f"""🌅 <b>CAMARILLA MORNING BRIEFING</b>
⏰ {ist}  |  Week: {week_start}

👁 Watching:  🟢 {bull_n} Bullish  |  🔴 {bear_n} Bearish
⚡ Live monitor starts at 9:15 AM IST"""
    send_telegram(msg)

# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════
def main():
    log("=" * 60)
    log(f"Camarilla Scanner — {get_ist().strftime('%d %b %Y %H:%M IST')}")
    log("=" * 60)

    conn       = get_db()
    init_tables(conn)

    today      = date.today()
    week_start = get_week_start(today)
    is_monday  = (today.weekday() == 0)

    log(f"Today: {today}  |  Week start: {week_start}  |  Monday: {is_monday}")

    if is_monday:
        # ── Monday: full rebuild ──────────────────────────────
        log("MONDAY — Full weekly build")

        # 1. Expire last week's watchlist entries
        expire_old_watchlist(conn, week_start)

        # 2. Compute fresh weekly Camarilla levels for all 794 stocks
        levels = compute_weekly_levels(conn, week_start)

        # 3. Apply filters → get bullish + bearish candidates
        bullish, bearish = apply_filters(conn, week_start, levels)

        # 4. Save to watchlist
        save_watchlist(conn, bullish + bearish, week_start)

        # 5. Send full Telegram watchlist
        log(f"Sending Monday watchlist: {len(bullish)} bull | {len(bearish)} bear")
        send_weekly_watchlist(bullish, bearish, week_start)

    else:
        # ── Tue-Fri: daily refresh ────────────────────────────
        log(f"{'TUESDAY' if today.weekday()==1 else 'WEEKDAY'} — Daily refresh")

        # 1. Load this week's levels (already computed on Monday)
        c = conn.cursor()
        c.execute('''
            SELECT * FROM weekly_camarilla WHERE week_start = ?
        ''', (week_start,))
        rows   = c.fetchall()
        levels = {r['symbol']: {
            'h1': r['h1'], 'h2': r['h2'], 'h3': r['h3'],
            'h4': r['h4'], 'h5': r['h5'],
            'l1': r['l1'], 'l2': r['l2'], 'l3': r['l3'],
            'l4': r['l4'], 'l5': r['l5'],
            'prev_close': r['prev_close'],
            'range_pct':  r['range_pct'],
        } for r in rows}

        if not levels:
            # Edge case: levels missing (e.g. VM was off on Monday)
            log("Weekly levels not found — computing now...")
            levels = compute_weekly_levels(conn, week_start)

        # 2. Apply filters with today's prices
        bullish, bearish = apply_filters(conn, week_start, levels)

        # 3. Find newly qualified stocks (not already in watchlist)
        c.execute('''
            SELECT symbol, direction FROM camarilla_watchlist
            WHERE  week_start = ? AND status IN ('WATCHING','TRIGGERED')
        ''', (week_start,))
        existing = {(r['symbol'], r['direction']) for r in c.fetchall()}

        new_bull = [s for s in bullish
                    if (s['symbol'], 'BULLISH') not in existing]
        new_bear = [s for s in bearish
                    if (s['symbol'], 'BEARISH') not in existing]

        # 4. Save newly qualified stocks
        if new_bull or new_bear:
            save_watchlist(conn, new_bull + new_bear, week_start)
            log(f"New additions: {len(new_bull)} bull | {len(new_bear)} bear")

        # 5. Refresh — expire failed setups
        expired = refresh_watchlist(conn, week_start)

        # 6. Send Telegram only if changes occurred
        send_daily_update(new_bull, new_bear, expired)

        # 7. Morning summary (always)
        send_watchlist_summary(conn, week_start)

    conn.close()
    log("Camarilla Scanner complete.")

if __name__ == '__main__':
    main()
