#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  index_options_backtest.py
#  Backtests Opening Range Breakout + Camarilla confirmation
#  for BankNifty and Nifty 50 index options (intraday)
#
#  Setup:
#    9:15 AM → First candle forms (Opening Range)
#    9:30 AM → Check conditions:
#
#    CALL (Bullish):
#      Price above H3 (Camarilla bullish zone)
#      Gap up > 0 from yesterday close
#      First candle is bullish (close > open)
#      First candle range > avg first candle range
#      → Buy ATM CALL at open
#      → Target: H4 hit intraday
#      → SL: Price closes below H3
#
#    PUT (Bearish):
#      Price below L3 (Camarilla bearish zone)
#      Gap down > 0 from yesterday close
#      First candle is bearish (close < open)
#      First candle range > avg first candle range
#      → Buy ATM PUT at open
#      → Target: L4 hit intraday
#      → SL: Price closes above L3
#
#  Exit: Target / SL / 3:15 PM (hard exit)
#
#  Options simulation:
#    BankNifty ATM delta ≈ 0.50
#    1 point index move = ₹0.50 option move (per share)
#    Lot size = 15
#    Target hit → option gain based on H4-H3 / L3-L4 distance
#    SL hit     → option loss based on entry-SL distance
#
#  Usage:
#    nohup python3 index_options_backtest.py > logs/index_bt.log 2>&1 &
#    cat index_options_results.txt
# ═══════════════════════════════════════════════════════════════

import sqlite3
import os
from datetime import date, timedelta, datetime
from collections import defaultdict

BASE_DIR    = os.path.expanduser('~/nse-scanner')
DB_PATH     = os.path.join(BASE_DIR, 'nse_data.db')
OUTPUT_FILE = os.path.join(BASE_DIR, 'index_options_results.txt')

# ── Config ────────────────────────────────────────────────────
MONTHS          = 24
MIN_GAP_PCT     = 0.0    # minimum gap from yesterday close (0 = any direction ok)
MIN_RANGE_MULT  = 1.0    # first candle range >= avg first candle range
MAX_HOLD_DAYS   = 1      # intraday only

# BankNifty lot size and typical premium
BANKNIFTY_LOT   = 15
NIFTY_LOT       = 75

# ── Option P&L simulation ─────────────────────────────────────
# When target (H4/L4) hits intraday:
#   Index moves H3→H4 or L3→L4 = Range×1.1/4 points
#   ATM option gains ~60-80% of that move (delta + gamma)
#   We use 65% as conservative estimate

# When SL hits (price closes back below H3 / above L3):
#   Usually 30-40% of premium lost
#   We use 40% as conservative estimate

# Time exit (3:15 PM):
#   Partial move captured minus theta
#   We use actual % of target reached × 50% delta

def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def compute_camarilla(high, low, close):
    rng = high - low
    h1  = close + rng * 1.1 / 12
    h2  = close + rng * 1.1 / 6
    h3  = close + rng * 1.1 / 4
    h4  = close + rng * 1.1 / 2
    h5  = (high / low) * close if low > 0 else 0
    l1  = close - rng * 1.1 / 12
    l2  = close - rng * 1.1 / 6
    l3  = close - rng * 1.1 / 4
    l4  = close - rng * 1.1 / 2
    l5  = close - (h5 - close) if h5 > 0 else 0
    return {
        'h1':h1,'h2':h2,'h3':h3,'h4':h4,'h5':h5,
        'l1':l1,'l2':l2,'l3':l3,'l4':l4,'l5':l5,
        'range': rng
    }

def compute_ema(closes, period=20):
    if len(closes) < period:
        return None
    k   = 2 / (period + 1)
    ema = closes[0]
    for p in closes[1:]:
        ema = p * k + ema * (1 - k)
    return ema

# ═══════════════════════════════════════════════════════════════
#  LOAD DATA
# ═══════════════════════════════════════════════════════════════
def load_data(start_date):
    log("Loading index data...")
    conn = get_db()
    c    = conn.cursor()
    c.execute('''
        SELECT symbol, date, open, high, low, close, volume
        FROM   daily_prices
        WHERE  symbol IN ('BANKNIFTY', 'NIFTY50')
        AND    date >= ?
        ORDER  BY symbol, date
    ''', ((start_date - timedelta(days=60)).strftime('%Y-%m-%d'),))
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
        })

    for sym in price_data:
        log(f"  {sym}: {len(price_data[sym])} bars")

    return price_data

# ═══════════════════════════════════════════════════════════════
#  BACKTEST ONE INDEX
# ═══════════════════════════════════════════════════════════════
def backtest_index(sym, bars, start_date, end_date):
    trades = []

    for i in range(21, len(bars) - 1):
        bar      = bars[i]
        bar_date = bar['date']

        if bar_date < start_date.strftime('%Y-%m-%d'):
            continue
        if bar_date > end_date.strftime('%Y-%m-%d'):
            break

        prev = bars[i - 1]

        # Compute Camarilla from yesterday
        cam   = compute_camarilla(prev['high'], prev['low'], prev['close'])
        h3    = cam['h3']; h4 = cam['h4']; h5 = cam['h5']
        l3    = cam['l3']; l4 = cam['l4']; l5 = cam['l5']
        rng   = cam['range']

        # Today's open = proxy for 9:15 AM first candle open
        today_open  = bar['open']
        today_close = bar['close']
        today_high  = bar['high']
        today_low   = bar['low']

        # Gap from yesterday's close
        gap_pct = (today_open - prev['close']) / prev['close'] * 100

        # Avg first candle range (use 20-day avg of daily range as proxy)
        recent     = bars[max(0, i-20):i]
        avg_range  = sum(b['high']-b['low'] for b in recent) / len(recent)

        # First candle estimated range
        # Since we don't have 15-min data, we estimate:
        # First candle range ≈ 30-40% of daily range
        # We check if today's open-to-first-move is significant
        # Proxy: if today's open is beyond H3/L3 = strong opening

        # 20-day EMA
        ema20 = compute_ema([b['close'] for b in recent], 20)
        if ema20 is None:
            continue

        direction = None

        # ── CALL conditions ────────────────────────────────────
        # 1. Opens above H3 (above Camarilla bullish zone)
        # 2. Gap up from yesterday (positive opening bias)
        # 3. Today's first move is upward (open < high in first hour)
        # 4. Yesterday's trend was up (prev close > ema20)
        if (today_open > h3                    # opens above H3
                and gap_pct > 0                # gap up
                and today_open > prev['close'] # opens above yesterday close
                and prev['close'] >= ema20 * 0.98):  # uptrend
            direction = 'CALL'

        # ── PUT conditions ─────────────────────────────────────
        # 1. Opens below L3 (below Camarilla bearish zone)
        # 2. Gap down from yesterday (negative opening bias)
        # 3. Yesterday's trend was down
        elif (today_open < l3                  # opens below L3
                and gap_pct < 0                # gap down
                and today_open < prev['close'] # opens below yesterday close
                and prev['close'] <= ema20 * 1.02):  # downtrend
            direction = 'PUT'

        if direction is None:
            continue

        # ── Simulate trade outcome ─────────────────────────────
        if direction == 'CALL':
            entry       = today_open
            target_lvl  = h4           # first target
            sl_lvl      = h3           # SL — close below H3
            target_hit  = today_high  >= h4
            sl_hit      = today_close <  h3 and today_low < h3
            # Points move
            target_pts  = h4 - entry
            sl_pts      = entry - h3
            actual_pts  = today_close - entry

        else:  # PUT
            entry       = today_open
            target_lvl  = l4           # first target
            sl_lvl      = l3           # SL — close above L3
            target_hit  = today_low   <= l4
            sl_hit      = today_close >  l3 and today_high > l3
            # Points move
            target_pts  = entry - l4
            sl_pts      = l3 - entry
            actual_pts  = entry - today_close

        # ── Option P&L calculation ─────────────────────────────
        # ATM option price ≈ 0.4% of index for BankNifty, 0.35% for Nifty
        atm_pct     = 0.40 if sym == 'BANKNIFTY' else 0.35
        atm_premium = entry * atm_pct / 100

        if target_hit:
            outcome     = 'TARGET'
            # Option gain: index moves target_pts, delta 0.50 + gamma boost
            # Conservative: 65% of points captured in option
            opt_gain    = target_pts * 0.65
            opt_pnl_pct = round(opt_gain / atm_premium * 100, 1)

        elif sl_hit:
            outcome     = 'SL_HIT'
            # Option loss: index moves sl_pts against us, lose 40% of premium
            opt_loss    = atm_premium * 0.40
            opt_pnl_pct = round(-opt_loss / atm_premium * 100, 1)

        else:
            outcome     = 'TIME_EXIT'
            # Partial move captured, theta eats some
            if actual_pts > 0:
                opt_gain    = actual_pts * 0.50  # 50% delta
                opt_pnl_pct = round((opt_gain / atm_premium * 100) - 8, 1)
                # -8% theta for holding all day
            else:
                opt_pnl_pct = round(-atm_premium * 0.20 / atm_premium * 100, 1)

        # Stock % move
        if direction == 'CALL':
            stock_pct = round((today_close - entry) / entry * 100, 2)
        else:
            stock_pct = round((entry - today_close) / entry * 100, 2)

        trades.append({
            'symbol':      sym,
            'date':        bar_date,
            'direction':   direction,
            'entry':       round(entry, 0),
            'h3':          round(h3, 0),
            'h4':          round(h4, 0),
            'l3':          round(l3, 0),
            'l4':          round(l4, 0),
            'target_lvl':  round(target_lvl, 0),
            'sl_lvl':      round(sl_lvl, 0),
            'atm_premium': round(atm_premium, 0),
            'outcome':     outcome,
            'stock_pct':   stock_pct,
            'opt_pnl_pct': opt_pnl_pct,
            'gap_pct':     round(gap_pct, 2),
            'target_pts':  round(target_pts, 0),
            'sl_pts':      round(sl_pts, 0),
        })

    return trades

# ═══════════════════════════════════════════════════════════════
#  ANALYSIS
# ═══════════════════════════════════════════════════════════════
def analyse_all(all_trades, months):
    if not all_trades:
        return "No trades.", {}

    total    = len(all_trades)
    wins     = [t for t in all_trades if t['outcome'] == 'TARGET']
    losses   = [t for t in all_trades if t['outcome'] == 'SL_HIT']
    timeex   = [t for t in all_trades if t['outcome'] == 'TIME_EXIT']

    win_rate  = round(len(wins)   / total * 100, 1)
    loss_rate = round(len(losses) / total * 100, 1)
    time_rate = round(len(timeex) / total * 100, 1)

    avg_win   = round(sum(t['opt_pnl_pct'] for t in wins)   / len(wins),   1) if wins   else 0
    avg_loss  = round(sum(t['opt_pnl_pct'] for t in losses) / len(losses), 1) if losses else 0
    avg_time  = round(sum(t['opt_pnl_pct'] for t in timeex) / len(timeex), 1) if timeex else 0
    total_pnl = round(sum(t['opt_pnl_pct'] for t in all_trades), 1)
    exp_val   = round(total_pnl / total, 1)

    calls = [t for t in all_trades if t['direction'] == 'CALL']
    puts  = [t for t in all_trades if t['direction'] == 'PUT']
    call_wr = round(sum(1 for t in calls if t['outcome']=='TARGET') / len(calls) * 100, 1) if calls else 0
    put_wr  = round(sum(1 for t in puts  if t['outcome']=='TARGET') / len(puts)  * 100, 1) if puts  else 0

    # By index
    by_idx   = defaultdict(list)
    for t in all_trades:
        by_idx[t['symbol']].append(t)

    # Monthly
    monthly  = defaultdict(list)
    for t in all_trades:
        monthly[t['date'][:7]].append(t)

    # Signals per month
    trades_per_month = round(total / months, 1)

    lines = []
    lines.append(f"\n{'═'*70}")
    lines.append(f"  ORB + CAMARILLA — INDEX OPTIONS BACKTEST")
    lines.append(f"  Indices : BANKNIFTY (primary) + NIFTY50 (secondary)")
    lines.append(f"  Period  : {months} months")
    lines.append(f"  Setup   : Gap open beyond H3/L3 + ORB direction")
    lines.append(f"  Entry   : Market open (9:15 AM)")
    lines.append(f"  Target  : H4 (CALL) / L4 (PUT)")
    lines.append(f"  SL      : Close back below H3 / above L3")
    lines.append(f"  Exit    : 3:15 PM hard exit")
    lines.append(f"  Options : ATM monthly, delta ~0.50")
    lines.append(f"{'═'*70}")

    lines.append(f"\n  SIGNAL FREQUENCY")
    lines.append(f"  {'Total signals':<35}: {total}")
    lines.append(f"  {'Avg per month':<35}: {trades_per_month}")
    lines.append(f"  {'Avg per week':<35}: {round(trades_per_month/4.3, 1)}")
    lines.append(f"  {'CALL signals':<35}: {len(calls)}")
    lines.append(f"  {'PUT signals':<35}: {len(puts)}")

    lines.append(f"\n  TRADE OUTCOMES")
    lines.append(f"  {'Total trades':<35}: {total}")
    lines.append(f"  {'✅ Target hit (WIN)':<35}: {len(wins)} ({win_rate}%)")
    lines.append(f"  {'❌ SL hit (LOSS)':<35}: {len(losses)} ({loss_rate}%)")
    lines.append(f"  {'⏱  Time exit':<35}: {len(timeex)} ({time_rate}%)")
    lines.append(f"  {'CALL win rate':<35}: {call_wr}%")
    lines.append(f"  {'PUT win rate':<35}: {put_wr}%")

    lines.append(f"\n  OPTIONS P&L (simulated)")
    lines.append(f"  {'Avg win (option %)':<35}: +{avg_win}%")
    lines.append(f"  {'Avg loss (option %)':<35}: {avg_loss}%")
    lines.append(f"  {'Avg time exit (option %)':<35}: {avg_time}%")
    lines.append(f"  {'Expected value per trade':<35}: {exp_val:+.1f}%")
    lines.append(f"  {'Total cumulative PnL':<35}: {total_pnl:+.1f}%")

    lines.append(f"\n  ₹ ESTIMATE PER TRADE (BankNifty, 1 lot = 15 shares)")
    bn_trades = [t for t in all_trades if t['symbol'] == 'BANKNIFTY']
    if bn_trades:
        avg_prem  = round(sum(t['atm_premium'] for t in bn_trades) / len(bn_trades), 0)
        avg_lot   = avg_prem * BANKNIFTY_LOT
        avg_w_rs  = round(avg_lot * avg_win / 100, 0)
        avg_l_rs  = round(avg_lot * abs(avg_loss) / 100, 0)
        avg_ev_rs = round(avg_lot * exp_val / 100, 0)
        lines.append(f"  {'Avg ATM premium (BN)':<35}: ₹{avg_prem:.0f}")
        lines.append(f"  {'1 lot cost (BN)':<35}: ₹{avg_lot:.0f}")
        lines.append(f"  {'Avg win per lot':<35}: +₹{avg_w_rs:.0f}")
        lines.append(f"  {'Avg loss per lot':<35}: -₹{avg_l_rs:.0f}")
        lines.append(f"  {'Expected value per lot':<35}: ₹{avg_ev_rs:+.0f}")

    lines.append(f"\n  BY INDEX")
    lines.append(f"  {'Index':<14} {'Trades':>7} {'Wins':>6} {'Win%':>6} "
                 f"{'AvgOpt%':>9} {'Total%':>9}")
    lines.append(f"  {'-'*55}")
    for idx in ['BANKNIFTY', 'NIFTY50']:
        it   = by_idx[idx]
        if not it:
            continue
        iw   = sum(1 for t in it if t['outcome'] == 'TARGET')
        iwr  = round(iw / len(it) * 100, 1)
        iav  = round(sum(t['opt_pnl_pct'] for t in it) / len(it), 1)
        itot = round(sum(t['opt_pnl_pct'] for t in it), 1)
        lines.append(f"  {idx:<14} {len(it):>7} {iw:>6} {iwr:>5}% {iav:>+8.1f}% {itot:>+8.1f}%")

    lines.append(f"\n  MONTHLY BREAKDOWN")
    lines.append(f"  {'Month':<10} {'Trades':>7} {'Wins':>6} {'Win%':>6} "
                 f"{'AvgOpt%':>9} {'Calls':>7} {'Puts':>6}")
    lines.append(f"  {'-'*60}")
    for month in sorted(monthly.keys()):
        mt   = monthly[month]
        mw   = sum(1 for t in mt if t['outcome'] == 'TARGET')
        mwr  = round(mw / len(mt) * 100, 1)
        mav  = round(sum(t['opt_pnl_pct'] for t in mt) / len(mt), 1)
        mc   = sum(1 for t in mt if t['direction'] == 'CALL')
        mp   = sum(1 for t in mt if t['direction'] == 'PUT')
        lines.append(f"  {month:<10} {len(mt):>7} {mw:>6} {mwr:>5}% {mav:>+8.1f}% {mc:>7} {mp:>6}")

    lines.append(f"\n  ALL TRADES")
    lines.append(f"  {'Index':<12} {'Date':<12} {'Dir':<6} {'Entry':>7} "
                 f"{'SL':>7} {'Target':>7} {'Gap%':>6} {'Opt%':>7} Outcome")
    lines.append(f"  {'-'*78}")
    for t in sorted(all_trades, key=lambda x: x['date']):
        out = ('✅ WIN ' if t['outcome'] == 'TARGET'
               else '❌ LOSS' if t['outcome'] == 'SL_HIT'
               else '⏱  TIME')
        lines.append(
            f"  {t['symbol']:<12} {t['date']:<12} {t['direction']:<6} "
            f"{t['entry']:>7.0f} {t['sl_lvl']:>7.0f} {t['target_lvl']:>7.0f} "
            f"{t['gap_pct']:>+5.2f}% {t['opt_pnl_pct']:>+6.1f}% {out}"
        )

    # Year by year
    yearly = defaultdict(list)
    for t in all_trades:
        yearly[t['date'][:4]].append(t)

    lines.append(f"\n  YEAR-BY-YEAR")
    lines.append(f"  {'Year':<8} {'Trades':>7} {'Wins':>6} {'Win%':>6} "
                 f"{'AvgOpt%':>9} {'TotalOpt%':>11}")
    lines.append(f"  {'-'*52}")
    for yr in sorted(yearly.keys()):
        yt   = yearly[yr]
        yw   = sum(1 for t in yt if t['outcome'] == 'TARGET')
        ywr  = round(yw / len(yt) * 100, 1)
        yav  = round(sum(t['opt_pnl_pct'] for t in yt) / len(yt), 1)
        ytot = round(sum(t['opt_pnl_pct'] for t in yt), 1)
        lines.append(f"  {yr:<8} {len(yt):>7} {yw:>6} {ywr:>5}% {yav:>+8.1f}% {ytot:>+10.1f}%")

    # Out of sample
    cutoff   = (date.today() - timedelta(days=366)).strftime('%Y-%m-%d')
    oos      = [t for t in all_trades if t['date'] <  cutoff]
    ins      = [t for t in all_trades if t['date'] >= cutoff]

    lines.append(f"\n  OUT-OF-SAMPLE VALIDATION")
    lines.append(f"  {'-'*60}")
    for label, subset in [('Out-of-sample (year 1)', oos),
                           ('In-sample     (year 2)', ins)]:
        if not subset:
            continue
        sw   = sum(1 for t in subset if t['outcome'] == 'TARGET')
        swr  = round(sw / len(subset) * 100, 1)
        sav  = round(sum(t['opt_pnl_pct'] for t in subset) / len(subset), 1)
        stot = round(sum(t['opt_pnl_pct'] for t in subset), 1)
        lines.append(f"  {label}: {len(subset)} trades | "
                     f"Win:{swr}% | Avg:{sav:+.1f}% | Total:{stot:+.1f}%")

    if oos and ins:
        oos_wr = sum(1 for t in oos if t['outcome']=='TARGET') / len(oos) * 100
        ins_wr = sum(1 for t in ins if t['outcome']=='TARGET') / len(ins) * 100
        diff   = abs(oos_wr - ins_wr)
        if diff <= 10:
            lines.append(f"  ✅ Win rate diff {diff:.1f}% — setup is ROBUST")
        elif diff <= 20:
            lines.append(f"  ⚠️  Win rate diff {diff:.1f}% — some variance")
        else:
            lines.append(f"  ❌ Win rate diff {diff:.1f}% — high variance")

    # Verdict
    lines.append(f"\n{'═'*70}")
    if exp_val >= 15 and win_rate >= 55:
        verdict = "✅ EXCELLENT — Strong options setup"
    elif exp_val >= 8 and win_rate >= 50:
        verdict = "✅ GOOD — Profitable options setup, trade with confidence"
    elif exp_val >= 0:
        verdict = "⚠️  MARGINAL — Slightly profitable"
    else:
        verdict = "❌ UNPROFITABLE — Do not trade"

    lines.append(f"  VERDICT     : {verdict}")
    lines.append(f"  Win rate    : {win_rate}%")
    lines.append(f"  CALL win    : {call_wr}%")
    lines.append(f"  PUT win     : {put_wr}%")
    lines.append(f"  Exp value   : {exp_val:+.1f}% per trade")
    lines.append(f"  Avg/month   : {trades_per_month} signals")
    lines.append(f"{'═'*70}\n")

    return '\n'.join(lines), {
        'total': total, 'win_rate': win_rate,
        'call_wr': call_wr, 'put_wr': put_wr,
        'exp_val': exp_val, 'per_month': trades_per_month
    }

# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
def main():
    os.makedirs(os.path.join(BASE_DIR, 'logs'), exist_ok=True)

    end_date   = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=MONTHS * 30)

    log("=" * 60)
    log("  ORB + CAMARILLA INDEX OPTIONS BACKTEST")
    log(f"  Period : {start_date} to {end_date}")
    log(f"  Months : {MONTHS}")
    log("=" * 60)

    price_data = load_data(start_date)

    all_trades = []
    for sym in ['BANKNIFTY', 'NIFTY50']:
        bars = price_data.get(sym, [])
        if not bars:
            log(f"  No data for {sym}")
            continue
        log(f"Running backtest for {sym}...")
        trades = backtest_index(sym, bars, start_date, end_date)
        log(f"  {sym}: {len(trades)} trades")
        all_trades.extend(trades)

    log(f"\nTotal trades: {len(all_trades)}")

    report, summary = analyse_all(all_trades, MONTHS)

    log(f"\nSUMMARY:")
    log(f"  Total       : {summary['total']}")
    log(f"  Win rate    : {summary['win_rate']}%")
    log(f"  CALL win    : {summary['call_wr']}%")
    log(f"  PUT win     : {summary['put_wr']}%")
    log(f"  Exp value   : {summary['exp_val']:+.1f}%/trade")
    log(f"  Per month   : {summary['per_month']} signals")

    with open(OUTPUT_FILE, 'w') as f:
        f.write(report)

    log(f"\n✅ Saved: {OUTPUT_FILE}")
    log(f"   View : cat {OUTPUT_FILE}")

if __name__ == '__main__':
    main()
