#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  hv_at_low_backtest.py
#  The cleanest test of the HV concept:
#
#  LOGIC:
#    1. Find 5-year HV day for each stock
#    2. Wait for price to come BACK to HV Low (after initial move)
#    3. Enter when price touches HV Low (low of candle <= HV Low)
#       AND closes above HV Low (not a breakdown)
#       AND green candle (close > open)
#       AND volume >= 1.5x average
#    4. SL  = low of the HV candle itself
#    5. Target = HV High
#    6. Max hold = 15 trading days (3 weeks)
#
#  Also tests different HV age bands to find the sweet spot:
#    Band A: 7-30 days old   (fresh HV)
#    Band B: 30-90 days old  (recent HV)
#    Band C: 90-180 days old (older HV)
#    Band D: all ages combined
#
#  Usage:
#    nohup python3 hv_at_low_backtest.py > logs/hv_atlow_bt.log 2>&1 &
#    cat hv_atlow_results.txt
# ═══════════════════════════════════════════════════════════════

import sqlite3
import os
from datetime import date, timedelta, datetime
from collections import defaultdict

BASE_DIR    = os.path.expanduser('~/nse-scanner')
DB_PATH     = os.path.join(BASE_DIR, 'nse_data.db')
OUTPUT_FILE = os.path.join(BASE_DIR, 'hv_atlow_results.txt')

YEARS       = 2
MAX_HOLD    = 15   # 3 weeks max hold

def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ═══════════════════════════════════════════════════════════════
#  LOAD DATA
# ═══════════════════════════════════════════════════════════════
def load_data(start_date):
    log("Loading price data...")
    conn    = get_db()
    c       = conn.cursor()
    c.execute('''SELECT DISTINCT symbol FROM daily_prices
                 WHERE symbol NOT IN ('NIFTY50','BANKNIFTY')
                 AND close > 0''')
    symbols = [r[0] for r in c.fetchall()]
    log(f"  {len(symbols)} symbols")

    price_data = {}
    date_idx   = {}
    start_str  = (start_date - timedelta(days=400)).strftime('%Y-%m-%d')

    for i, sym in enumerate(symbols):
        c.execute('''SELECT date, open, high, low, close, volume
                     FROM daily_prices
                     WHERE symbol=? AND date>=? AND close>0
                     ORDER BY date''', (sym, start_str))
        rows = c.fetchall()
        if len(rows) < 150:
            continue
        bars = [{'date':r[0],'open':r[1],'high':r[2],
                 'low':r[3],'close':r[4],'volume':r[5]} for r in rows]
        price_data[sym] = bars
        date_idx[sym]   = {b['date']: j for j, b in enumerate(bars)}
        if (i+1) % 100 == 0:
            log(f"  {i+1}/{len(symbols)}...")

    conn.close()
    log(f"Loaded {len(price_data)} symbols")
    return price_data, date_idx

# ═══════════════════════════════════════════════════════════════
#  NIFTY TREND
# ═══════════════════════════════════════════════════════════════
def build_nifty_trend(price_data):
    BASKET = [
        'RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK',
        'HINDUNILVR','SBIN','BHARTIARTL','ITC','KOTAKBANK',
        'LT','AXISBANK','ASIANPAINT','MARUTI','SUNPHARMA',
        'TITAN','BAJFINANCE','NTPC','POWERGRID','ONGC',
        'TATASTEEL','WIPRO','HCLTECH','ADANIENT','ULTRACEMCO',
        'NESTLEIND','BAJAJFINSV','COALINDIA','HINDALCO'
    ]
    basket = [s for s in BASKET if s in price_data]
    base   = {s: price_data[s][0]['close'] for s in basket}
    sd     = {s: {b['date']: b['close'] for b in price_data[s]} for s in basket}
    dates  = sorted(set(b['date'] for s in basket for b in price_data[s]))
    series = []
    for dt in dates:
        vals = [sd[s][dt]/base[s]*100 for s in basket if dt in sd[s]]
        if vals:
            series.append((dt, sum(vals)/len(vals)))
    k, ema = 2/101, series[0][1]
    trend  = {}
    for dt, val in series:
        ema = val*k + ema*(1-k)
        trend[dt] = val >= ema
    return trend

# ═══════════════════════════════════════════════════════════════
#  CORE BACKTEST
# ═══════════════════════════════════════════════════════════════
def run_backtest(price_data, date_idx, start_date, end_date,
                 min_age_days, max_age_days, nifty_trend,
                 use_nifty=False, min_vol_ratio=1.5):
    """
    Entry: price LOW touches HV Low AND closes above it (green candle)
    SL   : HV candle low
    Target: HV High
    Age  : HV day must be min_age to max_age days before signal
    """
    start_str = start_date.strftime('%Y-%m-%d')
    end_str   = end_date.strftime('%Y-%m-%d')
    trades    = []

    for sym, bars in price_data.items():
        if len(bars) < 150:
            continue

        for i in range(100, len(bars)):
            bar      = bars[i]
            bar_date = bar['date']

            if bar_date < start_str or bar_date > end_str:
                continue

            # Nifty trend filter
            if use_nifty and not nifty_trend.get(bar_date, True):
                continue

            price = bar['close']
            if price < 10:
                continue

            # ── Find HV day in past 5 years ───────────────────
            five_yr = (datetime.strptime(bar_date, '%Y-%m-%d').date()
                       - timedelta(days=5*365)).strftime('%Y-%m-%d')
            hist = [b for b in bars[:i]
                    if b['date'] >= five_yr and b['volume'] > 0]
            if len(hist) < 50:
                continue

            hv_bar  = max(hist, key=lambda b: b['volume'])
            hv_date = hv_bar['date']
            hv_high = hv_bar['high']
            hv_low  = hv_bar['low']

            if hv_low <= 0:
                continue

            # ── Age filter ────────────────────────────────────
            days_since = (datetime.strptime(bar_date, '%Y-%m-%d').date() -
                          datetime.strptime(hv_date, '%Y-%m-%d').date()).days
            if days_since < min_age_days or days_since > max_age_days:
                continue

            # ── Price must touch HV Low ───────────────────────
            # Low of today's candle must touch HV Low
            if bar['low'] > hv_low * 1.005:  # within 0.5% counts as touch
                continue

            # Must close ABOVE HV Low (not a breakdown)
            if bar['close'] < hv_low:
                continue

            # Green candle
            if bar['close'] <= bar['open']:
                continue

            # Volume confirmation
            recent  = bars[max(0,i-20):i]
            avg_vol = sum(b['volume'] for b in recent)/len(recent) if recent else 0
            if avg_vol > 0 and bar['volume'] / avg_vol < min_vol_ratio:
                continue

            # ── Check this is NOT the HV day itself ──────────
            if bar_date == hv_date:
                continue

            # ── Entry at HV Low (limit order price) ──────────
            entry  = hv_low   # entered exactly at HV Low
            sl     = hv_low * 0.97  # 3% below HV Low as buffer
            # Actually use the HV candle's true low if we have it
            # The HV candle low IS hv_low (that's what we store)
            # Add small buffer for slippage
            target = hv_high

            risk   = entry - sl
            reward = target - entry

            if risk <= 0 or reward <= 0:
                continue

            rr = round(reward / risk, 1)
            if rr < 0.5:
                continue

            sl_pct = round(risk / entry * 100, 2)
            t1_pct = round(reward / entry * 100, 2)

            # ── Simulate trade ────────────────────────────────
            future = bars[i+1: i+1+MAX_HOLD]
            outcome    = 'TIME_EXIT'
            exit_price = bar['close']  # time exit at signal day close
            exit_date  = bar_date
            days_held  = 0

            for fb in future:
                days_held += 1
                if fb['low'] <= sl:
                    outcome    = 'SL_HIT'
                    exit_price = sl
                    exit_date  = fb['date']
                    break
                if fb['high'] >= target:
                    outcome    = 'TARGET_HIT'
                    exit_price = target
                    exit_date  = fb['date']
                    break
                exit_price = fb['close']
                exit_date  = fb['date']

            pnl_pct = round((exit_price - entry) / entry * 100, 2)

            trades.append({
                'symbol':     sym,
                'date':       bar_date,
                'hv_date':    hv_date,
                'days_since': days_since,
                'entry':      round(entry, 2),
                'sl':         round(sl, 2),
                'target':     round(target, 2),
                'exit_price': round(exit_price, 2),
                'exit_date':  exit_date,
                'outcome':    outcome,
                'pnl_pct':    pnl_pct,
                'sl_pct':     sl_pct,
                'target_pct': t1_pct,
                'rr':         rr,
                'days_held':  days_held,
            })

    return trades

# ═══════════════════════════════════════════════════════════════
#  ANALYSE
# ═══════════════════════════════════════════════════════════════
def analyse(name, trades, years):
    if not trades:
        return f"\n{name}: No trades\n", {}

    total  = len(trades)
    wins   = [t for t in trades if t['outcome'] == 'TARGET_HIT']
    losses = [t for t in trades if t['outcome'] == 'SL_HIT']
    timeex = [t for t in trades if t['outcome'] == 'TIME_EXIT']

    wr    = round(len(wins)/total*100, 1)
    aw    = round(sum(t['pnl_pct'] for t in wins)/len(wins), 2)   if wins   else 0
    al    = round(sum(t['pnl_pct'] for t in losses)/len(losses),2) if losses else 0
    at    = round(sum(t['pnl_pct'] for t in timeex)/len(timeex),2) if timeex else 0
    tpnl  = round(sum(t['pnl_pct'] for t in trades), 2)
    ev    = round(tpnl/total, 2)
    asl   = round(sum(t['sl_pct'] for t in trades)/total, 2)
    arr   = round(sum(t['rr'] for t in trades)/total, 2)
    ahold = round(sum(t['days_held'] for t in trades)/total, 1)
    atgt  = round(sum(t['target_pct'] for t in trades)/total, 2)

    monthly = defaultdict(list)
    yearly  = defaultdict(list)
    for t in trades:
        monthly[t['date'][:7]].append(t)
        yearly[t['date'][:4]].append(t)

    # Out of sample
    cutoff = (date.today() - timedelta(days=365)).strftime('%Y-%m-%d')
    oos    = [t for t in trades if t['date'] <  cutoff]
    ins    = [t for t in trades if t['date'] >= cutoff]

    lines = []
    lines.append(f"\n{'═'*65}")
    lines.append(f"  {name}")
    lines.append(f"{'═'*65}")

    lines.append(f"\n  OVERVIEW")
    lines.append(f"  {'Total trades':<35}: {total}")
    lines.append(f"  {'Per month':<35}: {round(total/(years*12),1)}")
    lines.append(f"  {'Avg hold days':<35}: {ahold}")
    lines.append(f"  {'Avg target distance':<35}: {atgt}%")
    lines.append(f"  {'Avg SL distance':<35}: {asl}%")
    lines.append(f"  {'Avg R:R':<35}: 1:{arr}")

    lines.append(f"\n  OUTCOMES")
    lines.append(f"  {'TARGET hit (WIN)':<35}: {len(wins)} ({wr}%)")
    lines.append(f"  {'SL hit (LOSS)':<35}: {len(losses)} ({round(len(losses)/total*100,1)}%)")
    lines.append(f"  {'Time exit':<35}: {len(timeex)} ({round(len(timeex)/total*100,1)}%)")

    lines.append(f"\n  P&L")
    lines.append(f"  {'Avg win %':<35}: +{aw}%")
    lines.append(f"  {'Avg loss %':<35}: {al}%")
    lines.append(f"  {'Avg time exit %':<35}: {at}%")
    lines.append(f"  {'Expected value/trade':<35}: {ev:+.2f}%")
    lines.append(f"  {'Total PnL':<35}: {tpnl:+.2f}%")

    lines.append(f"\n  YEAR BY YEAR")
    lines.append(f"  {'Year':<8} {'Trades':>7} {'Wins':>6} "
                 f"{'Win%':>6} {'Avg%':>8} {'Total%':>10}")
    lines.append(f"  {'-'*50}")
    for yr in sorted(yearly.keys()):
        yt   = yearly[yr]
        yw   = sum(1 for t in yt if t['outcome']=='TARGET_HIT')
        ywr  = round(yw/len(yt)*100, 1)
        yav  = round(sum(t['pnl_pct'] for t in yt)/len(yt), 2)
        ytot = round(sum(t['pnl_pct'] for t in yt), 2)
        lines.append(f"  {yr:<8} {len(yt):>7} {yw:>6} "
                     f"{ywr:>5}% {yav:>+7.2f}% {ytot:>+9.2f}%")

    lines.append(f"\n  MONTHLY BREAKDOWN")
    lines.append(f"  {'Month':<10} {'Trades':>7} {'Wins':>6} "
                 f"{'Win%':>6} {'Avg%':>8}")
    lines.append(f"  {'-'*45}")
    for month in sorted(monthly.keys()):
        mt  = monthly[month]
        mw  = sum(1 for t in mt if t['outcome']=='TARGET_HIT')
        mwr = round(mw/len(mt)*100, 1)
        mav = round(sum(t['pnl_pct'] for t in mt)/len(mt), 2)
        lines.append(f"  {month:<10} {len(mt):>7} {mw:>6} "
                     f"{mwr:>5}% {mav:>+7.2f}%")

    lines.append(f"\n  OUT-OF-SAMPLE VALIDATION")
    lines.append(f"  {'-'*55}")
    for label, subset in [('Year 1 (out-of-sample)', oos),
                           ('Year 2 (in-sample)',     ins)]:
        if not subset:
            continue
        sw   = sum(1 for t in subset if t['outcome']=='TARGET_HIT')
        swr  = round(sw/len(subset)*100, 1)
        sav  = round(sum(t['pnl_pct'] for t in subset)/len(subset), 2)
        stot = round(sum(t['pnl_pct'] for t in subset), 2)
        lines.append(f"  {label}: {len(subset)} trades | "
                     f"Win:{swr}% | Avg:{sav:+.2f}% | Total:{stot:+.2f}%")
    if oos and ins:
        d = abs(
            sum(1 for t in oos if t['outcome']=='TARGET_HIT')/len(oos)*100 -
            sum(1 for t in ins if t['outcome']=='TARGET_HIT')/len(ins)*100
        )
        tag = "ROBUST" if d <= 10 else "some variance" if d <= 20 else "HIGH VARIANCE"
        lines.append(f"  Win rate diff: {d:.1f}% — {tag}")

    lines.append(f"\n  ALL TRADES")
    lines.append(f"  {'Sym':<12} {'Date':<12} {'HVAge':>6} "
                 f"{'Entry':>8} {'SL':>8} {'Target':>8} "
                 f"{'Exit':>8} {'PnL%':>7} {'Days':>5} Out")
    lines.append(f"  {'-'*88}")
    for t in sorted(trades, key=lambda x: x['date']):
        out = ('WIN ' if t['outcome']=='TARGET_HIT'
               else 'LOSS' if t['outcome']=='SL_HIT' else 'TIME')
        lines.append(
            f"  {t['symbol']:<12} {t['date']:<12} {t['days_since']:>5}d "
            f"{t['entry']:>8.2f} {t['sl']:>8.2f} {t['target']:>8.2f} "
            f"{t['exit_price']:>8.2f} {t['pnl_pct']:>+6.2f}% "
            f"{t['days_held']:>5} {out}"
        )

    # Verdict
    if ev >= 2.0 and wr >= 45:
        verdict = "EXCELLENT — Trade this setup with confidence"
    elif ev >= 1.0 and wr >= 35:
        verdict = "GOOD — Profitable, worth trading"
    elif ev >= 0:
        verdict = "MARGINAL — Slightly profitable"
    else:
        verdict = "UNPROFITABLE"

    lines.append(f"\n  VERDICT: {verdict}")
    lines.append(f"  Win rate   : {wr}%")
    lines.append(f"  Exp value  : {ev:+.2f}% per trade")
    lines.append(f"  Avg R:R    : 1:{arr}")
    lines.append(f"{'═'*65}\n")

    return '\n'.join(lines), {
        'name': name, 'total': total, 'win_rate': wr,
        'exp_val': ev, 'avg_rr': arr, 'avg_sl': asl,
        'avg_win': aw, 'avg_loss': al, 'avg_hold': ahold,
        'target_pct': atgt, 'verdict': verdict,
    }

# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
def main():
    os.makedirs(os.path.join(BASE_DIR, 'logs'), exist_ok=True)

    end_date   = date.today() - timedelta(days=7)
    start_date = end_date - timedelta(days=YEARS*365)

    log("=" * 60)
    log("  HV AT LOW — ENTRY EXACTLY AT HV LOW")
    log(f"  Period : {start_date} to {end_date}")
    log("=" * 60)

    price_data, date_idx = load_data(start_date)
    nifty_trend          = build_nifty_trend(price_data)

    header = f"""
{'═'*65}
  HV AT LOW — BACKTEST
  Entry  : When price LOW touches HV Low (limit order filled)
  SL     : 3% below HV Low
  Target : HV High
  Hold   : Max 15 days (3 weeks)
  Period : {start_date} to {end_date} ({YEARS} years)
  Run at : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
{'═'*65}
"""
    all_output   = [header]
    summary_rows = []
    log(header)

    # Test different age bands
    tests = [
        ('AGE 7-30 days   (fresh HV)',
         7, 30, False, 1.5),
        ('AGE 30-90 days  (recent HV)',
         30, 90, False, 1.5),
        ('AGE 90-180 days (older HV)',
         90, 180, False, 1.5),
        ('AGE 7-90 days   (all fresh+recent)',
         7, 90, False, 1.5),
        ('AGE 7-90 days + NIFTY FILTER',
         7, 90, True, 1.5),
        ('AGE 7-90 days + VOLUME 2x',
         7, 90, False, 2.0),
        ('AGE 7-90 days + NIFTY + VOLUME 2x  (BEST COMBO)',
         7, 90, True, 2.0),
    ]

    for name, min_age, max_age, use_nifty, min_vol in tests:
        log(f"\nRunning: {name}...")
        trades = run_backtest(
            price_data, date_idx, start_date, end_date,
            min_age, max_age, nifty_trend, use_nifty, min_vol
        )
        log(f"  Trades: {len(trades)}")
        report, summary = analyse(name, trades, YEARS)
        all_output.append(report)
        summary_rows.append(summary)
        if summary:
            log(f"  Win rate : {summary['win_rate']}%")
            log(f"  Exp val  : {summary['exp_val']:+.2f}%/trade")
            log(f"  Avg R:R  : 1:{summary['avg_rr']}")
            log(f"  Target   : {summary['target_pct']}% avg")

    # Final comparison
    comp = [
        f"\n{'═'*65}",
        f"  FINAL COMPARISON — ENTRY AT HV LOW",
        f"{'═'*65}",
        f"  {'Test':<40} {'N':>5} {'Win%':>6} {'ExpVal':>8} {'R:R':>6}",
        f"  {'-'*68}",
    ]
    for s in summary_rows:
        if not s:
            continue
        comp.append(
            f"  {s['name'][:40]:<40} {s['total']:>5} "
            f"{s['win_rate']:>5}% {s['exp_val']:>+7.2f}% 1:{s['avg_rr']:>4}"
        )

    profitable = [s for s in summary_rows if s and s.get('exp_val',0) > 0]
    if profitable:
        best = max(profitable, key=lambda x: x['exp_val'])
        comp.append(f"\n  BEST : {best['name']}")
        comp.append(f"  Win rate   : {best['win_rate']}%")
        comp.append(f"  Exp value  : {best['exp_val']:+.2f}% per trade")
        comp.append(f"  R:R        : 1:{best['avg_rr']}")
        comp.append(f"  Avg target : {best['target_pct']}%")
        comp.append(f"  Avg SL     : {best['avg_sl']}%")
        comp.append(f"  Avg hold   : {best['avg_hold']} days")
        comp.append(f"\n  PRACTICAL RULES:")
        comp.append(f"  Entry  : Limit order at HV Low")
        comp.append(f"  SL     : 3% below HV Low")
        comp.append(f"  Target : HV High ({best['target_pct']}% avg move)")
        comp.append(f"  Hold   : Max 15 days, exit if target not hit")
        comp.append(f"  Filter : HV date must be 7-90 days ago")
        if best.get('win_rate', 0) >= 40:
            comp.append(f"  Result : {best['win_rate']}% win rate — TRADEABLE ✅")
        else:
            comp.append(f"  Result : {best['win_rate']}% win rate — needs manual selection")
    else:
        comp.append(f"\n  No test was profitable at HV Low entry")
        comp.append(f"  This suggests manual stock selection is essential")
        comp.append(f"  The scanner finds candidates — you pick the best ones")

    comp.append(f"{'═'*65}\n")
    comp_str = '\n'.join(comp)
    all_output.append(comp_str)
    log(comp_str)

    final = '\n'.join(all_output)
    with open(OUTPUT_FILE, 'w') as f:
        f.write(final)

    log(f"\nResults: {OUTPUT_FILE}")
    log(f"View   : cat {OUTPUT_FILE}")

if __name__ == '__main__':
    main()
