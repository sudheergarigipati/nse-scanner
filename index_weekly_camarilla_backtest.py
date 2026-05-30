#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  index_weekly_camarilla_backtest.py
#  Weekly Camarilla L3/H3 setup on BankNifty and Nifty
#  Simulates buying CALL at L3 bounce / PUT at H3 fade
#  Hold 1-3 days (swing options, not intraday)
#
#  Setup:
#    CALL: Price touches weekly L3, closes above L3 (green candle)
#          → Buy ATM CALL, target H3, SL L4
#    PUT : Price touches weekly H3, closes below H3 (red candle)
#          → Buy ATM PUT, target L3, SL H4
#
#  Options simulation:
#    Weekly options (not monthly) — less theta decay
#    ATM delta 0.50
#    Hold max 3 days
#    Target hit → option gains based on H3-L3 distance
#    SL hit     → option loses 40% of premium
#    Time exit  → partial gain minus theta (8% per day)
#
#  Usage:
#    python3 index_weekly_camarilla_backtest.py
#    Results saved to: index_weekly_results.txt
# ═══════════════════════════════════════════════════════════════

import sqlite3
import os
from datetime import date, timedelta, datetime
from collections import defaultdict

BASE_DIR    = os.path.expanduser('~/nse-scanner')
DB_PATH     = os.path.join(BASE_DIR, 'nse_data.db')
OUTPUT_FILE = os.path.join(BASE_DIR, 'index_weekly_results.txt')

MONTHS      = 24
MAX_HOLD    = 3     # max 3 trading days for swing options

# Index-specific parameters
# Indices move 600-800 points daily so 0.15% is too tight
# 0.5% proximity = within 270 points for BankNifty at 54000
INDEX_MAX_DIST_PCT  = 0.5   # within 0.5% of L3/H3
INDEX_MIN_REWARD    = 1.5   # H3 must be 1.5%+ above L3
INDEX_MAX_SL_PCT    = 5.0   # slightly wider SL ok for indices
INDEX_MIN_RANGE_PCT = 1.0   # weekly range >= 1% of close
# No volume filter for indices (yfinance returns 0 volume)
# No CPR filter (indices don't have CPR in same way)

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

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
    return {'h3':h3,'h4':h4,'h5':h5,'l3':l3,'l4':l4,'l5':l5,'range':rng}

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

def get_mondays(start_date, end_date):
    mondays = []
    d = start_date
    while d.weekday() != 0:
        d += timedelta(days=1)
    while d <= end_date:
        mondays.append(d)
        d += timedelta(days=7)
    return mondays

# ═══════════════════════════════════════════════════════════════
#  BACKTEST ONE INDEX
# ═══════════════════════════════════════════════════════════════
def backtest_index(sym, bars, date_idx, start_date, end_date):
    trades     = []
    mondays    = get_mondays(start_date, end_date)

    for monday in mondays:
        week_start   = monday.strftime('%Y-%m-%d')
        prev_mon     = monday - timedelta(days=7)
        prev_fri     = monday - timedelta(days=3)
        prev_mon_str = prev_mon.strftime('%Y-%m-%d')
        prev_fri_str = prev_fri.strftime('%Y-%m-%d')

        # Get last week OHLCV
        prev_bars = [b for b in bars
                     if prev_mon_str <= b['date'] <= prev_fri_str]
        if len(prev_bars) < 3:
            continue

        prev_high  = max(b['high']  for b in prev_bars)
        prev_low   = min(b['low']   for b in prev_bars)
        prev_close = prev_bars[-1]['close']

        if prev_low <= 0:
            continue

        # Weekly range quality check
        range_pct = (prev_high - prev_low) / prev_close * 100
        if range_pct < INDEX_MIN_RANGE_PCT:
            continue

        # Compute weekly Camarilla
        cam = compute_camarilla(prev_high, prev_low, prev_close)
        h3, h4, h5 = cam['h3'], cam['h4'], cam['h5']
        l3, l4, l5 = cam['l3'], cam['l4'], cam['l5']

        # Minimum reward check
        reward_pct = (h3 - l3) / l3 * 100 if l3 > 0 else 0
        if reward_pct < INDEX_MIN_REWARD:
            continue

        # SL cap
        sl_pct = (l3 - l4) / l3 * 100 if l3 > 0 else 99
        if sl_pct > INDEX_MAX_SL_PCT:
            continue

        # Get this week's bars
        week_end_str = (monday + timedelta(days=6)).strftime('%Y-%m-%d')
        week_bars    = [b for b in bars
                        if week_start <= b['date'] <= week_end_str]
        if not week_bars:
            continue

        # 20-week EMA (100-day) for trend filter
        hist = [b for b in bars if b['date'] < week_start][-100:]
        if len(hist) < 20:
            continue
        ema20w = compute_ema([b['close'] for b in hist], 20)
        if ema20w is None:
            continue

        # Process each day of the week
        prior_bars_this_week = []

        for day_idx, bar in enumerate(week_bars):
            # Virgin check — L3/H3 not touched before today this week
            l3_touched = any(b['low']  <= l3 for b in prior_bars_this_week)
            h3_touched = any(b['high'] >= h3 for b in prior_bars_this_week)
            l4_broken  = any(b['low']  <  l4 for b in prior_bars_this_week + [bar])
            h4_broken  = any(b['high'] >  h4 for b in prior_bars_this_week + [bar])

            price = bar['close']

            # ── CALL setup ─────────────────────────────────────
            # Price touches L3 (within 0.5%), closes above L3
            # Green candle, virgin L3, L4 not broken
            # EMA trend: price should be generally in uptrend
            dist_to_l3 = abs(bar['low'] - l3) / l3 * 100 if l3 > 0 else 99

            if (not l3_touched
                    and not l4_broken
                    and bar['low']    <= l3 * 1.005  # touched or came within 0.5%
                    and bar['close']  >  l3
                    and bar['close']  >  bar['open']  # green candle
                    and price         >= ema20w * 0.97): # loose uptrend check

                entry_price = l3  # enter at the level

                # ATM weekly option premium ≈ 0.5% of index
                atm_pct     = 0.50 if sym == 'BANKNIFTY' else 0.45
                atm_premium = entry_price * atm_pct / 100

                # Simulate next MAX_HOLD bars for outcome
                trigger_idx = date_idx.get(bar['date'])
                if trigger_idx is None:
                    prior_bars_this_week.append(bar)
                    continue

                future_bars = bars[trigger_idx + 1:
                                   trigger_idx + 1 + MAX_HOLD]

                outcome    = 'TIME_EXIT'
                exit_price = bar['close']
                days_held  = 0

                for fb in future_bars:
                    days_held += 1
                    if fb['low'] <= l4:
                        outcome    = 'SL_HIT'
                        exit_price = l4
                        break
                    if fb['high'] >= h3:
                        outcome    = 'TARGET_HIT'
                        exit_price = h3
                        break
                    exit_price = fb['close']

                # Option P&L
                if outcome == 'TARGET_HIT':
                    pts_gained  = h3 - entry_price
                    opt_pnl_pct = round(pts_gained * 0.65 / atm_premium * 100, 1)
                elif outcome == 'SL_HIT':
                    opt_pnl_pct = -40.0
                else:
                    pts_gained  = exit_price - entry_price
                    if pts_gained > 0:
                        raw_gain    = pts_gained * 0.5 / atm_premium * 100
                        # Subtract theta (8% per day held)
                        opt_pnl_pct = round(raw_gain - (8 * days_held), 1)
                    else:
                        opt_pnl_pct = round(-8 * days_held - 5, 1)

                # ₹ P&L per lot
                lot_size = 15 if sym == 'BANKNIFTY' else 75
                lot_cost = atm_premium * lot_size
                rs_pnl   = round(lot_cost * opt_pnl_pct / 100, 0)

                trades.append({
                    'symbol':      sym,
                    'week':        week_start,
                    'date':        bar['date'],
                    'direction':   'CALL',
                    'entry':       round(entry_price, 0),
                    'sl':          round(l4, 0),
                    'target':      round(h3, 0),
                    'target2':     round(h4, 0),
                    'atm_premium': round(atm_premium, 0),
                    'lot_cost':    round(lot_cost, 0),
                    'outcome':     outcome,
                    'opt_pnl_pct': opt_pnl_pct,
                    'rs_pnl':      rs_pnl,
                    'days_held':   days_held,
                    'sl_pct':      round(sl_pct, 2),
                    'reward_pct':  round(reward_pct, 2),
                    'range_pct':   round(range_pct, 2),
                })

            # ── PUT setup ──────────────────────────────────────
            # Price touches H3 (within 0.5%), closes below H3
            # Red candle, virgin H3, H4 not broken
            elif (not h3_touched
                    and not h4_broken
                    and bar['high']  >= h3 * 0.995   # touched or came within 0.5%
                    and bar['close'] <  h3
                    and bar['close'] <  bar['open']   # red candle
                    and price        <= ema20w * 1.03): # loose downtrend check

                entry_price = h3

                atm_pct     = 0.50 if sym == 'BANKNIFTY' else 0.45
                atm_premium = entry_price * atm_pct / 100

                trigger_idx = date_idx.get(bar['date'])
                if trigger_idx is None:
                    prior_bars_this_week.append(bar)
                    continue

                future_bars = bars[trigger_idx + 1:
                                   trigger_idx + 1 + MAX_HOLD]

                outcome    = 'TIME_EXIT'
                exit_price = bar['close']
                days_held  = 0

                for fb in future_bars:
                    days_held += 1
                    if fb['high'] >= h4:
                        outcome    = 'SL_HIT'
                        exit_price = h4
                        break
                    if fb['low'] <= l3:
                        outcome    = 'TARGET_HIT'
                        exit_price = l3
                        break
                    exit_price = fb['close']

                if outcome == 'TARGET_HIT':
                    pts_gained  = entry_price - l3
                    opt_pnl_pct = round(pts_gained * 0.65 / atm_premium * 100, 1)
                elif outcome == 'SL_HIT':
                    opt_pnl_pct = -40.0
                else:
                    pts_gained  = entry_price - exit_price
                    if pts_gained > 0:
                        raw_gain    = pts_gained * 0.5 / atm_premium * 100
                        opt_pnl_pct = round(raw_gain - (8 * days_held), 1)
                    else:
                        opt_pnl_pct = round(-8 * days_held - 5, 1)

                lot_size = 15 if sym == 'BANKNIFTY' else 75
                lot_cost = atm_premium * lot_size
                rs_pnl   = round(lot_cost * opt_pnl_pct / 100, 0)

                trades.append({
                    'symbol':      sym,
                    'week':        week_start,
                    'date':        bar['date'],
                    'direction':   'PUT',
                    'entry':       round(entry_price, 0),
                    'sl':          round(h4, 0),
                    'target':      round(l3, 0),
                    'target2':     round(l4, 0),
                    'atm_premium': round(atm_premium, 0),
                    'lot_cost':    round(lot_cost, 0),
                    'outcome':     outcome,
                    'opt_pnl_pct': opt_pnl_pct,
                    'rs_pnl':      rs_pnl,
                    'days_held':   days_held,
                    'sl_pct':      round(sl_pct, 2),
                    'reward_pct':  round(reward_pct, 2),
                    'range_pct':   round(range_pct, 2),
                })

            prior_bars_this_week.append(bar)

    return trades

# ═══════════════════════════════════════════════════════════════
#  ANALYSIS
# ═══════════════════════════════════════════════════════════════
def analyse(all_trades, months):
    if not all_trades:
        return "No trades.", {}

    total   = len(all_trades)
    wins    = [t for t in all_trades if t['outcome'] == 'TARGET_HIT']
    losses  = [t for t in all_trades if t['outcome'] == 'SL_HIT']
    timeex  = [t for t in all_trades if t['outcome'] == 'TIME_EXIT']
    calls   = [t for t in all_trades if t['direction'] == 'CALL']
    puts    = [t for t in all_trades if t['direction'] == 'PUT']

    win_rate  = round(len(wins)   / total * 100, 1)
    loss_rate = round(len(losses) / total * 100, 1)
    time_rate = round(len(timeex) / total * 100, 1)
    avg_win   = round(sum(t['opt_pnl_pct'] for t in wins)   / len(wins),   1) if wins   else 0
    avg_loss  = round(sum(t['opt_pnl_pct'] for t in losses) / len(losses), 1) if losses else 0
    avg_time  = round(sum(t['opt_pnl_pct'] for t in timeex) / len(timeex), 1) if timeex else 0
    total_pnl = round(sum(t['opt_pnl_pct'] for t in all_trades), 1)
    exp_val   = round(total_pnl / total, 1)
    call_wr   = round(sum(1 for t in calls if t['outcome']=='TARGET_HIT') / len(calls) * 100, 1) if calls else 0
    put_wr    = round(sum(1 for t in puts  if t['outcome']=='TARGET_HIT') / len(puts)  * 100, 1) if puts  else 0

    # By index
    by_idx  = defaultdict(list)
    for t in all_trades:
        by_idx[t['symbol']].append(t)

    # Monthly
    monthly = defaultdict(list)
    for t in all_trades:
        monthly[t['date'][:7]].append(t)

    # Yearly
    yearly  = defaultdict(list)
    for t in all_trades:
        yearly[t['date'][:4]].append(t)

    # ₹ estimates
    bn_trades  = [t for t in all_trades if t['symbol'] == 'BANKNIFTY']
    avg_prem_bn= round(sum(t['atm_premium'] for t in bn_trades) / len(bn_trades), 0) if bn_trades else 0
    lot_cost_bn= avg_prem_bn * 15
    avg_win_rs = round(lot_cost_bn * avg_win / 100, 0)
    avg_los_rs = round(lot_cost_bn * abs(avg_loss) / 100, 0)
    avg_ev_rs  = round(lot_cost_bn * exp_val / 100, 0)

    # Out-of-sample
    cutoff = (date.today() - timedelta(days=366)).strftime('%Y-%m-%d')
    oos    = [t for t in all_trades if t['date'] <  cutoff]
    ins    = [t for t in all_trades if t['date'] >= cutoff]

    lines = []
    lines.append(f"\n{'═'*68}")
    lines.append(f"  WEEKLY CAMARILLA L3/H3 — INDEX OPTIONS BACKTEST")
    lines.append(f"  Indices : BANKNIFTY + NIFTY50")
    lines.append(f"  Period  : {months} months")
    lines.append(f"  Setup   : Weekly L3 bounce (CALL) + H3 fade (PUT)")
    lines.append(f"  Entry   : L3 (CALL) / H3 (PUT) — limit order")
    lines.append(f"  Target  : H3 (CALL) / L3 (PUT)")
    lines.append(f"  SL      : L4 (CALL) / H4 (PUT)")
    lines.append(f"  Options : Weekly ATM, hold max {MAX_HOLD} days")
    lines.append(f"  R:R     : 2:1 built-in by Camarilla formula")
    lines.append(f"{'═'*68}")

    lines.append(f"\n  SIGNAL FREQUENCY")
    lines.append(f"  {'Total trades':<35}: {total}")
    lines.append(f"  {'Avg per month':<35}: {round(total/months, 1)}")
    lines.append(f"  {'Avg per week':<35}: {round(total/months/4.3, 1)}")
    lines.append(f"  {'CALL trades':<35}: {len(calls)}")
    lines.append(f"  {'PUT trades':<35}: {len(puts)}")

    lines.append(f"\n  TRADE OUTCOMES")
    lines.append(f"  {'Total trades':<35}: {total}")
    lines.append(f"  {'✅ Target hit (WIN)':<35}: {len(wins)} ({win_rate}%)")
    lines.append(f"  {'❌ SL hit (LOSS)':<35}: {len(losses)} ({loss_rate}%)")
    lines.append(f"  {'⏱  Time exit':<35}: {len(timeex)} ({time_rate}%)")
    lines.append(f"  {'CALL win rate':<35}: {call_wr}%")
    lines.append(f"  {'PUT win rate':<35}: {put_wr}%")

    lines.append(f"\n  OPTIONS P&L")
    lines.append(f"  {'Avg win (option %)':<35}: +{avg_win}%")
    lines.append(f"  {'Avg loss (option %)':<35}: {avg_loss}%")
    lines.append(f"  {'Avg time exit (option %)':<35}: {avg_time}%")
    lines.append(f"  {'Expected value per trade':<35}: {exp_val:+.1f}%")
    lines.append(f"  {'Total cumulative PnL':<35}: {total_pnl:+.1f}%")

    lines.append(f"\n  ₹ PER TRADE (BankNifty, 1 lot = 15 shares)")
    lines.append(f"  {'Avg ATM premium':<35}: ₹{avg_prem_bn}")
    lines.append(f"  {'1 lot cost':<35}: ₹{lot_cost_bn}")
    lines.append(f"  {'Avg win per lot':<35}: +₹{avg_win_rs}")
    lines.append(f"  {'Avg loss per lot':<35}: -₹{avg_los_rs}")
    lines.append(f"  {'Expected value per lot':<35}: ₹{avg_ev_rs:+}")
    if lot_cost_bn > 0:
        trades_to_recoup = round(avg_los_rs / max(avg_win_rs, 1), 1)
        lines.append(f"  {'Wins needed to cover 1 loss':<35}: {trades_to_recoup} trades")

    lines.append(f"\n  BY INDEX")
    lines.append(f"  {'Index':<14} {'Trades':>7} {'Wins':>6} {'Win%':>6} "
                 f"{'AvgOpt%':>9} {'Total%':>9}")
    lines.append(f"  {'-'*55}")
    for idx in ['BANKNIFTY', 'NIFTY50']:
        it   = by_idx[idx]
        if not it:
            continue
        iw   = sum(1 for t in it if t['outcome'] == 'TARGET_HIT')
        iwr  = round(iw / len(it) * 100, 1)
        iav  = round(sum(t['opt_pnl_pct'] for t in it) / len(it), 1)
        itot = round(sum(t['opt_pnl_pct'] for t in it), 1)
        lines.append(f"  {idx:<14} {len(it):>7} {iw:>6} {iwr:>5}% {iav:>+8.1f}% {itot:>+8.1f}%")

    lines.append(f"\n  MONTHLY BREAKDOWN")
    lines.append(f"  {'Month':<10} {'Trades':>7} {'Wins':>6} {'Win%':>6} "
                 f"{'AvgOpt%':>9} {'Calls':>7} {'Puts':>6}")
    lines.append(f"  {'-'*58}")
    for month in sorted(monthly.keys()):
        mt  = monthly[month]
        mw  = sum(1 for t in mt if t['outcome'] == 'TARGET_HIT')
        mwr = round(mw / len(mt) * 100, 1)
        mav = round(sum(t['opt_pnl_pct'] for t in mt) / len(mt), 1)
        mc  = sum(1 for t in mt if t['direction'] == 'CALL')
        mp  = sum(1 for t in mt if t['direction'] == 'PUT')
        lines.append(f"  {month:<10} {len(mt):>7} {mw:>6} {mwr:>5}% {mav:>+8.1f}% {mc:>7} {mp:>6}")

    lines.append(f"\n  YEAR-BY-YEAR")
    lines.append(f"  {'Year':<8} {'Trades':>7} {'Wins':>6} {'Win%':>6} "
                 f"{'AvgOpt%':>9} {'TotalOpt%':>11}")
    lines.append(f"  {'-'*52}")
    for yr in sorted(yearly.keys()):
        yt   = yearly[yr]
        yw   = sum(1 for t in yt if t['outcome'] == 'TARGET_HIT')
        ywr  = round(yw / len(yt) * 100, 1)
        yav  = round(sum(t['opt_pnl_pct'] for t in yt) / len(yt), 1)
        ytot = round(sum(t['opt_pnl_pct'] for t in yt), 1)
        lines.append(f"  {yr:<8} {len(yt):>7} {yw:>6} {ywr:>5}% {yav:>+8.1f}% {ytot:>+10.1f}%")

    lines.append(f"\n  OUT-OF-SAMPLE VALIDATION")
    lines.append(f"  {'-'*60}")
    for label, subset in [('Out-of-sample (year 1)', oos),
                           ('In-sample     (year 2)', ins)]:
        if not subset:
            continue
        sw   = sum(1 for t in subset if t['outcome'] == 'TARGET_HIT')
        swr  = round(sw / len(subset) * 100, 1)
        sav  = round(sum(t['opt_pnl_pct'] for t in subset) / len(subset), 1)
        stot = round(sum(t['opt_pnl_pct'] for t in subset), 1)
        lines.append(f"  {label}: {len(subset)} trades | "
                     f"Win:{swr}% | Avg:{sav:+.1f}% | Total:{stot:+.1f}%")
    if oos and ins:
        oos_wr = sum(1 for t in oos if t['outcome']=='TARGET_HIT') / len(oos) * 100
        ins_wr = sum(1 for t in ins if t['outcome']=='TARGET_HIT') / len(ins) * 100
        diff   = abs(oos_wr - ins_wr)
        if diff <= 10:
            lines.append(f"  ✅ Win rate diff {diff:.1f}% — ROBUST setup")
        elif diff <= 20:
            lines.append(f"  ⚠️  Win rate diff {diff:.1f}% — some variance")
        else:
            lines.append(f"  ❌ Win rate diff {diff:.1f}% — high variance")

    lines.append(f"\n  ALL TRADES")
    lines.append(f"  {'Sym':<12} {'Date':<12} {'Dir':<6} {'Entry':>7} "
                 f"{'SL':>7} {'T1':>7} {'Prem':>6} {'Opt%':>8} {'₹/lot':>8} Outcome")
    lines.append(f"  {'-'*85}")
    for t in sorted(all_trades, key=lambda x: x['date']):
        out = ('✅ WIN ' if t['outcome'] == 'TARGET_HIT'
               else '❌ LOSS' if t['outcome'] == 'SL_HIT'
               else '⏱  TIME')
        lines.append(
            f"  {t['symbol']:<12} {t['date']:<12} {t['direction']:<6} "
            f"{t['entry']:>7.0f} {t['sl']:>7.0f} {t['target']:>7.0f} "
            f"{t['atm_premium']:>6.0f} {t['opt_pnl_pct']:>+7.1f}% "
            f"{t['rs_pnl']:>+8.0f} {out}"
        )

    # Verdict
    lines.append(f"\n{'═'*68}")
    if exp_val >= 15 and win_rate >= 60:
        verdict = "✅ EXCELLENT — Strong setup, trade with confidence"
    elif exp_val >= 8 and win_rate >= 55:
        verdict = "✅ GOOD — Profitable setup"
    elif exp_val >= 0:
        verdict = "⚠️  MARGINAL — Slightly profitable, monitor carefully"
    else:
        verdict = "❌ UNPROFITABLE — Do not trade"

    lines.append(f"  VERDICT     : {verdict}")
    lines.append(f"  Win rate    : {win_rate}%")
    lines.append(f"  CALL win    : {call_wr}%")
    lines.append(f"  PUT win     : {put_wr}%")
    lines.append(f"  Exp value   : {exp_val:+.1f}% per trade")
    lines.append(f"  ₹ per lot   : {avg_ev_rs:+} expected per trade")
    lines.append(f"  Avg/month   : {round(total/months, 1)} signals")
    lines.append(f"{'═'*68}\n")

    return '\n'.join(lines), {
        'total': total, 'win_rate': win_rate,
        'call_wr': call_wr, 'put_wr': put_wr,
        'exp_val': exp_val, 'per_month': round(total/months, 1),
        'avg_ev_rs': avg_ev_rs,
    }

# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
def main():
    os.makedirs(os.path.join(BASE_DIR, 'logs'), exist_ok=True)

    end_date   = date.today() - timedelta(days=7)
    start_date = end_date - timedelta(days=MONTHS * 30)

    log("=" * 60)
    log("  WEEKLY CAMARILLA INDEX OPTIONS BACKTEST")
    log(f"  Period : {start_date} to {end_date} ({MONTHS} months)")
    log("=" * 60)

    conn = get_db()
    c    = conn.cursor()

    all_trades = []

    for sym in ['BANKNIFTY', 'NIFTY50']:
        c.execute('''
            SELECT date, open, high, low, close
            FROM   daily_prices
            WHERE  symbol = ? AND date >= ?
            ORDER  BY date
        ''', (sym, (start_date - timedelta(days=120)).strftime('%Y-%m-%d')))
        bars = c.fetchall()
        bars = [dict(b) for b in bars]

        # Build date index
        date_idx = {b['date']: i for i, b in enumerate(bars)}

        log(f"Processing {sym} ({len(bars)} bars)...")
        trades = backtest_index(sym, bars, date_idx, start_date, end_date)
        log(f"  {sym}: {len(trades)} trades found")
        all_trades.extend(trades)

    conn.close()

    log(f"\nTotal trades: {len(all_trades)}")
    report, summary = analyse(all_trades, MONTHS)

    log(f"\n{'='*50}")
    log(f"  SUMMARY")
    log(f"{'='*50}")
    log(f"  Total trades  : {summary['total']}")
    log(f"  Win rate      : {summary['win_rate']}%")
    log(f"  CALL win rate : {summary['call_wr']}%")
    log(f"  PUT win rate  : {summary['put_wr']}%")
    log(f"  Exp value     : {summary['exp_val']:+.1f}% per trade")
    log(f"  ₹ per lot     : {summary['avg_ev_rs']:+} per trade")
    log(f"  Per month     : {summary['per_month']} signals")
    log(f"{'='*50}")

    with open(OUTPUT_FILE, 'w') as f:
        f.write(report)

    log(f"\n✅ Results: {OUTPUT_FILE}")
    log(f"   View   : cat {OUTPUT_FILE}")

if __name__ == '__main__':
    main()
