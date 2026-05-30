#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  camarilla_options_backtest.py
#  Backtests Daily Camarilla L4/H4 Breakout for top 10 liquid
#  Nifty 50 stocks — simulated as intraday options trades
#
#  Setup:
#    CALL : Price closes above H4 on 15-min candle → Buy ATM CALL
#    PUT  : Price closes below L4 on 15-min candle → Buy ATM PUT
#
#  Simulation (using daily data as proxy):
#    Signal  : Daily close breaks L4 (PUT) or H4 (CALL)
#    Entry   : Next day open (simulate buying option at open)
#    Target  : L5 (PUT) or H5 (CALL) hit intraday
#    SL      : Close back above L4 (PUT) or below H4 (CALL)
#    Time SL : End of day exit if neither target nor SL hit
#
#  Filters:
#    1. Virgin L4/H4 — not broken earlier this week
#    2. Volume >= 2x average on signal day
#    3. 20-day EMA trend confirms direction
#    4. No gap beyond L4/H4 at open (entered during session)
#    5. Minimum range quality (ATR >= 1% of price)
#
#  Usage:
#    nohup python3 camarilla_options_backtest.py > logs/options_backtest.log 2>&1 &
#    cat options_backtest_results.txt
# ═══════════════════════════════════════════════════════════════

import sqlite3
import os
from datetime import date, timedelta, datetime
from collections import defaultdict

BASE_DIR    = os.path.expanduser('~/nse-scanner')
DB_PATH     = os.path.join(BASE_DIR, 'nse_data.db')
OUTPUT_FILE = os.path.join(BASE_DIR, 'options_backtest_results.txt')

# ── Top 10 most liquid Nifty 50 stock options ─────────────────
STOCKS = [
    'RELIANCE', 'HDFCBANK', 'ICICIBANK', 'SBIN',   'INFY',
    'TCS',      'AXISBANK', 'BAJFINANCE','KOTAKBANK','BHARTIARTL'
]

# ── Config ────────────────────────────────────────────────────
MONTHS         = 24     # backtest period
MIN_VOL_RATIO  = 2.0    # volume must be 2x average on signal day
MIN_ATR_PCT    = 1.0    # minimum daily range as % of price
MAX_GAP_PCT    = 0.3    # max gap allowed at open beyond L4/H4

# Options simulation parameters
# Approximate delta of ATM option = 0.50
# Option premium moves ~0.50 per 1 point stock move
# We simulate % move in stock and apply delta factor
OPTION_DELTA   = 0.50   # ATM option delta
OPTION_THETA   = 0.15   # daily theta decay as % of premium
WIN_MULTIPLIER = 2.5    # avg option gain multiple when target hit
LOSS_MULTIPLIER= 0.45   # avg option loss multiple when SL hit

def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def compute_camarilla(high, low, close):
    rng = high - low
    h3  = close + rng * 1.1 / 4
    h4  = close + rng * 1.1 / 2
    h5  = (high / low) * close if low > 0 else 0
    l3  = close - rng * 1.1 / 4
    l4  = close - rng * 1.1 / 2
    l5  = close - (h5 - close) if h5 > 0 else 0
    return {'h3':h3,'h4':h4,'h5':h5,'l3':l3,'l4':l4,'l5':l5}

def compute_ema(closes, period=20):
    if len(closes) < period:
        return None
    k   = 2 / (period + 1)
    ema = closes[0]
    for p in closes[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def get_week_start(date_str):
    d = datetime.strptime(date_str, '%Y-%m-%d').date()
    return (d - timedelta(days=d.weekday())).strftime('%Y-%m-%d')

# ═══════════════════════════════════════════════════════════════
#  LOAD DATA
# ═══════════════════════════════════════════════════════════════
def load_data(start_date):
    log("Loading price data...")
    conn = get_db()
    c    = conn.cursor()
    ph   = ','.join('?' * len(STOCKS))
    c.execute(f'''
        SELECT symbol, date, open, high, low, close, volume
        FROM   daily_prices
        WHERE  symbol IN ({ph})
        AND    date >= ?
        ORDER  BY symbol, date
    ''', (*STOCKS, (start_date - timedelta(days=60)).strftime('%Y-%m-%d')))
    rows = c.fetchall()
    conn.close()

    price_data = defaultdict(list)
    for r in rows:
        price_data[r['symbol']].append({
            'date':   r['date'],
            'open':   r['open'],
            'high':   r['high'],
            'low':    r['low'],
            'close':  r['close'],
            'volume': r['volume'],
        })

    date_idx = {}
    for sym, bars in price_data.items():
        date_idx[sym] = {b['date']: i for i, b in enumerate(bars)}

    log(f"Loaded {len(price_data)} symbols")
    return price_data, date_idx

# ═══════════════════════════════════════════════════════════════
#  BACKTEST
# ═══════════════════════════════════════════════════════════════
def run_backtest(price_data, date_idx, start_date, end_date):
    all_trades  = []
    daily_signals = defaultdict(list)  # date → signals

    for sym in STOCKS:
        bars = price_data.get(sym, [])
        if not bars:
            log(f"  {sym}: no data")
            continue

        log(f"  Processing {sym} ({len(bars)} bars)...")

        for i in range(21, len(bars) - 1):
            bar      = bars[i]
            bar_date = bar['date']

            # Only backtest within our period
            if bar_date < start_date.strftime('%Y-%m-%d'):
                continue
            if bar_date > end_date.strftime('%Y-%m-%d'):
                break

            prev_bar = bars[i - 1]

            # Compute daily Camarilla from previous day
            cam = compute_camarilla(
                prev_bar['high'],
                prev_bar['low'],
                prev_bar['close']
            )
            h4 = cam['h4']; h5 = cam['h5']
            l4 = cam['l4']; l5 = cam['l5']

            # ── Filter 1: Range quality (ATR) ─────────────────
            recent = bars[max(0, i-20):i]
            avg_range = sum(b['high']-b['low'] for b in recent) / len(recent)
            atr_pct   = avg_range / prev_bar['close'] * 100
            if atr_pct < MIN_ATR_PCT:
                continue

            # ── Filter 2: Volume check ─────────────────────────
            avg_vol   = sum(b['volume'] for b in recent) / len(recent)
            vol_ratio = bar['volume'] / avg_vol if avg_vol > 0 else 0
            if vol_ratio < MIN_VOL_RATIO:
                continue

            # ── Filter 3: 20-day EMA trend ─────────────────────
            ema20 = compute_ema([b['close'] for b in recent], 20)
            if ema20 is None:
                continue

            # ── Filter 4: Virgin L4/H4 this week ──────────────
            week_start = get_week_start(bar_date)
            week_bars  = [b for b in bars
                          if week_start <= b['date'] < bar_date]
            l4_broken_before = any(b['close'] < l4 for b in week_bars)
            h4_broken_before = any(b['close'] > h4 for b in week_bars)

            # ── Check CALL signal (H4 breakout) ───────────────
            if (bar['close'] > h4              # closes above H4
                    and bar['open'] < h4        # didn't gap above H4
                    and not h4_broken_before    # virgin level
                    and prev_bar['close'] < ema20  # was below EMA... no
                    ):
                pass  # will check EMA below

            # CALL: uptrend — price above 20-day EMA going into break
            call_signal = (
                bar['close']  >  h4
                and bar['open']   <  h4              # no gap
                and not h4_broken_before             # virgin H4
                and prev_bar['close'] > ema20 * 0.98 # trending up (allow 2% tolerance)
            )

            # PUT: downtrend — price below 20-day EMA going into break
            put_signal = (
                bar['close']  <  l4
                and bar['open']   >  l4              # no gap
                and not l4_broken_before             # virgin L4
                and prev_bar['close'] < ema20 * 1.02 # trending down (allow 2% tolerance)
            )

            if not call_signal and not put_signal:
                continue

            # ── Simulate next day trade ────────────────────────
            if i + 1 >= len(bars):
                continue

            next_bar   = bars[i + 1]
            direction  = 'CALL' if call_signal else 'PUT'

            # Entry at next day open
            entry_price = next_bar['open']

            # Gap filter — if next day already gapped beyond target
            if direction == 'CALL':
                if next_bar['open'] > h5:
                    continue  # gapped above target — missed
                target_hit = next_bar['high']  >= h5
                sl_hit     = next_bar['close'] < h4
                target_pct = (h5 - entry_price) / entry_price * 100
                sl_pct     = (h4 - entry_price) / entry_price * 100
            else:
                if next_bar['open'] < l5:
                    continue  # gapped below target — missed
                target_hit = next_bar['low']   <= l5
                sl_hit     = next_bar['close'] > l4
                target_pct = (l5 - entry_price) / entry_price * 100
                sl_pct     = (l4 - entry_price) / entry_price * 100

            # Determine outcome
            if target_hit:
                outcome        = 'TARGET'
                stock_pct      = round(abs(target_pct), 2)
                option_pct     = round(stock_pct * WIN_MULTIPLIER, 1)
            elif sl_hit:
                outcome        = 'SL_HIT'
                stock_pct      = round(abs(sl_pct), 2)
                option_pct     = round(-stock_pct * (1/OPTION_DELTA) * LOSS_MULTIPLIER, 1)
            else:
                outcome        = 'TIME_EXIT'
                # Exit at close — partial move
                if direction == 'CALL':
                    stock_pct  = round((next_bar['close'] - entry_price) / entry_price * 100, 2)
                else:
                    stock_pct  = round((entry_price - next_bar['close']) / entry_price * 100, 2)
                option_pct     = round(stock_pct * OPTION_DELTA * 100 / entry_price, 1)
                # Apply theta decay for time exit
                option_pct     = round(option_pct - OPTION_THETA * 100, 1)

            all_trades.append({
                'symbol':      sym,
                'signal_date': bar_date,
                'entry_date':  next_bar['date'],
                'direction':   direction,
                'signal_close': round(bar['close'], 2),
                'entry_price': round(entry_price, 2),
                'h4':          round(h4, 2),
                'l4':          round(l4, 2),
                'h5':          round(h5, 2),
                'l5':          round(l5, 2),
                'outcome':     outcome,
                'stock_pct':   stock_pct,
                'option_pct':  option_pct,
                'vol_ratio':   round(vol_ratio, 2),
                'atr_pct':     round(atr_pct, 2),
            })

            daily_signals[bar_date].append(sym)

    return all_trades, daily_signals

# ═══════════════════════════════════════════════════════════════
#  ANALYSIS
# ═══════════════════════════════════════════════════════════════
def analyse(all_trades, daily_signals, months):
    if not all_trades:
        return "No trades found.", {}

    total    = len(all_trades)
    wins     = [t for t in all_trades if t['outcome'] == 'TARGET']
    losses   = [t for t in all_trades if t['outcome'] == 'SL_HIT']
    timeex   = [t for t in all_trades if t['outcome'] == 'TIME_EXIT']

    win_rate  = round(len(wins)   / total * 100, 1) if total else 0
    loss_rate = round(len(losses) / total * 100, 1) if total else 0
    time_rate = round(len(timeex) / total * 100, 1) if total else 0

    avg_win_opt  = round(sum(t['option_pct'] for t in wins)   / len(wins),   1) if wins   else 0
    avg_loss_opt = round(sum(t['option_pct'] for t in losses) / len(losses), 1) if losses else 0
    avg_time_opt = round(sum(t['option_pct'] for t in timeex) / len(timeex), 1) if timeex else 0
    total_opt    = round(sum(t['option_pct'] for t in all_trades), 1)
    exp_val      = round(total_opt / total, 1) if total else 0

    # By direction
    calls = [t for t in all_trades if t['direction'] == 'CALL']
    puts  = [t for t in all_trades if t['direction'] == 'PUT']
    call_wr = round(sum(1 for t in calls if t['outcome']=='TARGET') / len(calls) * 100, 1) if calls else 0
    put_wr  = round(sum(1 for t in puts  if t['outcome']=='TARGET') / len(puts)  * 100, 1) if puts  else 0

    # By stock
    by_stock = defaultdict(list)
    for t in all_trades:
        by_stock[t['symbol']].append(t)

    # Daily signal count distribution
    sig_counts = [len(v) for v in daily_signals.values()]
    days_with_signals = len(daily_signals)
    avg_daily = round(sum(sig_counts) / days_with_signals, 1) if days_with_signals else 0
    days_zero  = (months * 21) - days_with_signals  # approx trading days

    # Monthly breakdown
    monthly = defaultdict(list)
    for t in all_trades:
        monthly[t['signal_date'][:7]].append(t)

    lines = []
    lines.append(f"\n{'═'*70}")
    lines.append(f"  DAILY CAMARILLA L4/H4 BREAKOUT — OPTIONS BACKTEST")
    lines.append(f"  Stocks  : {', '.join(STOCKS)}")
    lines.append(f"  Period  : {months} months")
    lines.append(f"  Entry   : Next day open after L4/H4 close break")
    lines.append(f"  Target  : L5 (PUT) / H5 (CALL)")
    lines.append(f"  SL      : Close back above L4 / below H4")
    lines.append(f"  Options : ATM delta ~0.50, monthly expiry")
    lines.append(f"{'═'*70}")

    lines.append(f"\n  SIGNAL FREQUENCY")
    lines.append(f"  {'Total signals':<35}: {total}")
    lines.append(f"  {'Days with signals':<35}: {days_with_signals}")
    lines.append(f"  {'Avg signals per day':<35}: {avg_daily}")
    lines.append(f"  {'CALL signals':<35}: {len(calls)}")
    lines.append(f"  {'PUT signals':<35}: {len(puts)}")

    lines.append(f"\n  TRADE OUTCOMES")
    lines.append(f"  {'Total trades':<35}: {total}")
    lines.append(f"  {'✅ Target hit':<35}: {len(wins)} ({win_rate}%)")
    lines.append(f"  {'❌ SL hit':<35}: {len(losses)} ({loss_rate}%)")
    lines.append(f"  {'⏱  Time exit':<35}: {len(timeex)} ({time_rate}%)")
    lines.append(f"  {'CALL win rate':<35}: {call_wr}%")
    lines.append(f"  {'PUT win rate':<35}: {put_wr}%")

    lines.append(f"\n  OPTIONS P&L (simulated)")
    lines.append(f"  {'Avg win (option %)':<35}: +{avg_win_opt}%")
    lines.append(f"  {'Avg loss (option %)':<35}: {avg_loss_opt}%")
    lines.append(f"  {'Avg time exit (option %)':<35}: {avg_time_opt}%")
    lines.append(f"  {'Expected value per trade':<35}: {exp_val:+.1f}%")
    lines.append(f"  {'Total cumulative option PnL':<35}: {total_opt:+.1f}%")

    lines.append(f"\n  PER STOCK BREAKDOWN")
    lines.append(f"  {'Symbol':<14} {'Trades':>7} {'Wins':>6} {'Win%':>6} {'Avg Opt%':>10} {'Total%':>10}")
    lines.append(f"  {'-'*58}")
    for sym in STOCKS:
        st  = by_stock[sym]
        if not st:
            lines.append(f"  {sym:<14} {'no trades':>7}")
            continue
        sw  = sum(1 for t in st if t['outcome'] == 'TARGET')
        swr = round(sw / len(st) * 100, 1)
        sav = round(sum(t['option_pct'] for t in st) / len(st), 1)
        stot= round(sum(t['option_pct'] for t in st), 1)
        lines.append(f"  {sym:<14} {len(st):>7} {sw:>6} {swr:>5}% {sav:>+9.1f}% {stot:>+9.1f}%")

    lines.append(f"\n  MONTHLY BREAKDOWN")
    lines.append(f"  {'Month':<10} {'Trades':>7} {'Wins':>6} {'Win%':>6} {'Avg Opt%':>10} {'Signals/day':>13}")
    lines.append(f"  {'-'*58}")
    for month in sorted(monthly.keys()):
        mt   = monthly[month]
        mw   = sum(1 for t in mt if t['outcome'] == 'TARGET')
        mwr  = round(mw / len(mt) * 100, 1) if mt else 0
        mav  = round(sum(t['option_pct'] for t in mt) / len(mt), 1) if mt else 0
        # Signals per day this month
        month_days = {t['signal_date'] for t in mt}
        spd  = round(len(mt) / max(len(month_days), 1), 1)
        lines.append(f"  {month:<10} {len(mt):>7} {mw:>6} {mwr:>5}% {mav:>+9.1f}% {spd:>12.1f}")

    lines.append(f"\n  ALL TRADES")
    lines.append(f"  {'Symbol':<12} {'Date':<12} {'Dir':<6} {'Entry':>8} {'L4/H4':>8} "
                 f"{'L5/H5':>8} {'Stock%':>8} {'Opt%':>8} {'Vol':>6} Outcome")
    lines.append(f"  {'-'*90}")
    for t in sorted(all_trades, key=lambda x: x['signal_date']):
        out = ('✅ TARGET' if t['outcome'] == 'TARGET'
               else '❌ SL    ' if t['outcome'] == 'SL_HIT'
               else '⏱  TIME  ')
        level = t['h4'] if t['direction'] == 'CALL' else t['l4']
        target= t['h5'] if t['direction'] == 'CALL' else t['l5']
        lines.append(
            f"  {t['symbol']:<12} {t['signal_date']:<12} {t['direction']:<6} "
            f"{t['entry_price']:>8.2f} {level:>8.2f} {target:>8.2f} "
            f"{t['stock_pct']:>+7.2f}% {t['option_pct']:>+7.1f}% "
            f"{t['vol_ratio']:>5.1f}x {out}"
        )

    # Verdict
    lines.append(f"\n{'═'*70}")
    if exp_val >= 10 and win_rate >= 50:
        verdict = "✅ EXCELLENT — Strong options setup, trade with confidence"
    elif exp_val >= 5 and win_rate >= 45:
        verdict = "✅ GOOD — Profitable options setup"
    elif exp_val >= 0:
        verdict = "⚠️  MARGINAL — Slightly profitable, needs careful execution"
    else:
        verdict = "❌ UNPROFITABLE — Do not trade, review filters"

    lines.append(f"  VERDICT: {verdict}")
    lines.append(f"  Win rate    : {win_rate}%")
    lines.append(f"  Exp value   : {exp_val:+.1f}% per trade")
    lines.append(f"  Avg signals : {avg_daily} per day")
    lines.append(f"{'═'*70}\n")

    return '\n'.join(lines), {
        'total': total, 'win_rate': win_rate,
        'exp_val': exp_val, 'avg_daily': avg_daily,
        'call_wr': call_wr, 'put_wr': put_wr,
    }

# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
def main():
    os.makedirs(os.path.join(BASE_DIR, 'logs'), exist_ok=True)

    end_date   = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=MONTHS * 30)

    log("=" * 60)
    log("  DAILY CAMARILLA OPTIONS BACKTEST")
    log(f"  Stocks : {', '.join(STOCKS)}")
    log(f"  Period : {start_date} to {end_date} ({MONTHS} months)")
    log("=" * 60)

    price_data, date_idx = load_data(start_date)

    log("Running backtest...")
    all_trades, daily_signals = run_backtest(
        price_data, date_idx, start_date, end_date
    )

    log(f"Total trades found: {len(all_trades)}")

    report, summary = analyse(all_trades, daily_signals, MONTHS)

    log(f"\nSUMMARY:")
    log(f"  Trades      : {summary['total']}")
    log(f"  Win rate    : {summary['win_rate']}%")
    log(f"  Exp value   : {summary['exp_val']:+.1f}% per trade")
    log(f"  CALL win    : {summary['call_wr']}%")
    log(f"  PUT win     : {summary['put_wr']}%")
    log(f"  Avg/day     : {summary['avg_daily']} signals")

    with open(OUTPUT_FILE, 'w') as f:
        f.write(report)

    log(f"\n✅ Results saved to: {OUTPUT_FILE}")
    log(f"   View: cat {OUTPUT_FILE}")
    log(f"   Page: less {OUTPUT_FILE}")

if __name__ == '__main__':
    main()
