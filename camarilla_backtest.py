#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  camarilla_backtest.py
#  Backtests the Weekly Camarilla BULLISH setup for last 1 year
#  Uses only data already in daily_prices table — no API needed
#
#  Setup tested:
#    - Weekly Camarilla computed from prior week H/L/C
#    - Price within 0.3% of L3 on any day that week
#    - Price above 20-week EMA (uptrend confirmed)
#    - Virgin L3 (first touch of week)
#    - L4 not broken that week
#    - Volume >= 1.5x 20-day average
#    - SL <= 4% from entry
#    - Liquid stock >= 5Cr daily value
#    - CPR Higher Value (this week BC > last week TC)
#
#  Trade management:
#    Entry  : Close of signal day (end of day entry)
#    SL     : L4 (hard stop)
#    Target1: H3 (first target — book 100% here for this test)
#    Exit   : whichever hits first in next 5 trading days
#             SL hit = LOSS | T1 hit = WIN | 5 days = TIME EXIT
#
#  Usage:
#    python3 camarilla_backtest.py
#    python3 camarilla_backtest.py --months 6   (last 6 months)
#    python3 camarilla_backtest.py --months 12  (last 12 months, default)
# ═══════════════════════════════════════════════════════════════

import sqlite3
import os
import sys
import argparse
from datetime import date, timedelta, datetime
from collections import defaultdict

BASE_DIR = os.path.expanduser('~/nse-scanner')
DB_PATH  = os.path.join(BASE_DIR, 'nse_data.db')

# ── Config (same as live scanner) ────────────────────────────
MIN_RANGE_PCT      = 2.0
MAX_DIST_PCT       = 0.3
MIN_CLOSE_PRICE    = 50.0
MIN_DAILY_VALUE_CR = 5.0
MIN_VOL_RATIO      = 1.5
MAX_SL_PCT         = 4.0
MAX_HOLD_DAYS      = 5       # exit after 5 trading days if no SL/T1

# ── Helpers ───────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_mondays(start_date, end_date):
    """Return all Mondays between start and end date."""
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
    """Compute EMA on a list of closes (chronological order)."""
    if len(closes) < period:
        return None
    k   = 2 / (period + 1)
    ema = closes[0]
    for p in closes[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def get_week_dates(monday):
    """Return Mon-Fri dates for a given Monday."""
    return [(monday + timedelta(days=i)).strftime('%Y-%m-%d') for i in range(5)]

# ═══════════════════════════════════════════════════════════════
#  MAIN BACKTEST
# ═══════════════════════════════════════════════════════════════
def run_backtest(months=12):
    conn  = get_db()
    c     = conn.cursor()

    end_date   = date.today() - timedelta(days=7)   # leave last week out
    start_date = end_date - timedelta(days=months * 30)

    print(f"\n{'='*65}")
    print(f"  CAMARILLA WEEKLY BULLISH BACKTEST")
    print(f"  Period : {start_date} to {end_date} ({months} months)")
    print(f"  Filters: 9 filters same as live scanner")
    print(f"  Exit   : T1(H3) hit = WIN | L4 hit = LOSS | 5 days = TIME")
    print(f"{'='*65}\n")

    # ── Load all daily prices into memory for speed ────────────
    print("Loading price data...")
    c.execute('''
        SELECT symbol, date, open, high, low, close, volume
        FROM   daily_prices
        WHERE  date >= ? AND close > 0
        ORDER  BY symbol, date
    ''', ((start_date - timedelta(days=200)).strftime('%Y-%m-%d'),))
    rows = c.fetchall()
    conn.close()

    # Organize by symbol → sorted list of bars
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

    all_symbols  = list(price_data.keys())
    print(f"Symbols loaded: {len(all_symbols)}")

    # Build date→index lookup per symbol for fast access
    date_idx = {}
    for sym, bars in price_data.items():
        date_idx[sym] = {b['date']: i for i, b in enumerate(bars)}

    # ── Get all Mondays in backtest period ─────────────────────
    mondays = get_mondays(start_date, end_date)
    print(f"Weeks to test : {len(mondays)}\n")

    # ── Results storage ────────────────────────────────────────
    all_signals   = []   # every time a stock qualified for watchlist
    all_trades    = []   # every time price actually touched L3 that week

    week_counts   = []   # (week, bull_watchlist_count)

    # ═══════════════════════════════════════════════════════════
    #  LOOP THROUGH EACH WEEK
    # ═══════════════════════════════════════════════════════════
    for monday in mondays:
        week_str      = monday.strftime('%Y-%m-%d')
        prev_mon      = monday - timedelta(days=7)
        prev_fri      = monday - timedelta(days=3)
        prev_mon_str  = prev_mon.strftime('%Y-%m-%d')
        prev_fri_str  = prev_fri.strftime('%Y-%m-%d')
        week_dates    = get_week_dates(monday)

        # Previous week for CPR trend
        pprev_mon_str = (prev_mon - timedelta(days=7)).strftime('%Y-%m-%d')
        pprev_fri_str = (prev_mon - timedelta(days=3)).strftime('%Y-%m-%d')

        bull_watchlist = []

        for sym in all_symbols:
            bars     = price_data[sym]
            di       = date_idx[sym]

            # ── Get last week OHLCV ────────────────────────────
            prev_bars = [b for b in bars
                         if prev_mon_str <= b['date'] <= prev_fri_str]
            if len(prev_bars) < 3:
                continue

            prev_high  = max(b['high']  for b in prev_bars)
            prev_low   = min(b['low']   for b in prev_bars)
            prev_close = prev_bars[-1]['close']

            if prev_low <= 0 or prev_close < MIN_CLOSE_PRICE:
                continue

            # ── Range quality ──────────────────────────────────
            range_pct = (prev_high - prev_low) / prev_close * 100
            if range_pct < MIN_RANGE_PCT:
                continue

            # ── Compute Camarilla levels ───────────────────────
            cam = compute_camarilla(prev_high, prev_low, prev_close)
            l3  = cam['l3']; l4 = cam['l4']
            h3  = cam['h3']; h4 = cam['h4']

            # ── SL risk cap ────────────────────────────────────
            sl_pct = abs(l3 - l4) / l3 * 100 if l3 > 0 else 99
            if sl_pct > MAX_SL_PCT:
                continue

            # ── Liquidity ──────────────────────────────────────
            recent_bars = [b for b in bars if b['date'] < prev_mon_str][-20:]
            if len(recent_bars) < 10:
                continue
            avg_vol   = sum(b['volume'] for b in recent_bars) / len(recent_bars)
            avg_close = sum(b['close']  for b in recent_bars) / len(recent_bars)
            daily_val = avg_vol * avg_close / 1e7
            if daily_val < MIN_DAILY_VALUE_CR:
                continue

            # ── 20-week EMA (100 trading days) ────────────────
            hist_bars = [b for b in bars if b['date'] < prev_mon_str][-100:]
            if len(hist_bars) < 20:
                continue
            ema20w = compute_ema([b['close'] for b in hist_bars], 20)
            if ema20w is None:
                continue

            # ── CPR Higher Value filter ────────────────────────
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
                # BULLISH: this week BC > last week TC
                if prev_bc <= pprev_tc:
                    continue

            # ── Get this week's bars ───────────────────────────
            this_week_bars = [b for b in bars if b['date'] in week_dates]
            if not this_week_bars:
                continue

            # ── Monday open price as reference ────────────────
            monday_bar = next((b for b in this_week_bars
                               if b['date'] == week_dates[0]), None)
            if not monday_bar:
                continue

            # Check price is in ballpark on Monday
            mon_close = monday_bar['close']
            dist_mon  = abs(mon_close - l3) / mon_close * 100

            # ── 20w EMA check on Monday ────────────────────────
            if mon_close < ema20w:
                continue

            # Stock qualifies for watchlist this week
            bull_watchlist.append({
                'symbol':  sym,
                'week':    week_str,
                'l3':      l3,
                'l4':      l4,
                'h3':      h3,
                'l3_entry': round(l3, 2),
                'sl':      round(l4, 2),
                't1':      round(h3, 2),
                'avg_vol': avg_vol,
            })
            all_signals.append({'symbol': sym, 'week': week_str})

        week_counts.append((week_str, len(bull_watchlist)))

        # ═══════════════════════════════════════════════════════
        #  FOR EACH WATCHLIST STOCK — CHECK IF TRADE TRIGGERED
        # ═══════════════════════════════════════════════════════
        for setup in bull_watchlist:
            sym  = setup['symbol']
            l3   = setup['l3']
            l4   = setup['l4']
            h3   = setup['h3']
            bars = price_data[sym]

            # Look at each day of this week
            this_week_bars = [b for b in bars if b['date'] in week_dates]
            prior_bars     = []
            triggered      = False
            entry_price    = None
            signal_date    = None

            for bar in this_week_bars:
                # Virgin check — L3 not touched in prior bars this week
                l3_touched_before = any(pb['low'] <= l3 for pb in prior_bars)
                l4_broken         = any(pb['low'] <  l4 for pb in prior_bars + [bar])

                if l4_broken:
                    break  # setup failed before trigger

                # Check volume
                avg_vol   = setup['avg_vol']
                vol_ratio = bar['volume'] / avg_vol if avg_vol > 0 else 0

                # Trigger condition:
                # Low touched L3, close above L3, green candle, virgin, volume ok
                if (not l3_touched_before
                        and bar['low']   <= l3
                        and bar['close'] >  l3
                        and bar['close'] >  bar['open']
                        and vol_ratio    >= MIN_VOL_RATIO):

                    triggered   = True
                    entry_price = bar['close']
                    signal_date = bar['date']
                    break

                prior_bars.append(bar)

            if not triggered or entry_price is None:
                continue

            # ── Simulate trade outcome over next 5 bars ────────
            entry_idx  = date_idx[sym].get(signal_date)
            if entry_idx is None:
                continue

            all_bars   = price_data[sym]
            future_bars = all_bars[entry_idx + 1: entry_idx + 1 + MAX_HOLD_DAYS]

            outcome    = 'TIME_EXIT'
            exit_price = entry_price
            exit_date  = signal_date
            pnl_pct    = 0

            for fb in future_bars:
                # SL hit
                if fb['low'] <= l4:
                    outcome    = 'SL_HIT'
                    exit_price = round(l4, 2)
                    exit_date  = fb['date']
                    pnl_pct    = round((l4 - entry_price) / entry_price * 100, 2)
                    break
                # Target hit
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

    # ═══════════════════════════════════════════════════════════
    #  RESULTS ANALYSIS
    # ═══════════════════════════════════════════════════════════
    total_weeks      = len(mondays)
    total_signals    = len(all_signals)
    total_trades     = len(all_trades)
    wins             = [t for t in all_trades if t['outcome'] == 'TARGET_HIT']
    losses           = [t for t in all_trades if t['outcome'] == 'SL_HIT']
    time_exits       = [t for t in all_trades if t['outcome'] == 'TIME_EXIT']
    win_rate         = round(len(wins) / total_trades * 100, 1) if total_trades else 0
    avg_win          = round(sum(t['pnl_pct'] for t in wins)   / len(wins),   2) if wins   else 0
    avg_loss         = round(sum(t['pnl_pct'] for t in losses) / len(losses), 2) if losses else 0
    avg_time_exit    = round(sum(t['pnl_pct'] for t in time_exits) / len(time_exits), 2) if time_exits else 0
    total_pnl        = round(sum(t['pnl_pct'] for t in all_trades), 2)
    avg_sl_pct       = round(sum(t['sl_pct']     for t in all_trades) / total_trades, 2) if total_trades else 0
    avg_reward_pct   = round(sum(t['reward_pct'] for t in all_trades) / total_trades, 2) if total_trades else 0
    avg_rr           = round(avg_reward_pct / avg_sl_pct, 2) if avg_sl_pct > 0 else 0

    # Signals per week
    weeks_with_signals = sum(1 for _, cnt in week_counts if cnt > 0)
    avg_signals_pw     = round(total_signals / total_weeks, 1) if total_weeks else 0

    print(f"{'='*65}")
    print(f"  BACKTEST RESULTS SUMMARY")
    print(f"{'='*65}")
    print(f"\n  COVERAGE")
    print(f"  {'Weeks tested':<30}: {total_weeks}")
    print(f"  {'Weeks with signals':<30}: {weeks_with_signals} ({round(weeks_with_signals/total_weeks*100,1)}%)")
    print(f"  {'Avg watchlist stocks/week':<30}: {avg_signals_pw}")
    print(f"  {'Total watchlist entries':<30}: {total_signals}")
    print(f"  {'Trades triggered (L3 touched)':<30}: {total_trades}")
    print(f"  {'Signal→Trade conversion':<30}: {round(total_trades/total_signals*100,1) if total_signals else 0}%")

    print(f"\n  TRADE OUTCOMES")
    print(f"  {'Total trades':<30}: {total_trades}")
    print(f"  {'✅ Target hit (WIN)':<30}: {len(wins)} ({win_rate}%)")
    print(f"  {'❌ SL hit (LOSS)':<30}: {len(losses)} ({round(len(losses)/total_trades*100,1) if total_trades else 0}%)")
    print(f"  {'⏱  Time exit (neutral)':<30}: {len(time_exits)} ({round(len(time_exits)/total_trades*100,1) if total_trades else 0}%)")

    print(f"\n  P&L")
    print(f"  {'Avg win %':<30}: +{avg_win}%")
    print(f"  {'Avg loss %':<30}: {avg_loss}%")
    print(f"  {'Avg time exit %':<30}: {avg_time_exit}%")
    print(f"  {'Avg risk per trade (SL%)':<30}: {avg_sl_pct}%")
    print(f"  {'Avg reward per trade':<30}: {avg_reward_pct}%")
    print(f"  {'Avg R:R':<30}: 1:{avg_rr}")
    print(f"  {'Total cumulative PnL':<30}: {total_pnl}%")
    print(f"  {'Expected value per trade':<30}: {round(total_pnl/total_trades,2) if total_trades else 0}%")

    # ── Week by week summary ───────────────────────────────────
    print(f"\n{'='*65}")
    print(f"  WEEK-BY-WEEK WATCHLIST COUNT")
    print(f"{'='*65}")
    print(f"  {'Week':<14} {'Watchlist':>10} {'Trades':>8} {'Wins':>6} {'Losses':>8}")
    print(f"  {'-'*50}")
    for week_str, cnt in week_counts:
        week_trades = [t for t in all_trades if t['week'] == week_str]
        week_wins   = sum(1 for t in week_trades if t['outcome'] == 'TARGET_HIT')
        week_losses = sum(1 for t in week_trades if t['outcome'] == 'SL_HIT')
        bar = '█' * min(cnt, 20)
        print(f"  {week_str:<14} {cnt:>10} {len(week_trades):>8} {week_wins:>6} {week_losses:>8}  {bar}")

    # ── Individual trades ──────────────────────────────────────
    print(f"\n{'='*65}")
    print(f"  ALL TRADES DETAIL ({total_trades} trades)")
    print(f"{'='*65}")
    print(f"  {'Symbol':<12} {'Week':<12} {'Entry':>8} {'SL':>8} {'T1':>8} "
          f"{'Exit':>8} {'PnL%':>7} {'Outcome':<14}")
    print(f"  {'-'*82}")

    for t in sorted(all_trades, key=lambda x: x['signal_date']):
        outcome_str = (
            '✅ WIN'      if t['outcome'] == 'TARGET_HIT' else
            '❌ LOSS'     if t['outcome'] == 'SL_HIT'     else
            '⏱  TIME'
        )
        pnl_str = f"+{t['pnl_pct']}%" if t['pnl_pct'] > 0 else f"{t['pnl_pct']}%"
        print(f"  {t['symbol']:<12} {t['week']:<12} {t['entry']:>8} "
              f"{t['sl']:>8} {t['t1']:>8} {t['exit_price']:>8} "
              f"{pnl_str:>7} {outcome_str}")

    # ── Monthly breakdown ──────────────────────────────────────
    print(f"\n{'='*65}")
    print(f"  MONTHLY BREAKDOWN")
    print(f"{'='*65}")
    print(f"  {'Month':<10} {'Trades':>7} {'Wins':>6} {'Losses':>8} {'Win%':>7} {'PnL%':>8}")
    print(f"  {'-'*50}")
    monthly = defaultdict(list)
    for t in all_trades:
        month = t['signal_date'][:7]
        monthly[month].append(t)
    for month in sorted(monthly.keys()):
        mt     = monthly[month]
        mw     = sum(1 for t in mt if t['outcome'] == 'TARGET_HIT')
        ml     = sum(1 for t in mt if t['outcome'] == 'SL_HIT')
        mwr    = round(mw / len(mt) * 100, 1) if mt else 0
        mpnl   = round(sum(t['pnl_pct'] for t in mt), 2)
        print(f"  {month:<10} {len(mt):>7} {mw:>6} {ml:>8} {mwr:>6}% {mpnl:>7}%")

    print(f"\n{'='*65}")
    print(f"  VERDICT")
    print(f"{'='*65}")
    if win_rate >= 55 and avg_rr >= 1.5:
        print(f"  ✅ STRONG SETUP — Win rate {win_rate}% with R:R 1:{avg_rr}")
        print(f"     This setup has a positive expected value.")
    elif win_rate >= 45 and avg_rr >= 2.0:
        print(f"  ✅ GOOD SETUP — Win rate {win_rate}% compensated by R:R 1:{avg_rr}")
        print(f"     High R:R makes this profitable even at lower win rate.")
    elif win_rate >= 40:
        print(f"  ⚠️  MODERATE SETUP — Win rate {win_rate}%, R:R 1:{avg_rr}")
        print(f"     Consider tightening filters further.")
    else:
        print(f"  ❌ WEAK SETUP — Win rate {win_rate}%, R:R 1:{avg_rr}")
        print(f"     Filters need adjustment.")
    print(f"{'='*65}\n")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Camarilla Weekly Bullish Backtest')
    parser.add_argument('--months', type=int, default=12,
                        help='Number of months to backtest (default: 12)')
    args = parser.parse_args()
    run_backtest(months=args.months)
