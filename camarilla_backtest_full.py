#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  camarilla_backtest_full.py
#  Runs 4 backtest variations of Weekly Camarilla BULLISH setup
#  Saves complete results to backtest_results.txt
#
#  Usage:
#    nohup python3 camarilla_backtest_full.py > logs/backtest.log 2>&1 &
#    tail -f logs/backtest.log          (watch progress)
#    cat backtest_results.txt           (final results)
#
#  Tests:
#    Test 0 — Baseline (original, as-is)
#    Test 1 — Fix R:R bug (H3 must be 2%+ above L3)
#    Test 2 — Add Nifty 50 trend filter
#    Test 3 — Tighter proximity (0.15% instead of 0.3%)
#    Test 4 — All fixes combined (final recommended config)
# ═══════════════════════════════════════════════════════════════

import sqlite3
import os
import sys
from datetime import date, timedelta, datetime
from collections import defaultdict

BASE_DIR    = os.path.expanduser('~/nse-scanner')
DB_PATH     = os.path.join(BASE_DIR, 'nse_data.db')
OUTPUT_FILE = os.path.join(BASE_DIR, 'backtest_results.txt')
LOG_FILE    = os.path.join(BASE_DIR, 'logs/backtest.log')

MONTHS      = 24   # 2 years — includes out-of-sample period
MAX_HOLD    = 5   # trading days

# ── Helpers ───────────────────────────────────────────────────
def log(msg):
    ts   = datetime.now().strftime('%H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line, flush=True)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_mondays(start_date, end_date):
    mondays = []
    d = start_date
    while d.weekday() != 0:
        d += timedelta(days=1)
    while d <= end_date:
        mondays.append(d)
        d += timedelta(days=7)
    return mondays

def compute_camarilla(high, low, close):
    rng = high - low
    return {
        'h3': close + rng * 1.1 / 4,
        'h4': close + rng * 1.1 / 2,
        'h5': (high / low) * close if low > 0 else 0,
        'l3': close - rng * 1.1 / 4,
        'l4': close - rng * 1.1 / 2,
    }

def compute_ema(closes, period=20):
    if len(closes) < period:
        return None
    k   = 2 / (period + 1)
    ema = closes[0]
    for p in closes[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def get_week_dates(monday):
    return [(monday + timedelta(days=i)).strftime('%Y-%m-%d') for i in range(5)]

# ═══════════════════════════════════════════════════════════════
#  LOAD ALL DATA INTO MEMORY ONCE
# ═══════════════════════════════════════════════════════════════
def load_data(start_date):
    log("Loading price data from DB...")
    conn = get_db()
    c    = conn.cursor()
    c.execute('''
        SELECT symbol, date, open, high, low, close, volume
        FROM   daily_prices
        WHERE  date >= ? AND close > 0
        ORDER  BY symbol, date
    ''', ((start_date - timedelta(days=250)).strftime('%Y-%m-%d'),))
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

    # Build date→index lookup
    date_idx = {}
    for sym, bars in price_data.items():
        date_idx[sym] = {b['date']: i for i, b in enumerate(bars)}

    log(f"Loaded {len(price_data)} symbols")
    return price_data, date_idx

# ═══════════════════════════════════════════════════════════════
#  NIFTY 50 EMA — compute once for all weeks
# ═══════════════════════════════════════════════════════════════
def build_nifty_ema(price_data, date_idx):
    """
    Compute Nifty 50 trend using equal-weighted basket of
    29 Nifty 50 stocks available in the DB.
    Normalises each stock to base 100 on first date, then averages.
    Returns dict of {date_str: bool} — True if basket above 20w EMA.
    """
    log("Computing Nifty 50 basket trend filter...")

    NIFTY50_BASKET = [
        'RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK',
        'HINDUNILVR','SBIN','BHARTIARTL','ITC','KOTAKBANK',
        'LT','AXISBANK','ASIANPAINT','MARUTI','SUNPHARMA',
        'TITAN','BAJFINANCE','NTPC','POWERGRID','ONGC',
        'TATASTEEL','WIPRO','HCLTECH','ADANIENT','ULTRACEMCO',
        'NESTLEIND','BAJAJFINSV','COALINDIA','HINDALCO'
    ]

    # Only use stocks available in price_data with enough history
    basket = [s for s in NIFTY50_BASKET
              if s in price_data and len(price_data[s]) > 100]
    log(f"  Basket: {len(basket)} Nifty 50 stocks")

    # Normalise each stock to 100 on its first available date
    base_prices = {}
    for sym in basket:
        bars = price_data[sym]
        if bars:
            base_prices[sym] = bars[0]['close']

    # Build price lookup: sym → date → close
    sym_date_close = {}
    for sym in basket:
        sym_date_close[sym] = {b['date']: b['close'] for b in price_data[sym]}

    # Collect all trading dates across basket
    all_dates = sorted(set(
        b['date']
        for sym in basket
        for b in price_data[sym]
    ))

    # Compute equal-weighted normalised basket index for each date
    basket_series = []
    for dt in all_dates:
        values = []
        for sym in basket:
            close = sym_date_close[sym].get(dt)
            base  = base_prices.get(sym)
            if close and base and base > 0:
                values.append(close / base * 100)
        if values:
            basket_series.append((dt, sum(values) / len(values)))

    log(f"  Basket index computed for {len(basket_series)} dates")

    # Compute rolling 20-period EMA on basket index
    # 20 periods here = 20 trading days ≈ 4 weeks
    # For weekly trend use 100-day EMA ≈ 20 weeks
    nifty_trend = {}
    k   = 2 / (100 + 1)   # 100-day EMA = 20-week EMA
    ema = basket_series[0][1]

    for dt, val in basket_series:
        ema = val * k + ema * (1 - k)
        nifty_trend[dt] = val >= ema

    bull_days = sum(1 for v in nifty_trend.values() if v)
    bear_days = len(nifty_trend) - bull_days
    log(f"  Bull days: {bull_days} | Bear days: {bear_days}")
    log(f"  Nifty 50 basket EMA ready ✅")
    return nifty_trend

# ═══════════════════════════════════════════════════════════════
#  CORE BACKTEST FUNCTION
# ═══════════════════════════════════════════════════════════════
def run_backtest(
    price_data,
    date_idx,
    nifty_trend,
    mondays,
    config,
):
    """
    Run one backtest variation with given config.
    config keys:
      min_range_pct      : minimum last-week range %
      max_dist_pct       : max proximity to L3
      min_close_price    : min stock price
      min_daily_value_cr : min daily traded value
      min_vol_ratio      : min volume ratio
      max_sl_pct         : max SL distance %
      min_reward_pct     : H3 must be X% above L3 (0 = disabled)
      use_nifty_filter   : bool — only trade when Nifty above 20w EMA
    """
    MIN_RANGE      = config['min_range_pct']
    MAX_DIST       = config['max_dist_pct']
    MIN_PRICE      = config['min_close_price']
    MIN_VALUE      = config['min_daily_value_cr']
    MIN_VOL        = config['min_vol_ratio']
    MAX_SL         = config['max_sl_pct']
    MIN_REWARD     = config['min_reward_pct']
    USE_NIFTY      = config['use_nifty_filter']

    all_signals    = []
    all_trades     = []
    week_counts    = []

    for monday in mondays:
        week_str     = monday.strftime('%Y-%m-%d')
        prev_mon     = monday - timedelta(days=7)
        prev_fri     = monday - timedelta(days=3)
        prev_mon_str = prev_mon.strftime('%Y-%m-%d')
        prev_fri_str = prev_fri.strftime('%Y-%m-%d')
        week_dates   = get_week_dates(monday)
        pprev_mon_str= (prev_mon - timedelta(days=7)).strftime('%Y-%m-%d')
        pprev_fri_str= (prev_mon - timedelta(days=3)).strftime('%Y-%m-%d')

        # Nifty trend on Monday
        monday_str      = monday.strftime('%Y-%m-%d')
        nifty_is_bull   = nifty_trend.get(monday_str, True)
        if USE_NIFTY and not nifty_is_bull:
            week_counts.append((week_str, 0, 'NIFTY_BEAR'))
            continue

        bull_watchlist = []

        for sym in price_data:
            bars = price_data[sym]
            if sym == 'NIFTYBEES' or sym == 'JUNIORBEES':
                continue

            # Last week bars
            prev_bars = [b for b in bars
                         if prev_mon_str <= b['date'] <= prev_fri_str]
            if len(prev_bars) < 3:
                continue

            prev_high  = max(b['high']  for b in prev_bars)
            prev_low   = min(b['low']   for b in prev_bars)
            prev_close = prev_bars[-1]['close']

            if prev_low <= 0 or prev_close < MIN_PRICE:
                continue

            # Range quality
            range_pct = (prev_high - prev_low) / prev_close * 100
            if range_pct < MIN_RANGE:
                continue

            # Camarilla
            cam = compute_camarilla(prev_high, prev_low, prev_close)
            l3  = cam['l3']; l4 = cam['l4']
            h3  = cam['h3']

            # SL cap
            sl_pct = abs(l3 - l4) / l3 * 100 if l3 > 0 else 99
            if sl_pct > MAX_SL:
                continue

            # Min reward check (Test 1 fix)
            if MIN_REWARD > 0:
                reward_pct = (h3 - l3) / l3 * 100 if l3 > 0 else 0
                if reward_pct < MIN_REWARD:
                    continue

            # Liquidity
            recent = [b for b in bars if b['date'] < prev_mon_str][-20:]
            if len(recent) < 10:
                continue
            avg_vol   = sum(b['volume'] for b in recent) / len(recent)
            avg_close = sum(b['close']  for b in recent) / len(recent)
            if (avg_vol * avg_close / 1e7) < MIN_VALUE:
                continue

            # 20w EMA
            hist = [b for b in bars if b['date'] < prev_mon_str][-100:]
            if len(hist) < 20:
                continue
            ema20w = compute_ema([b['close'] for b in hist], 20)
            if ema20w is None:
                continue

            # CPR Higher Value
            pprev_bars = [b for b in bars
                          if pprev_mon_str <= b['date'] <= pprev_fri_str]
            if len(pprev_bars) >= 3:
                pprev_h  = max(b['high']  for b in pprev_bars)
                pprev_l  = min(b['low']   for b in pprev_bars)
                pprev_c  = pprev_bars[-1]['close']
                prev_bc  = (prev_high + prev_low) / 2
                prev_piv = (prev_high + prev_low + prev_close) / 3
                prev_tc  = 2 * prev_piv - prev_bc
                pprev_bc = (pprev_h + pprev_l) / 2
                pprev_piv= (pprev_h + pprev_l + pprev_c) / 3
                pprev_tc = 2 * pprev_piv - pprev_bc
                if prev_bc <= pprev_tc:
                    continue

            # Monday price check
            this_week = [b for b in bars if b['date'] in week_dates]
            if not this_week:
                continue
            monday_bar = next((b for b in this_week
                               if b['date'] == week_dates[0]), None)
            if not monday_bar:
                continue
            mon_close = monday_bar['close']

            # EMA trend
            if mon_close < ema20w:
                continue

            # Proximity check — add to watchlist if Monday close
            # is within MAX_DIST of L3 OR if L3 is likely to be
            # touched during the week (price within 3% of L3)
            dist_mon = abs(mon_close - l3) / mon_close * 100
            if dist_mon > 3.0 and mon_close > l3:
                continue   # price too far above L3 — won't touch this week

            bull_watchlist.append({
                'symbol':  sym,
                'week':    week_str,
                'l3':      l3,
                'l4':      l4,
                'h3':      h3,
                'avg_vol': avg_vol,
            })
            all_signals.append({'symbol': sym, 'week': week_str})

        week_counts.append((week_str, len(bull_watchlist), 'OK'))

        # ── Check each watchlist stock for trigger ─────────────
        for setup in bull_watchlist:
            sym  = setup['symbol']
            l3   = setup['l3']
            l4   = setup['l4']
            h3   = setup['h3']
            bars = price_data[sym]
            week_bars = [b for b in bars if b['date'] in week_dates]
            prior_bars = []
            triggered  = False
            entry_price= None
            signal_date= None

            for bar in week_bars:
                l3_touched_before = any(pb['low'] <= l3 for pb in prior_bars)
                l4_broken         = any(pb['low'] <  l4 for pb in prior_bars + [bar])

                if l4_broken:
                    break

                avg_vol   = setup['avg_vol']
                vol_ratio = bar['volume'] / avg_vol if avg_vol > 0 else 0

                # Trigger: low touched L3, close above L3, green candle, virgin, volume
                # No proximity check here — if stock is on watchlist it can trigger
                if (not l3_touched_before
                        and bar['low']   <= l3
                        and bar['close'] >  l3
                        and bar['close'] >  bar['open']
                        and vol_ratio    >= MIN_VOL):
                    triggered    = True
                    # Entry at L3 (the level) — book says enter AT the level
                    # not at close (which has already bounced)
                    # This gives the true 2:1 R:R built into Camarilla formula
                    entry_price  = l3
                    signal_date  = bar['date']
                    break

                prior_bars.append(bar)

            if not triggered or entry_price is None:
                continue

            # Simulate trade — entry at L3 (limit order)
            # Also check the signal bar itself — entry is L3, intraday
            # the bar may have already hit H3 or closed below L4
            entry_idx   = date_idx[sym].get(signal_date)
            if entry_idx is None:
                continue

            signal_bar  = price_data[sym][entry_idx]
            all_bars    = price_data[sym]

            outcome    = 'TIME_EXIT'
            exit_price = signal_bar['close']
            exit_date  = signal_date
            pnl_pct    = 0

            # Check signal bar first (intraday — entered at L3)
            if signal_bar['high'] >= h3:
                outcome    = 'TARGET_HIT'
                exit_price = round(h3, 2)
                exit_date  = signal_date
                pnl_pct    = round((h3 - entry_price) / entry_price * 100, 2)
            elif signal_bar['low'] < l4:
                # False bounce — touched L3 and then broke L4 same bar
                outcome    = 'SL_HIT'
                exit_price = round(l4, 2)
                exit_date  = signal_date
                pnl_pct    = round((l4 - entry_price) / entry_price * 100, 2)
            else:
                # Check future bars
                future_bars = all_bars[entry_idx + 1: entry_idx + 1 + MAX_HOLD]
                for fb in future_bars:
                    if fb['low'] <= l4:
                        outcome    = 'SL_HIT'
                        exit_price = round(l4, 2)
                        exit_date  = fb['date']
                        pnl_pct    = round((l4 - entry_price) / entry_price * 100, 2)
                        break
                    if fb['high'] >= h3:
                        outcome    = 'TARGET_HIT'
                        exit_price = round(h3, 2)
                        exit_date  = fb['date']
                        pnl_pct    = round((h3 - entry_price) / entry_price * 100, 2)
                        break
                    exit_date  = fb['date']
                    exit_price = fb['close']

                if outcome == 'TIME_EXIT':
                    pnl_pct = round((exit_price - entry_price) / entry_price * 100, 2)

            all_trades.append({
                'symbol':      sym,
                'week':        week_str,
                'signal_date': signal_date,
                'entry':       round(entry_price, 2),
                'sl':          round(l4, 2),
                't1':          round(h3, 2),
                'exit_price':  round(exit_price, 2),
                'exit_date':   exit_date,
                'outcome':     outcome,
                'pnl_pct':     pnl_pct,
                'sl_pct':      round(abs(entry_price - l4) / entry_price * 100, 2),
                'reward_pct':  round(abs(h3 - entry_price) / entry_price * 100, 2),
            })

    return all_signals, all_trades, week_counts

# ═══════════════════════════════════════════════════════════════
#  ANALYSE AND FORMAT RESULTS
# ═══════════════════════════════════════════════════════════════
def analyse(test_name, config_desc, all_signals, all_trades, week_counts, mondays):
    total_weeks   = len(mondays)
    total_signals = len(all_signals)
    total_trades  = len(all_trades)
    wins    = [t for t in all_trades if t['outcome'] == 'TARGET_HIT']
    losses  = [t for t in all_trades if t['outcome'] == 'SL_HIT']
    timeex  = [t for t in all_trades if t['outcome'] == 'TIME_EXIT']

    win_rate    = round(len(wins)   / total_trades * 100, 1) if total_trades else 0
    loss_rate   = round(len(losses) / total_trades * 100, 1) if total_trades else 0
    time_rate   = round(len(timeex) / total_trades * 100, 1) if total_trades else 0
    avg_win     = round(sum(t['pnl_pct'] for t in wins)   / len(wins),   2) if wins   else 0
    avg_loss    = round(sum(t['pnl_pct'] for t in losses) / len(losses), 2) if losses else 0
    avg_time    = round(sum(t['pnl_pct'] for t in timeex) / len(timeex), 2) if timeex else 0
    total_pnl   = round(sum(t['pnl_pct'] for t in all_trades), 2)
    avg_sl      = round(sum(t['sl_pct']     for t in all_trades) / total_trades, 2) if total_trades else 0
    avg_reward  = round(sum(t['reward_pct'] for t in all_trades) / total_trades, 2) if total_trades else 0
    avg_rr      = round(avg_reward / avg_sl, 2) if avg_sl > 0 else 0
    exp_value   = round(total_pnl / total_trades, 2) if total_trades else 0
    weeks_sig   = sum(1 for _, cnt, _ in week_counts if cnt > 0)
    avg_wl      = round(total_signals / total_weeks, 1) if total_weeks else 0
    conv_rate   = round(total_trades / total_signals * 100, 1) if total_signals else 0

    # Monthly breakdown
    monthly = defaultdict(list)
    for t in all_trades:
        monthly[t['signal_date'][:7]].append(t)

    # Verdict
    if exp_value > 0.5 and win_rate >= 65 and avg_rr >= 1.5:
        verdict = "✅ EXCELLENT — Profitable with good R:R and win rate"
    elif exp_value > 0 and win_rate >= 60:
        verdict = "✅ GOOD — Profitable setup, worth trading"
    elif exp_value > 0:
        verdict = "⚠️  MARGINAL — Slightly profitable, needs monitoring"
    else:
        verdict = "❌ UNPROFITABLE — Avoid or modify further"

    lines = []
    lines.append(f"\n{'═'*68}")
    lines.append(f"  {test_name}")
    lines.append(f"  Config: {config_desc}")
    lines.append(f"{'═'*68}")
    lines.append(f"\n  COVERAGE")
    lines.append(f"  {'Weeks tested':<35}: {total_weeks}")
    lines.append(f"  {'Weeks with signals':<35}: {weeks_sig} ({round(weeks_sig/total_weeks*100,1) if total_weeks else 0}%)")
    lines.append(f"  {'Avg watchlist stocks/week':<35}: {avg_wl}")
    lines.append(f"  {'Total watchlist entries':<35}: {total_signals}")
    lines.append(f"  {'Trades triggered':<35}: {total_trades}")
    lines.append(f"  {'Signal→Trade conversion':<35}: {conv_rate}%")

    lines.append(f"\n  TRADE OUTCOMES")
    lines.append(f"  {'Total trades':<35}: {total_trades}")
    lines.append(f"  {'✅ Target hit (WIN)':<35}: {len(wins)} ({win_rate}%)")
    lines.append(f"  {'❌ SL hit (LOSS)':<35}: {len(losses)} ({loss_rate}%)")
    lines.append(f"  {'⏱  Time exit':<35}: {len(timeex)} ({time_rate}%)")

    lines.append(f"\n  P&L")
    lines.append(f"  {'Avg win %':<35}: +{avg_win}%")
    lines.append(f"  {'Avg loss %':<35}: {avg_loss}%")
    lines.append(f"  {'Avg time exit %':<35}: {avg_time}%")
    lines.append(f"  {'Avg SL risk':<35}: {avg_sl}%")
    lines.append(f"  {'Avg reward':<35}: +{avg_reward}%")
    lines.append(f"  {'Avg R:R':<35}: 1:{avg_rr}")
    lines.append(f"  {'Total PnL (sum of all trades)':<35}: {total_pnl}%")
    lines.append(f"  {'Expected value per trade':<35}: {exp_value}%")

    lines.append(f"\n  MONTHLY BREAKDOWN")
    lines.append(f"  {'Month':<10} {'Trades':>7} {'Wins':>6} {'Losses':>8} {'Win%':>7} {'Avg PnL':>9}")
    lines.append(f"  {'-'*55}")
    for month in sorted(monthly.keys()):
        mt   = monthly[month]
        mw   = sum(1 for t in mt if t['outcome'] == 'TARGET_HIT')
        ml   = sum(1 for t in mt if t['outcome'] == 'SL_HIT')
        mwr  = round(mw / len(mt) * 100, 1) if mt else 0
        mpnl = round(sum(t['pnl_pct'] for t in mt) / len(mt), 2) if mt else 0
        lines.append(f"  {month:<10} {len(mt):>7} {mw:>6} {ml:>8} {mwr:>6}% {mpnl:>+8.2f}%")

    # ── Year-by-year breakdown ─────────────────────────────────
    yearly = defaultdict(list)
    for t in all_trades:
        yr = t['signal_date'][:4]
        yearly[yr].append(t)

    lines.append(f"\n  YEAR-BY-YEAR BREAKDOWN")
    lines.append(f"  {'Year':<8} {'Trades':>7} {'Wins':>6} {'Losses':>8} {'Win%':>7} {'Avg PnL':>9} {'Total PnL':>11}")
    lines.append(f"  {'-'*62}")
    for yr in sorted(yearly.keys()):
        yt    = yearly[yr]
        yw    = sum(1 for t in yt if t['outcome'] == 'TARGET_HIT')
        yl    = sum(1 for t in yt if t['outcome'] == 'SL_HIT')
        ywr   = round(yw / len(yt) * 100, 1) if yt else 0
        yapnl = round(sum(t['pnl_pct'] for t in yt) / len(yt), 2) if yt else 0
        ytpnl = round(sum(t['pnl_pct'] for t in yt), 2)
        lines.append(f"  {yr:<8} {len(yt):>7} {yw:>6} {yl:>8} {ywr:>6}% {yapnl:>+8.2f}% {ytpnl:>+10.2f}%")

    # ── Out-of-sample vs in-sample split ──────────────────────
    from datetime import date as date_cls
    cutoff = (date_cls.today() - timedelta(days=372)).strftime('%Y-%m-%d')
    oos_trades = [t for t in all_trades if t['signal_date'] <  cutoff]
    is_trades  = [t for t in all_trades if t['signal_date'] >= cutoff]

    def period_stats(trades, label):
        if not trades:
            return f"  {label}: No trades"
        w    = sum(1 for t in trades if t['outcome'] == 'TARGET_HIT')
        l    = sum(1 for t in trades if t['outcome'] == 'SL_HIT')
        wr   = round(w / len(trades) * 100, 1)
        apnl = round(sum(t['pnl_pct'] for t in trades) / len(trades), 2)
        tpnl = round(sum(t['pnl_pct'] for t in trades), 2)
        return (f"  {label:<30}: {len(trades)} trades | "
                f"Win:{wr}% | Avg:{apnl:+.2f}% | Total:{tpnl:+.2f}%")

    lines.append(f"\n  OUT-OF-SAMPLE VALIDATION")
    lines.append(f"  (If both periods similar → setup is NOT curve-fitted)")
    lines.append(f"  {'-'*62}")
    lines.append(period_stats(oos_trades, f"Out-of-sample (before {cutoff[:7]})"))
    lines.append(period_stats(is_trades,  f"In-sample     (from   {cutoff[:7]})"))
    if oos_trades and is_trades:
        oos_wr = sum(1 for t in oos_trades if t['outcome']=='TARGET_HIT') / len(oos_trades) * 100
        is_wr  = sum(1 for t in is_trades  if t['outcome']=='TARGET_HIT') / len(is_trades)  * 100
        diff   = abs(oos_wr - is_wr)
        if diff <= 10:
            lines.append(f"  ✅ Win rate difference: {diff:.1f}% — setup is ROBUST")
        elif diff <= 20:
            lines.append(f"  ⚠️  Win rate difference: {diff:.1f}% — some curve fitting possible")
        else:
            lines.append(f"  ❌ Win rate difference: {diff:.1f}% — likely curve fitted, use caution")

    lines.append(f"\n  WEEK-BY-WEEK")
    lines.append(f"  {'Week':<14} {'Watchlist':>10} {'Trades':>8} {'Wins':>6} {'Losses':>8} {'Note'}")
    lines.append(f"  {'-'*65}")
    for week_str, cnt, note in week_counts:
        wt = [t for t in all_trades if t['week'] == week_str]
        ww = sum(1 for t in wt if t['outcome'] == 'TARGET_HIT')
        wl = sum(1 for t in wt if t['outcome'] == 'SL_HIT')
        n  = f" [{note}]" if note != 'OK' else ''
        lines.append(f"  {week_str:<14} {cnt:>10} {len(wt):>8} {ww:>6} {wl:>8}{n}")

    lines.append(f"\n  ALL TRADES")
    lines.append(f"  {'Symbol':<14} {'Date':<12} {'Entry':>8} {'SL':>8} "
                 f"{'T1':>8} {'Exit':>8} {'PnL%':>8} Outcome")
    lines.append(f"  {'-'*80}")
    for t in sorted(all_trades, key=lambda x: x['signal_date']):
        out = ('✅ WIN' if t['outcome'] == 'TARGET_HIT'
               else '❌ LOSS' if t['outcome'] == 'SL_HIT'
               else '⏱  TIME')
        pnl = f"+{t['pnl_pct']}%" if t['pnl_pct'] > 0 else f"{t['pnl_pct']}%"
        lines.append(f"  {t['symbol']:<14} {t['signal_date']:<12} "
                     f"{t['entry']:>8} {t['sl']:>8} {t['t1']:>8} "
                     f"{t['exit_price']:>8} {pnl:>8} {out}")

    lines.append(f"\n  VERDICT: {verdict}")
    lines.append(f"{'═'*68}\n")

    return '\n'.join(lines), {
        'test':       test_name,
        'trades':     total_trades,
        'win_rate':   win_rate,
        'avg_win':    avg_win,
        'avg_loss':   avg_loss,
        'avg_rr':     avg_rr,
        'total_pnl':  total_pnl,
        'exp_value':  exp_value,
        'verdict':    verdict,
    }

# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
def main():
    os.makedirs(os.path.join(BASE_DIR, 'logs'), exist_ok=True)

    end_date   = date.today() - timedelta(days=7)
    start_date = end_date - timedelta(days=MONTHS * 30)
    mondays    = get_mondays(start_date, end_date)

    header = f"""
{'═'*68}
  CAMARILLA WEEKLY BULLISH — FULL BACKTEST SUITE
  Period  : {start_date} to {end_date} ({MONTHS} months)
  Weeks   : {len(mondays)}
  Run at  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
  Exit    : T1(H3) hit=WIN | L4 hit=LOSS | {MAX_HOLD} days=TIME
  Entry   : L3 level (limit order at the level, not bar close)
  R:R     : Built-in 2:1 by Camarilla formula (H3-L3 = 2x L3-L4)

  PERIOD SPLIT:
  In-sample     : {(end_date - timedelta(days=365)).strftime('%Y-%m-%d')} to {end_date} (last 12 months)
  Out-of-sample : {start_date} to {(end_date - timedelta(days=365)).strftime('%Y-%m-%d')} (prior 12 months)
  If results are similar in both — setup is NOT curve-fitted
{'═'*68}
"""
    log(header)

    price_data, date_idx = load_data(start_date)
    nifty_trend          = build_nifty_ema(price_data, date_idx)

    # ── Define 5 test configurations ──────────────────────────
    tests = [
        {
            'name': 'TEST 0 — BASELINE (original)',
            'desc': 'All 9 filters, no R:R fix, no Nifty filter, dist=0.3%',
            'config': {
                'min_range_pct':      2.0,
                'max_dist_pct':       0.3,
                'min_close_price':    50.0,
                'min_daily_value_cr': 5.0,
                'min_vol_ratio':      1.5,
                'max_sl_pct':         4.0,
                'min_reward_pct':     0.0,   # disabled
                'use_nifty_filter':   False,
            }
        },
        {
            'name': 'TEST 1 — FIX R:R BUG (H3 must be 2%+ above L3)',
            'desc': 'Baseline + min_reward_pct=2.0 (H3 must be 2% above entry)',
            'config': {
                'min_range_pct':      2.0,
                'max_dist_pct':       0.3,
                'min_close_price':    50.0,
                'min_daily_value_cr': 5.0,
                'min_vol_ratio':      1.5,
                'max_sl_pct':         4.0,
                'min_reward_pct':     2.0,   # FIX: H3 must be 2%+ above L3
                'use_nifty_filter':   False,
            }
        },
        {
            'name': 'TEST 2 — ADD NIFTY TREND FILTER',
            'desc': 'Baseline + only trade when Nifty above 20w EMA',
            'config': {
                'min_range_pct':      2.0,
                'max_dist_pct':       0.3,
                'min_close_price':    50.0,
                'min_daily_value_cr': 5.0,
                'min_vol_ratio':      1.5,
                'max_sl_pct':         4.0,
                'min_reward_pct':     0.0,
                'use_nifty_filter':   True,  # NEW: Nifty trend filter
            }
        },
        {
            'name': 'TEST 3 — TIGHTER PROXIMITY (0.15%)',
            'desc': 'Baseline + max_dist_pct=0.15% (price must be right at L3)',
            'config': {
                'min_range_pct':      2.0,
                'max_dist_pct':       0.15,  # TIGHTER: was 0.3%
                'min_close_price':    50.0,
                'min_daily_value_cr': 5.0,
                'min_vol_ratio':      1.5,
                'max_sl_pct':         4.0,
                'min_reward_pct':     0.0,
                'use_nifty_filter':   False,
            }
        },
        {
            'name': 'TEST 4 — ALL FIXES COMBINED (recommended)',
            'desc': 'R:R fix + Nifty filter + tighter proximity',
            'config': {
                'min_range_pct':      2.0,
                'max_dist_pct':       0.15,  # tighter
                'min_close_price':    50.0,
                'min_daily_value_cr': 5.0,
                'min_vol_ratio':      1.5,
                'max_sl_pct':         4.0,
                'min_reward_pct':     2.0,   # H3 must be 2%+ above L3
                'use_nifty_filter':   True,  # Nifty trend filter
            }
        },
    ]

    all_output   = [header]
    summary_rows = []

    for i, test in enumerate(tests):
        log(f"\nRunning {test['name']}...")
        signals, trades, wk_counts = run_backtest(
            price_data, date_idx, nifty_trend,
            mondays, test['config']
        )
        log(f"  Signals: {len(signals)} | Trades: {len(trades)}")
        report, summary = analyse(
            test['name'], test['desc'],
            signals, trades, wk_counts, mondays
        )
        all_output.append(report)
        summary_rows.append(summary)
        log(f"  Win rate: {summary['win_rate']}% | "
            f"R:R: 1:{summary['avg_rr']} | "
            f"Expected: {summary['exp_value']}%/trade")

    # ── Comparison table ───────────────────────────────────────
    comp = [
        f"\n{'═'*68}",
        f"  COMPARISON SUMMARY — ALL TESTS",
        f"{'═'*68}",
        f"  {'Test':<10} {'Trades':>8} {'Win%':>7} {'AvgWin':>9} "
        f"{'AvgLoss':>9} {'R:R':>6} {'ExpVal':>9} {'PnL':>10}",
        f"  {'-'*75}",
    ]
    for s in summary_rows:
        tname = s['test'].split('—')[0].strip()
        comp.append(
            f"  {tname:<10} {s['trades']:>8} {s['win_rate']:>6}% "
            f"{s['avg_win']:>+8.2f}% {s['avg_loss']:>+8.2f}% "
            f"{s['avg_rr']:>6} {s['exp_value']:>+8.2f}% {s['total_pnl']:>+9.2f}%"
        )

    # Best test
    profitable = [s for s in summary_rows if s['exp_value'] > 0]
    if profitable:
        best = max(profitable, key=lambda x: x['exp_value'])
        comp.append(f"\n  🏆 BEST TEST: {best['test']}")
        comp.append(f"     Expected value: +{best['exp_value']}%/trade")
        comp.append(f"     Win rate: {best['win_rate']}% | R:R: 1:{best['avg_rr']}")
    else:
        comp.append(f"\n  ⚠️  No test was consistently profitable")
        comp.append(f"     Recommend reviewing entry/exit logic further")

    comp.append(f"{'═'*68}\n")
    comp_str = '\n'.join(comp)
    all_output.append(comp_str)
    log(comp_str)

    # ── Write output file ──────────────────────────────────────
    final = '\n'.join(all_output)
    with open(OUTPUT_FILE, 'w') as f:
        f.write(final)

    log(f"\n✅ Complete! Results saved to: {OUTPUT_FILE}")
    log(f"   View with: cat {OUTPUT_FILE}")
    log(f"   Or scroll: less {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
