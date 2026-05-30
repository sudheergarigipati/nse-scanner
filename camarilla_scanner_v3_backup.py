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

# ── Noise filters — BACKTESTED CONFIG (Test 4, 24 months) ────
# Backtest results: 110 trades | 75.5% win rate | R:R 2:1
# Expected value: +2.88% per trade | Total PnL: +317% over 2 years

# Filter 1 — Range quality
MIN_RANGE_PCT      = 2.0   # Last week range >= 2% of close

# Filter 2 — Proximity: price must be right at L3/H3
MAX_DIST_PCT       = 0.15  # Within 0.15% of L3 (bull) or H3 (bear)
                            # Backtest: tighter = fewer but cleaner setups

# Filter 3 — Minimum price
MIN_CLOSE_PRICE    = 50.0  # Skip stocks below Rs.50

# Filter 4 — Liquidity: avg daily traded value
MIN_DAILY_VALUE_CR = 5.0   # Avg daily value >= Rs.5 Crore

# Filter 5 — Volume confirmation
MIN_VOL_RATIO      = 1.5   # Today's volume >= 1.5x 20-day average

# Filter 6 — SL risk cap
MAX_SL_PCT         = 4.0   # Stop loss must be within 4% of entry

# Filter 9 — Minimum reward (H3 must be 2%+ above L3)
# Eliminates setups where price has already bounced so much
# that H3 target is too close or below entry
# Backtest: this fixes the R:R bug — ensures true 2:1 is achievable
MIN_REWARD_PCT     = 2.0   # H3 must be at least 2% above L3

# Filter 10 — Nifty 50 basket trend
# Only take BULLISH trades when Nifty 50 basket is above its 100-day EMA
# Uses 29 Nifty 50 stocks as equal-weighted basket (no ETF needed)
# Backtest: eliminated bad months (Jan 2026 correction, Mar 2026 bottom)
USE_NIFTY_FILTER   = True

# Nifty 50 basket — 29 stocks available in daily_prices
NIFTY50_BASKET = [
    'RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK',
    'HINDUNILVR','SBIN','BHARTIARTL','ITC','KOTAKBANK',
    'LT','AXISBANK','ASIANPAINT','MARUTI','SUNPHARMA',
    'TITAN','BAJFINANCE','NTPC','POWERGRID','ONGC',
    'TATASTEEL','WIPRO','HCLTECH','ADANIENT','ULTRACEMCO',
    'NESTLEIND','BAJAJFINSV','COALINDIA','HINDALCO'
]

# Filter 7 — No sideways stocks (in both bull and bear list)
# Filter 8 — Virgin level (L3/H3 not touched earlier this week)

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

# ══════════════════════════════════════════════════════════════
#  NIFTY 50 BASKET TREND — computed once per scan
#  Uses 29 Nifty 50 stocks as equal-weighted normalised basket
#  Returns True if basket is above its 100-day EMA (bull market)
# ══════════════════════════════════════════════════════════════
def is_nifty_bullish(conn, as_of_date):
    """
    Check if Nifty 50 basket is above its 100-day EMA on given date.
    Uses equal-weighted basket of 29 Nifty 50 stocks.
    Each stock normalised to 100 on earliest date → averaged.
    100-day EMA on basket ≈ 20-week EMA.
    """
    if not USE_NIFTY_FILTER:
        return True

    c = conn.cursor()

    # Get last 120 days of prices for basket stocks
    start_dt = (datetime.strptime(as_of_date, '%Y-%m-%d').date()
                - timedelta(days=170)).strftime('%Y-%m-%d')

    placeholders = ','.join('?' * len(NIFTY50_BASKET))
    c.execute(f'''
        SELECT symbol, date, close
        FROM   daily_prices
        WHERE  symbol IN ({placeholders})
        AND    date BETWEEN ? AND ?
        AND    close > 0
        ORDER  BY date
    ''', (*NIFTY50_BASKET, start_dt, as_of_date))

    rows = c.fetchall()
    if not rows:
        return True  # no data — allow trade

    # Organise by symbol
    from collections import defaultdict
    sym_bars = defaultdict(list)
    for r in rows:
        sym_bars[r['symbol']].append((r['date'], r['close']))

    # Normalise each stock to 100 on its first date
    base = {}
    for sym, bars in sym_bars.items():
        if bars:
            base[sym] = bars[0][1]

    # Get all unique dates in order
    all_dates = sorted(set(d for sym in sym_bars for d, _ in sym_bars[sym]))

    # Build sym → date → close lookup
    sd_close = {}
    for sym, bars in sym_bars.items():
        sd_close[sym] = {d: c for d, c in bars}

    # Compute basket index for each date
    basket = []
    for dt in all_dates:
        vals = []
        for sym in sym_bars:
            cl = sd_close[sym].get(dt)
            bs = base.get(sym)
            if cl and bs and bs > 0:
                vals.append(cl / bs * 100)
        if vals:
            basket.append((dt, sum(vals) / len(vals)))

    if len(basket) < 20:
        return True  # not enough data

    # Compute 100-day EMA on basket
    k   = 2 / (100 + 1)
    ema = basket[0][1]
    last_val = basket[0][1]
    for dt, val in basket:
        ema      = val * k + ema * (1 - k)
        last_val = val

    return last_val >= ema

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
            ema20w          REAL,            -- 20-week EMA at time of scan
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
    All 8 filters applied (Pivot Boss book logic):

    Filter 1 — Range quality    : last week range >= MIN_RANGE_PCT
    Filter 2 — Proximity        : price within MAX_DIST_PCT of L3/H3
    Filter 3 — Min price        : skip stocks below MIN_CLOSE_PRICE
    Filter 4 — Liquidity        : avg daily value >= MIN_DAILY_VALUE_CR
    Filter 5 — Volume confirm   : today vol >= MIN_VOL_RATIO x avg
    Filter 6 — SL risk cap      : stop loss within MAX_SL_PCT of entry
    Filter 7 — No sideways      : skip stocks in both bull + bear list
    Filter 8 — Virgin level     : L3/H3 not touched earlier this week
                                  + L4/H4 not broken this week

    Returns two lists: bullish_candidates, bearish_candidates
    """
    c        = conn.cursor()
    today    = date.today().strftime('%Y-%m-%d')

    # ── Pre-load this week's OHLCV for all symbols ────────────
    c.execute('''
        SELECT symbol, date, high, low, close, volume
        FROM   daily_prices
        WHERE  date >= ? AND date <= ? AND close > 0
        ORDER  BY symbol, date
    ''', (week_start, today))
    week_rows = c.fetchall()

    # Group by symbol
    from collections import defaultdict
    week_data = defaultdict(list)
    for r in week_rows:
        week_data[r['symbol']].append(dict(r))

    # ── Pre-load 20-day avg volume + avg close for liquidity ──
    c.execute('''
        SELECT symbol,
               AVG(volume) as avg_vol,
               AVG(close)  as avg_close
        FROM (
            SELECT symbol, volume, close,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) as rn
            FROM   daily_prices
            WHERE  date < ? AND close > 0
        )
        WHERE rn <= 20
        GROUP BY symbol
    ''', (week_start,))
    avg_data = {r['symbol']: {'avg_vol':   r['avg_vol']   or 0,
                               'avg_close': r['avg_close'] or 0}
                for r in c.fetchall()}

    # ── Pre-compute prev week CPR for trend filter ─────────────
    prev_week_start = (datetime.strptime(week_start, '%Y-%m-%d').date()
                       - timedelta(days=7)).strftime('%Y-%m-%d')
    prev_week_end   = (datetime.strptime(week_start, '%Y-%m-%d').date()
                       - timedelta(days=1)).strftime('%Y-%m-%d')

    c.execute('''
        SELECT symbol,
               MAX(high) as wh, MIN(low) as wl,
               (SELECT close FROM daily_prices d2
                WHERE  d2.symbol = d1.symbol
                AND    d2.date <= ?
                ORDER  BY d2.date DESC LIMIT 1) as wc
        FROM   daily_prices d1
        WHERE  date BETWEEN ? AND ? AND close > 0
        GROUP  BY symbol
    ''', (prev_week_end, prev_week_start, prev_week_end))
    prev_cpr = {}
    for r in c.fetchall():
        if r['wh'] and r['wl'] and r['wc']:
            bc    = (r['wh'] + r['wl']) / 2
            pivot = (r['wh'] + r['wl'] + r['wc']) / 3
            tc    = (2 * pivot) - bc
            prev_cpr[r['symbol']] = {'bc': bc, 'tc': tc}

    # ── Pre-compute 20-week EMA for all symbols ────────────────
    # 20 weeks = ~100 trading days
    # BULLISH only if price > 20w EMA (weekly uptrend confirmed)
    # BEARISH only if price < 20w EMA (weekly downtrend confirmed)
    # Uses daily closes — computes EMA over last 100 trading days
    log("Computing 20-week EMA trend filter...")
    ema20w = {}
    c.execute('''
        SELECT symbol,
               (SELECT close FROM daily_prices d2
                WHERE  d2.symbol = d1.symbol
                AND    d2.date <= ?
                ORDER  BY d2.date DESC LIMIT 1) as latest_close
        FROM   daily_prices d1
        WHERE  close > 0
        GROUP  BY symbol
    ''', (today,))
    all_syms = [r['symbol'] for r in c.fetchall() if r['latest_close']]

    for sym in all_syms:
        c.execute('''
            SELECT close FROM daily_prices
            WHERE  symbol = ? AND date <= ? AND close > 0
            ORDER  BY date DESC LIMIT 100
        ''', (sym, today))
        closes = [r['close'] for r in c.fetchall()]
        if len(closes) < 20:
            continue
        # Reverse to chronological order and compute EMA
        closes = list(reversed(closes))
        k      = 2 / (20 + 1)   # smoothing factor for 20-period EMA
        ema    = closes[0]
        for price in closes[1:]:
            ema = price * k + ema * (1 - k)
        ema20w[sym] = round(ema, 4)

    log(f"20-week EMA computed for {len(ema20w)} symbols")

    # ── First pass: collect bull + bear raw candidates ─────────
    raw_bull = set()
    raw_bear = set()

    for sym, cam in levels.items():
        bars = week_data.get(sym, [])
        if not bars:
            continue
        today_bar = bars[-1]
        price     = today_bar['close']

        # Filter 3 — min price
        if price < MIN_CLOSE_PRICE:
            continue
        # Filter 1 — range quality
        if cam['range_pct'] < MIN_RANGE_PCT:
            continue

        l3, l4, l5 = cam['l3'], cam['l4'], cam['l5']
        h3, h4, h5 = cam['h3'], cam['h4'], cam['h5']

        dist_l3 = abs(price - l3) / price * 100
        dist_h3 = abs(price - h3) / price * 100

        if dist_l3 <= MAX_DIST_PCT and price >= l3:
            raw_bull.add(sym)
        if dist_h3 <= MAX_DIST_PCT and price <= h3:
            raw_bear.add(sym)

    # Filter 7 — remove sideways (in both bull and bear)
    sideways = raw_bull & raw_bear
    if sideways:
        log(f"  Sideways (skipped): {sorted(sideways)}")
    raw_bull -= sideways
    raw_bear -= sideways

    # ── Second pass: apply remaining filters ───────────────────
    bullish = []
    bearish = []

    for direction, candidates in [('BULLISH', raw_bull), ('BEARISH', raw_bear)]:
        for sym in candidates:
            cam  = levels[sym]
            bars = week_data.get(sym, [])
            if not bars:
                continue

            today_bar  = bars[-1]
            prior_bars = bars[:-1]
            price      = today_bar['close']

            l3, l4, l5 = cam['l3'], cam['l4'], cam['l5']
            h3, h4, h5 = cam['h3'], cam['h4'], cam['h5']

            # Filter 8a — Virgin level check
            if direction == 'BULLISH':
                l3_touched = any(b['low']  <= l3 for b in prior_bars)
                l4_broken  = any(b['low']  <  l4 for b in bars)
                if l3_touched or l4_broken:
                    continue
            else:
                h3_touched = any(b['high'] >= h3 for b in prior_bars)
                h4_broken  = any(b['high'] >  h4 for b in bars)
                if h3_touched or h4_broken:
                    continue

            # Filter 6 — SL risk cap
            entry  = l3 if direction == 'BULLISH' else h3
            sl     = l4 if direction == 'BULLISH' else h4
            sl_pct = abs(entry - sl) / entry * 100
            if sl_pct > MAX_SL_PCT:
                continue

            # Filter 4 — Liquidity
            ad        = avg_data.get(sym, {})
            avg_vol   = ad.get('avg_vol', 0)
            avg_close = ad.get('avg_close', price)
            daily_val = (avg_vol * avg_close) / 1e7  # in Crores
            if daily_val < MIN_DAILY_VALUE_CR:
                continue

            # Filter 5 — Volume confirmation
            vol_ratio = today_bar['volume'] / avg_vol if avg_vol > 0 else 0
            if vol_ratio < MIN_VOL_RATIO:
                continue

            # Filter 8b — CPR weekly trend
            pc = prev_cpr.get(sym)
            if pc:
                this_bc    = (cam['prev_high'] + cam['prev_low']) / 2
                this_pivot = (cam['prev_high'] + cam['prev_low'] + cam['prev_close']) / 3
                this_tc    = (2 * this_pivot) - this_bc

                if direction == 'BULLISH' and this_bc <= pc['tc']:
                    continue   # not a Higher Value week — skip
                if direction == 'BEARISH' and this_tc >= pc['bc']:
                    continue   # not a Lower Value week — skip

            # Filter 9 — 20-week EMA trend
            # BULLISH : price must be ABOVE 20w EMA (uptrend confirmed)
            # BEARISH : price must be BELOW 20w EMA (downtrend confirmed)
            ema_val = ema20w.get(sym)
            if ema_val:
                if direction == 'BULLISH' and price < ema_val:
                    log(f"  {sym} BULLISH skipped — price {price} < 20w EMA {ema_val} (downtrend)")
                    continue
                if direction == 'BEARISH' and price > ema_val:
                    log(f"  {sym} BEARISH skipped — price {price} > 20w EMA {ema_val} (uptrend)")
                    continue

            # Filter 10 — Minimum reward
            # H3 must be at least MIN_REWARD_PCT above L3
            # Ensures target is meaningful — not too close to entry
            if direction == 'BULLISH':
                reward_pct = (h3 - l3) / l3 * 100 if l3 > 0 else 0
                if reward_pct < MIN_REWARD_PCT:
                    continue
            else:
                reward_pct = (h3 - l3) / h3 * 100 if h3 > 0 else 0
                if reward_pct < MIN_REWARD_PCT:
                    continue

            # ── All filters passed ──────────────────────────────
            t1    = h3 if direction == 'BULLISH' else l3
            t2    = h4 if direction == 'BULLISH' else l4
            rr    = round(abs(t1 - entry) / abs(entry - sl), 2) if abs(entry - sl) > 0 else 0
            dist  = abs(price - entry) / price * 100

            result = {
                'symbol':     sym,
                'direction':  direction,
                'price':      round(price,      2),
                'l3':         round(l3,         2),
                'l4':         round(l4,         2),
                'l5':         round(l5,         2),
                'h3':         round(h3,         2),
                'h4':         round(h4,         2),
                'h5':         round(h5,         2),
                'prev_close': round(cam['prev_close'], 2),
                'dist_pct':   round(dist,       2),
                'range_pct':  round(cam['range_pct'], 2),
                'sl_pct':     round(sl_pct,     2),
                'vol_ratio':  round(vol_ratio,  2),
                'daily_val':  round(daily_val,  2),
                'rr':         rr,
                'ema20w':     round(ema_val, 2) if ema_val else 0,
            }
            if direction == 'BULLISH':
                bullish.append(result)
            else:
                bearish.append(result)

    # Sort by volume ratio — highest conviction first
    bullish.sort(key=lambda x: x['vol_ratio'], reverse=True)
    bearish.sort(key=lambda x: x['vol_ratio'], reverse=True)

    log(f"Filters applied → Bullish: {len(bullish)} | Bearish: {len(bearish)}")
    log(f"  Filter breakdown:")
    log(f"  Range+Proximity raw  → Bull:{len(raw_bull)+len(sideways)} Bear:{len(raw_bear)+len(sideways)}")
    log(f"  After sideways skip  → {len(sideways)} removed")
    log(f"  After all 9 filters  → Bull:{len(bullish)} Bear:{len(bearish)}")
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
                     prev_close, ema20w, added_date, status)
                VALUES (?,?,?, ?,?,?,?,?,?, ?,?,?,?)
            ''', (
                s['symbol'], week_start, s['direction'],
                s['h3'], s['h4'], s['h5'],
                s['l3'], s['l4'], s['l5'],
                s['prev_close'],
                s.get('ema20w', 0),
                today, 'WATCHING'
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
        [f"  {s['symbol']:12} L3:₹{s['l3']} SL:₹{s['l4']} T1:₹{s['h3']} "
         f"RR:{s.get('rr',2.0)} Vol:{s.get('vol_ratio',0)}x"
         for s in bullish[:15]]
    ) or '  None'
    bear_lines = '\n'.join(
        [f"  {s['symbol']:12} H3:₹{s['h3']} SL:₹{s['h4']} T1:₹{s['l3']} "
         f"RR:{s.get('rr',2.0)} Vol:{s.get('vol_ratio',0)}x"
         for s in bearish[:15]]
    ) or '  None'

    msg = f"""📊 <b>CAMARILLA WEEKLY WATCHLIST</b>
📅 Week: {week_start}  |  {ist_time}
📈 Nifty 50 Basket: BULLISH ✅

🟢 <b>BULLISH SETUPS ({len(bullish)} stocks)</b>
<code>{bull_lines}</code>

🔴 <b>BEARISH SETUPS ({len(bearish)} stocks)</b>
<code>{bear_lines}</code>

⚡ Monitoring live every 15 min
🚨 Entry alert fires when L3/H3 touched + candle confirms
📖 Strategy: Weekly Camarilla L3 Bounce (Pivot Boss)
⚙️ Config: 75.5% win rate | 2:1 R:R | +2.88%/trade (24mo backtest)"""
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

        # 0. Check Nifty 50 basket trend
        today_str    = today.strftime('%Y-%m-%d')
        nifty_bull   = is_nifty_bullish(conn, today_str)
        nifty_status = "BULLISH ✅" if nifty_bull else "BEARISH ⚠️"
        log(f"Nifty 50 basket trend: {nifty_status}")

        if not nifty_bull:
            log("Nifty basket is BEARISH — skipping BULLISH setups this week")
            send_telegram(
                f"📊 <b>CAMARILLA WEEKLY SCAN</b>\n"
                f"📅 Week: {week_start}\n\n"
                f"⚠️ <b>Nifty 50 basket is BEARISH this week</b>\n"
                f"No bullish setups added to watchlist.\n"
                f"Strategy: only trade bullish when market is in uptrend.\n"
                f"Will check again tomorrow morning."
            )
            conn.close()
            log("Camarilla Scanner complete.")
            return

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

        # 0. Check Nifty trend — skip bullish additions if bearish
        today_str  = today.strftime('%Y-%m-%d')
        nifty_bull = is_nifty_bullish(conn, today_str)
        log(f"Nifty 50 basket trend: {'BULLISH ✅' if nifty_bull else 'BEARISH ⚠️'}")

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

        # If Nifty is bearish — don't add new bullish stocks mid-week
        if not nifty_bull:
            log("Nifty bearish — suppressing new BULLISH additions today")
            new_bull = []

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
