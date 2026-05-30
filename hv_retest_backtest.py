#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  hv_retest_backtest.py
#  Tests the HIGH VOLUME RETEST setup
#
#  Logic:
#    1. Find the HV day (highest volume in 5 years)
#    2. After HV day, stock must rally AT LEAST 8% above HV Low
#       (confirms the HV day created genuine demand)
#    3. Stock then PULLS BACK to within 5% of HV Low
#       (price retests the support zone)
#    4. On the retest day: green candle + volume confirmation
#       (confirms buyers defending the level)
#    5. Entry at close of that candle
#    6. SL = low of HV candle (the actual support)
#    7. Target = previous high (where stock rallied to after HV day)
#
#  This is the FIRST RETEST setup from Pivot Boss book
#  Much higher win rate than pure EMA alignment entry
#
#  Usage:
#    nohup python3 hv_retest_backtest.py > logs/hv_retest_bt.log 2>&1 &
#    cat hv_retest_results.txt
# ═══════════════════════════════════════════════════════════════

import sqlite3
import os
from datetime import date, timedelta, datetime
from collections import defaultdict

BASE_DIR    = os.path.expanduser('~/nse-scanner')
DB_PATH     = os.path.join(BASE_DIR, 'nse_data.db')
OUTPUT_FILE = os.path.join(BASE_DIR, 'hv_retest_results.txt')

YEARS = 2

def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def compute_ema(closes, period):
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
    log("Loading price data...")
    conn     = get_db()
    c        = conn.cursor()
    c.execute('''SELECT DISTINCT symbol FROM daily_prices
                 WHERE symbol NOT IN ('NIFTY50','BANKNIFTY')
                 AND close > 0''')
    symbols  = [r[0] for r in c.fetchall()]
    log(f"  {len(symbols)} symbols found")

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
            log(f"  Loaded {i+1}/{len(symbols)}...")

    conn.close()
    log(f"Loaded {len(price_data)} symbols")
    return price_data, date_idx

# ═══════════════════════════════════════════════════════════════
#  NIFTY TREND
# ═══════════════════════════════════════════════════════════════
def build_nifty_trend(price_data):
    log("Building Nifty trend...")
    BASKET = [
        'RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK',
        'HINDUNILVR','SBIN','BHARTIARTL','ITC','KOTAKBANK',
        'LT','AXISBANK','ASIANPAINT','MARUTI','SUNPHARMA',
        'TITAN','BAJFINANCE','NTPC','POWERGRID','ONGC',
        'TATASTEEL','WIPRO','HCLTECH','ADANIENT','ULTRACEMCO',
        'NESTLEIND','BAJAJFINSV','COALINDIA','HINDALCO'
    ]
    basket = [s for s in BASKET if s in price_data and len(price_data[s]) > 100]
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

    log(f"  Trend: {sum(trend.values())} bull / {len(trend)} days")
    return trend

# ═══════════════════════════════════════════════════════════════
#  FIND HV RETEST SETUPS
# ═══════════════════════════════════════════════════════════════
def find_retest_setups(price_data, date_idx, start_date, end_date, config):
    """
    For each stock:
    1. Find HV day (highest volume in past 5 years)
    2. After HV day, check if stock rallied >= MIN_RALLY% above HV Low
    3. Check if stock pulled back to within RETEST_BAND% of HV Low
    4. On retest day: green candle, volume >= avg, price in zone
    5. Entry = close, SL = HV candle low, Target = prior high or HV High
    """
    MIN_RALLY    = config['min_rally_pct']    # stock must have gone up this much after HV
    RETEST_BAND  = config['retest_band_pct']  # how close to HV Low = retest zone
    MIN_VOL_RATIO= config['min_vol_ratio']    # volume on retest day vs avg
    MAX_HOLD     = config['max_hold_days']
    TARGET_TYPE  = config['target_type']      # 'prior_high' or 'rr_multiple'
    TARGET_RR    = config['target_rr']
    USE_NIFTY    = config['use_nifty_filter']
    LOOKBACK_HV  = 5 * 365  # find HV in last 5 years

    start_str = start_date.strftime('%Y-%m-%d')
    end_str   = end_date.strftime('%Y-%m-%d')
    all_trades= []

    for sym, bars in price_data.items():
        if len(bars) < 150:
            continue

        for i in range(100, len(bars)):
            bar      = bars[i]
            bar_date = bar['date']

            if bar_date < start_str or bar_date > end_str:
                continue

            price = bar['close']
            if price < 10:
                continue

            # ── Find HV day in past 5 years ───────────────────
            five_yr = (datetime.strptime(bar_date, '%Y-%m-%d').date()
                       - timedelta(days=LOOKBACK_HV)).strftime('%Y-%m-%d')
            hist = [b for b in bars[:i] if b['date'] >= five_yr and b['volume'] > 0]
            if len(hist) < 50:
                continue

            hv_bar  = max(hist, key=lambda b: b['volume'])
            hv_date = hv_bar['date']
            hv_high = hv_bar['high']
            hv_low  = hv_bar['low']
            hv_vol  = hv_bar['volume']

            if hv_low <= 0:
                continue

            # ── Get bars AFTER the HV day ─────────────────────
            hv_idx = date_idx[sym].get(hv_date)
            if hv_idx is None:
                continue

            # Bars between HV day and today
            post_hv_bars = bars[hv_idx+1:i]
            if len(post_hv_bars) < 5:
                continue

            # ── Check stock rallied MIN_RALLY% after HV day ───
            post_highs = [b['high'] for b in post_hv_bars]
            max_high   = max(post_highs) if post_highs else 0
            rally_pct  = (max_high - hv_low) / hv_low * 100 if hv_low > 0 else 0

            if rally_pct < MIN_RALLY:
                continue  # stock never rallied enough — not a valid HV setup

            # ── Check current price is in RETEST ZONE ─────────
            pct_above = (price - hv_low) / hv_low * 100
            if pct_above < 0:
                continue  # below HV Low = broken support
            if pct_above > RETEST_BAND:
                continue  # not in retest zone yet

            # ── Check this is a RETEST (was higher recently) ──
            # Price must have been above RETEST_BAND in last 30 bars
            recent_bars = bars[max(0, i-30):i]
            was_higher  = any(b['high'] > hv_low * (1 + RETEST_BAND/100 + 0.05)
                              for b in recent_bars)
            if not was_higher:
                continue  # price was never higher recently — not a retest

            # ── Green candle check ────────────────────────────
            if bar['close'] <= bar['open']:
                continue

            # ── Volume check ──────────────────────────────────
            avg_bars  = bars[max(0, i-20):i]
            avg_vol   = sum(b['volume'] for b in avg_bars) / len(avg_bars) if avg_bars else 0
            vol_ratio = bar['volume'] / avg_vol if avg_vol > 0 else 0
            if vol_ratio < MIN_VOL_RATIO:
                continue

            # ── EMA check (loose — just uptrend) ─────────────
            recent_closes = [b['close'] for b in bars[max(0,i-50):i]]
            ema21  = compute_ema(recent_closes, 21)
            ema50  = compute_ema(recent_closes[-50:], 50) if len(recent_closes) >= 50 else None

            if ema21 and price < ema21 * 0.97:
                continue  # too far below EMA21 — skip

            # ── Entry and levels ──────────────────────────────
            entry  = bar['close']
            sl     = hv_low  # SL = low of the HV candle (the actual support)

            if sl >= entry:
                continue
            risk   = entry - sl
            sl_pct = risk / entry * 100
            if sl_pct > 20 or sl_pct < 0.5:
                continue  # unrealistic SL

            # ── Target ────────────────────────────────────────
            if TARGET_TYPE == 'prior_high':
                target = max_high  # target the prior high after HV day
            else:
                target = entry + risk * TARGET_RR

            reward = target - entry
            if reward <= 0:
                continue
            rr = round(reward / risk, 1)
            if rr < 1.0:
                continue

            # ── Simulate trade ────────────────────────────────
            future_bars = bars[i+1: i+1+MAX_HOLD]
            outcome     = 'TIME_EXIT'
            exit_price  = entry
            exit_date   = bar_date
            days_held   = 0

            for fb in future_bars:
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

            all_trades.append({
                'symbol':     sym,
                'date':       bar_date,
                'hv_date':    hv_date,
                'entry':      round(entry, 2),
                'sl':         round(sl, 2),
                'target':     round(target, 2),
                'hv_low':     round(hv_low, 2),
                'hv_high':    round(hv_high, 2),
                'exit_price': round(exit_price, 2),
                'exit_date':  exit_date,
                'outcome':    outcome,
                'pnl_pct':    pnl_pct,
                'sl_pct':     round(sl_pct, 2),
                'rr':         rr,
                'days_held':  days_held,
                'rally_pct':  round(rally_pct, 2),
                'vol_ratio':  round(vol_ratio, 2),
            })

    return all_trades

# ═══════════════════════════════════════════════════════════════
#  APPLY NIFTY FILTER
# ═══════════════════════════════════════════════════════════════
def apply_nifty(trades, trend):
    return [t for t in trades if trend.get(t['date'], True)]

# ═══════════════════════════════════════════════════════════════
#  ANALYSE
# ═══════════════════════════════════════════════════════════════
def analyse(name, desc, trades, years):
    if not trades:
        return f"\n{name}: No trades\n", {}

    total  = len(trades)
    wins   = [t for t in trades if t['outcome'] == 'TARGET_HIT']
    losses = [t for t in trades if t['outcome'] == 'SL_HIT']
    timeex = [t for t in trades if t['outcome'] == 'TIME_EXIT']

    wr     = round(len(wins)/total*100, 1)
    lr     = round(len(losses)/total*100, 1)
    tr     = round(len(timeex)/total*100, 1)
    aw     = round(sum(t['pnl_pct'] for t in wins)/len(wins), 2)     if wins   else 0
    al     = round(sum(t['pnl_pct'] for t in losses)/len(losses), 2) if losses else 0
    at     = round(sum(t['pnl_pct'] for t in timeex)/len(timeex), 2) if timeex else 0
    tpnl   = round(sum(t['pnl_pct'] for t in trades), 2)
    ev     = round(tpnl/total, 2)
    asl    = round(sum(t['sl_pct'] for t in trades)/total, 2)
    arr    = round(sum(t['rr'] for t in trades)/total, 2)
    ahold  = round(sum(t['days_held'] for t in trades)/total, 1)

    monthly = defaultdict(list)
    yearly  = defaultdict(list)
    for t in trades:
        monthly[t['date'][:7]].append(t)
        yearly[t['date'][:4]].append(t)

    cutoff = (date.today() - timedelta(days=365)).strftime('%Y-%m-%d')
    oos    = [t for t in trades if t['date'] <  cutoff]
    ins    = [t for t in trades if t['date'] >= cutoff]

    by_sym = defaultdict(list)
    for t in trades:
        by_sym[t['symbol']].append(t)
    sym_stats = sorted(
        [(s, len(st),
          round(sum(1 for t in st if t['outcome']=='TARGET_HIT')/len(st)*100,1),
          round(sum(t['pnl_pct'] for t in st),2))
         for s, st in by_sym.items()],
        key=lambda x: x[3], reverse=True
    )

    lines = []
    lines.append(f"\n{'═'*68}")
    lines.append(f"  {name}")
    lines.append(f"  {desc}")
    lines.append(f"{'═'*68}")

    lines.append(f"\n  OVERVIEW")
    lines.append(f"  {'Total trades':<35}: {total}")
    lines.append(f"  {'Per month':<35}: {round(total/(years*12),1)}")
    lines.append(f"  {'Avg hold days':<35}: {ahold}")

    lines.append(f"\n  OUTCOMES")
    lines.append(f"  {'TARGET hit (WIN)':<35}: {len(wins)} ({wr}%)")
    lines.append(f"  {'SL hit (LOSS)':<35}: {len(losses)} ({lr}%)")
    lines.append(f"  {'Time exit':<35}: {len(timeex)} ({tr}%)")

    lines.append(f"\n  P&L")
    lines.append(f"  {'Avg win %':<35}: +{aw}%")
    lines.append(f"  {'Avg loss %':<35}: {al}%")
    lines.append(f"  {'Avg time exit %':<35}: {at}%")
    lines.append(f"  {'Avg SL distance':<35}: {asl}%")
    lines.append(f"  {'Avg R:R':<35}: 1:{arr}")
    lines.append(f"  {'Expected value/trade':<35}: {ev:+.2f}%")
    lines.append(f"  {'Total PnL':<35}: {tpnl:+.2f}%")

    lines.append(f"\n  YEAR BY YEAR")
    lines.append(f"  {'Year':<8} {'Trades':>7} {'Wins':>6} {'Win%':>6} "
                 f"{'Avg%':>8} {'Total%':>10}")
    lines.append(f"  {'-'*52}")
    for yr in sorted(yearly.keys()):
        yt   = yearly[yr]
        yw   = sum(1 for t in yt if t['outcome']=='TARGET_HIT')
        ywr  = round(yw/len(yt)*100,1)
        yav  = round(sum(t['pnl_pct'] for t in yt)/len(yt),2)
        ytot = round(sum(t['pnl_pct'] for t in yt),2)
        lines.append(f"  {yr:<8} {len(yt):>7} {yw:>6} {ywr:>5}% "
                     f"{yav:>+7.2f}% {ytot:>+9.2f}%")

    lines.append(f"\n  MONTHLY BREAKDOWN")
    lines.append(f"  {'Month':<10} {'Trades':>7} {'Wins':>6} {'Win%':>6} {'Avg%':>8}")
    lines.append(f"  {'-'*45}")
    for month in sorted(monthly.keys()):
        mt  = monthly[month]
        mw  = sum(1 for t in mt if t['outcome']=='TARGET_HIT')
        mwr = round(mw/len(mt)*100,1)
        mav = round(sum(t['pnl_pct'] for t in mt)/len(mt),2)
        lines.append(f"  {month:<10} {len(mt):>7} {mw:>6} {mwr:>5}% {mav:>+7.2f}%")

    lines.append(f"\n  OUT-OF-SAMPLE VALIDATION")
    lines.append(f"  {'-'*55}")
    for label, subset in [('Year 1 (out-of-sample)', oos),
                           ('Year 2 (in-sample)',     ins)]:
        if not subset:
            continue
        sw   = sum(1 for t in subset if t['outcome']=='TARGET_HIT')
        swr  = round(sw/len(subset)*100,1)
        sav  = round(sum(t['pnl_pct'] for t in subset)/len(subset),2)
        stot = round(sum(t['pnl_pct'] for t in subset),2)
        lines.append(f"  {label}: {len(subset)} trades | "
                     f"Win:{swr}% | Avg:{sav:+.2f}% | Total:{stot:+.2f}%")
    if oos and ins:
        d = abs(sum(1 for t in oos if t['outcome']=='TARGET_HIT')/len(oos)*100 -
                sum(1 for t in ins if t['outcome']=='TARGET_HIT')/len(ins)*100)
        tag = "ROBUST" if d<=10 else "some variance" if d<=20 else "HIGH VARIANCE"
        lines.append(f"  Win rate diff: {d:.1f}% — {tag}")

    lines.append(f"\n  TOP 10 STOCKS BY TOTAL PnL")
    lines.append(f"  {'Symbol':<15} {'Trades':>7} {'Win%':>6} {'Total%':>9}")
    lines.append(f"  {'-'*42}")
    for sym, cnt, swr, spnl in sym_stats[:10]:
        lines.append(f"  {sym:<15} {cnt:>7} {swr:>5}% {spnl:>+8.2f}%")

    lines.append(f"\n  ALL TRADES (first 50)")
    lines.append(f"  {'Symbol':<12} {'Date':<12} {'Entry':>8} {'SL':>8} "
                 f"{'Target':>8} {'Exit':>8} {'PnL%':>7} {'Days':>5} Outcome")
    lines.append(f"  {'-'*82}")
    for t in sorted(trades, key=lambda x: x['date'])[:50]:
        out = ('WIN ' if t['outcome']=='TARGET_HIT'
               else 'LOSS' if t['outcome']=='SL_HIT' else 'TIME')
        lines.append(
            f"  {t['symbol']:<12} {t['date']:<12} {t['entry']:>8.2f} "
            f"{t['sl']:>8.2f} {t['target']:>8.2f} {t['exit_price']:>8.2f} "
            f"{t['pnl_pct']:>+6.2f}% {t['days_held']:>5} {out}"
        )

    if ev >= 2 and wr >= 50:
        verdict = "EXCELLENT — High win rate with good R:R"
    elif ev >= 1 and wr >= 45:
        verdict = "GOOD — Profitable, worth trading"
    elif ev >= 0:
        verdict = "MARGINAL — Slightly profitable"
    else:
        verdict = "UNPROFITABLE"

    lines.append(f"\n  VERDICT: {verdict}")
    lines.append(f"{'═'*68}\n")

    return '\n'.join(lines), {
        'name': name, 'total': total, 'win_rate': wr,
        'exp_val': ev, 'avg_rr': arr, 'avg_sl': asl,
        'avg_win': aw, 'avg_loss': al, 'verdict': verdict,
    }

# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
def main():
    os.makedirs(os.path.join(BASE_DIR, 'logs'), exist_ok=True)

    end_date   = date.today() - timedelta(days=7)
    start_date = end_date - timedelta(days=YEARS*365)

    log("=" * 60)
    log("  HV RETEST SETUP BACKTEST")
    log(f"  Period : {start_date} to {end_date} ({YEARS} years)")
    log("=" * 60)

    price_data, date_idx = load_data(start_date)
    nifty_trend          = build_nifty_trend(price_data)

    configs = [
        {
            'name': 'RETEST 0 — BASELINE',
            'desc': 'Rally 8%+ then retest within 5% | SL=HV Low | Target=Prior High | Vol 1.5x',
            'min_rally_pct':   8.0,
            'retest_band_pct': 5.0,
            'min_vol_ratio':   1.5,
            'max_hold_days':   10,
            'target_type':     'prior_high',
            'target_rr':       2.0,
            'use_nifty_filter': False,
        },
        {
            'name': 'RETEST 1 — STRONGER RALLY',
            'desc': 'Rally 15%+ then retest within 5% | SL=HV Low | Target=Prior High | Vol 2x',
            'min_rally_pct':   15.0,
            'retest_band_pct': 5.0,
            'min_vol_ratio':   2.0,
            'max_hold_days':   10,
            'target_type':     'prior_high',
            'target_rr':       2.0,
            'use_nifty_filter': False,
        },
        {
            'name': 'RETEST 2 — FIXED 2:1 TARGET',
            'desc': 'Rally 8%+ | Retest 5% | SL=HV Low | Target=2:1 R:R | Vol 1.5x',
            'min_rally_pct':   8.0,
            'retest_band_pct': 5.0,
            'min_vol_ratio':   1.5,
            'max_hold_days':   10,
            'target_type':     'rr_multiple',
            'target_rr':       2.0,
            'use_nifty_filter': False,
        },
        {
            'name': 'RETEST 3 — NIFTY FILTER',
            'desc': 'Rally 8%+ | Retest 5% | SL=HV Low | Target=Prior High | Nifty Bull',
            'min_rally_pct':   8.0,
            'retest_band_pct': 5.0,
            'min_vol_ratio':   1.5,
            'max_hold_days':   10,
            'target_type':     'prior_high',
            'target_rr':       2.0,
            'use_nifty_filter': True,
        },
        {
            'name': 'RETEST 4 — ALL COMBINED',
            'desc': 'Rally 15%+ | Retest 3% | SL=HV Low | Target=Prior High | Nifty Bull | Vol 2x',
            'min_rally_pct':   15.0,
            'retest_band_pct': 3.0,
            'min_vol_ratio':   2.0,
            'max_hold_days':   10,
            'target_type':     'prior_high',
            'target_rr':       2.0,
            'use_nifty_filter': True,
        },
    ]

    header = f"""
{'═'*68}
  HV RETEST SETUP — BACKTEST
  Period  : {start_date} to {end_date} ({YEARS} years)
  Entry   : First retest of HV Low after rally
  SL      : HV candle low (actual support level)
  Target  : Prior high or 2:1 R:R
  Run at  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
{'═'*68}
"""
    all_output   = [header]
    summary_rows = []
    log(header)

    for cfg in configs:
        log(f"\nRunning {cfg['name']}...")
        trades = find_retest_setups(
            price_data, date_idx, start_date, end_date, cfg)

        if cfg['use_nifty_filter']:
            trades = apply_nifty(trades, nifty_trend)

        log(f"  Found {len(trades)} trades")
        report, summary = analyse(cfg['name'], cfg['desc'], trades, YEARS)
        all_output.append(report)
        summary_rows.append(summary)
        log(f"  Win rate : {summary.get('win_rate',0)}%")
        log(f"  Exp val  : {summary.get('exp_val',0):+.2f}%/trade")
        log(f"  Avg R:R  : 1:{summary.get('avg_rr',0)}")
        log(f"  Avg SL   : {summary.get('avg_sl',0)}%")

    # Comparison
    comp = [
        f"\n{'═'*68}",
        f"  COMPARISON — ALL RETEST CONFIGURATIONS",
        f"{'═'*68}",
        f"  {'Config':<12} {'Trades':>8} {'Win%':>7} {'AvgWin':>9} "
        f"{'AvgLoss':>9} {'R:R':>6} {'SL%':>6} {'ExpVal':>9}",
        f"  {'-'*72}",
    ]
    for s in summary_rows:
        if not s:
            continue
        cname = s['name'].split('—')[0].strip()
        comp.append(
            f"  {cname:<12} {s['total']:>8} {s['win_rate']:>6}% "
            f"{s['avg_win']:>+8.2f}% {s['avg_loss']:>+8.2f}% "
            f"{s['avg_rr']:>6} {s['avg_sl']:>5}% {s['exp_val']:>+8.2f}%"
        )

    profitable = [s for s in summary_rows if s and s.get('exp_val',0) > 0]
    if profitable:
        best = max(profitable, key=lambda x: x['exp_val'])
        comp.append(f"\n  BEST CONFIG : {best['name']}")
        comp.append(f"  Win rate    : {best['win_rate']}%")
        comp.append(f"  Exp value   : {best['exp_val']:+.2f}% per trade")
        comp.append(f"  Avg R:R     : 1:{best['avg_rr']}")
        comp.append(f"  Avg SL      : {best['avg_sl']}%")

        # Compare with original HV (Config 3 was best at 20% win, +0.21%)
        comp.append(f"\n  VS ORIGINAL HV SETUP (Config 3):")
        comp.append(f"  Original : 20.0% win | +0.21% exp val | 1:2.0 R:R")
        comp.append(f"  Retest   : {best['win_rate']}% win | {best['exp_val']:+.2f}% exp val | 1:{best['avg_rr']} R:R")
        improvement = round(best['exp_val'] - 0.21, 2)
        comp.append(f"  Improvement: {improvement:+.2f}% per trade")
    else:
        comp.append(f"\n  No retest config was profitable")
        comp.append(f"  Recommendation: Keep original HV entry but filter more aggressively")

    comp.append(f"{'═'*68}\n")
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
