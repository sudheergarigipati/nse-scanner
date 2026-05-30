#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  hv_backtest.py
#  Backtests the High Volume setup with multiple SL/Target combos
#  Uses 5 years of daily data from your DB
#
#  Setup:
#    Entry  : When price is near HV Low (within buffer %)
#             EMA aligned (simulated via daily close > all EMAs)
#             Green candle (close > open)
#    Exit   : Target hit / SL hit / Time exit (max days)
#
#  Tests 4 configurations:
#    Config 0 — Baseline (current logic)
#    Config 1 — Tighter entry (5% buffer, SL = HV candle low)
#    Config 2 — Weekly trend filter added
#    Config 3 — All improvements combined
#
#  Usage:
#    nohup python3 hv_backtest.py > logs/hv_bt.log 2>&1 &
#    cat hv_backtest_results.txt
# ═══════════════════════════════════════════════════════════════

import sqlite3
import os
from datetime import date, timedelta, datetime
from collections import defaultdict

BASE_DIR    = os.path.expanduser('~/nse-scanner')
DB_PATH     = os.path.join(BASE_DIR, 'nse_data.db')
OUTPUT_FILE = os.path.join(BASE_DIR, 'hv_backtest_results.txt')

YEARS       = 2    # start with 2 years for speed, change to 5 for full backtest
MIN_SCORE   = 50   # minimum HV score to qualify

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
#  LOAD ALL DATA
# ═══════════════════════════════════════════════════════════════
def load_data(start_date):
    log("Loading price data from DB...")
    conn = get_db()
    c    = conn.cursor()

    # Get list of symbols first
    c.execute('''
        SELECT DISTINCT symbol FROM daily_prices
        WHERE symbol NOT IN ('NIFTY50', 'BANKNIFTY')
        AND   close > 0
    ''')
    symbols = [r[0] for r in c.fetchall()]
    log(f"  {len(symbols)} symbols found")

    # Load data symbol by symbol — more memory efficient
    price_data = {}
    date_idx   = {}
    start_str  = (start_date - timedelta(days=300)).strftime('%Y-%m-%d')

    for i, sym in enumerate(symbols):
        c.execute('''
            SELECT date, open, high, low, close, volume
            FROM   daily_prices
            WHERE  symbol = ? AND date >= ? AND close > 0
            ORDER  BY date
        ''', (sym, start_str))
        rows = c.fetchall()
        if len(rows) < 100:
            continue
        bars = [{'date':r[0],'open':r[1],'high':r[2],
                 'low':r[3],'close':r[4],'volume':r[5]} for r in rows]
        price_data[sym] = bars
        date_idx[sym]   = {b['date']: j for j, b in enumerate(bars)}

        if (i+1) % 100 == 0:
            log(f"  Loaded {i+1}/{len(symbols)} symbols...")

    conn.close()
    log(f"Loaded {len(price_data)} symbols with sufficient data")
    return price_data, date_idx

# ═══════════════════════════════════════════════════════════════
#  NIFTY BASKET TREND (same as Camarilla)
# ═══════════════════════════════════════════════════════════════
def build_nifty_trend(price_data):
    log("Building Nifty basket trend...")
    BASKET = [
        'RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK',
        'HINDUNILVR','SBIN','BHARTIARTL','ITC','KOTAKBANK',
        'LT','AXISBANK','ASIANPAINT','MARUTI','SUNPHARMA',
        'TITAN','BAJFINANCE','NTPC','POWERGRID','ONGC',
        'TATASTEEL','WIPRO','HCLTECH','ADANIENT','ULTRACEMCO',
        'NESTLEIND','BAJAJFINSV','COALINDIA','HINDALCO'
    ]
    basket = [s for s in BASKET if s in price_data and len(price_data[s]) > 100]

    base     = {s: price_data[s][0]['close'] for s in basket}
    sd       = {s: {b['date']: b['close'] for b in price_data[s]} for s in basket}
    all_dates= sorted(set(b['date'] for s in basket for b in price_data[s]))

    basket_series = []
    for dt in all_dates:
        vals = [sd[s][dt]/base[s]*100 for s in basket if dt in sd[s]]
        if vals:
            basket_series.append((dt, sum(vals)/len(vals)))

    k   = 2/(100+1)
    ema = basket_series[0][1]
    trend = {}
    for dt, val in basket_series:
        ema = val*k + ema*(1-k)
        trend[dt] = val >= ema

    log(f"  Nifty trend: {sum(trend.values())} bull days / {len(trend)} total")
    return trend

# ═══════════════════════════════════════════════════════════════
#  COMPUTE HV SETUPS
#  For each stock on each day, check if a valid HV setup exists
# ═══════════════════════════════════════════════════════════════
def find_hv_setups(price_data, date_idx, start_date, end_date, config):
    """
    For each stock, find all historical HV days before the signal date.
    Then check if price came into the HV zone with EMA alignment.
    """
    all_trades = []
    LOOKBACK   = 5 * 365  # use 5yr history to find HV

    BULL_BUFFER    = config['bull_buffer']      # how close to HV Low to enter
    SL_TYPE        = config['sl_type']          # 'hv_low_pct' or 'hv_candle_low'
    SL_PCT         = config['sl_pct']           # used if sl_type = hv_low_pct
    TARGET_TYPE    = config['target_type']      # 'hv_high' or 'rr_multiple'
    TARGET_RR      = config['target_rr']        # R:R multiple if rr_multiple
    MAX_HOLD       = config['max_hold_days']    # max holding days
    USE_NIFTY      = config['use_nifty_filter']
    MIN_EMA_DAYS   = config.get('min_ema_days', 21)  # min bars for EMA

    start_str = start_date.strftime('%Y-%m-%d')
    end_str   = end_date.strftime('%Y-%m-%d')

    processed = 0
    for sym, bars in price_data.items():
        if len(bars) < 100:
            continue
        processed += 1

        for i in range(200, len(bars)):
            bar      = bars[i]
            bar_date = bar['date']

            if bar_date < start_str or bar_date > end_str:
                continue

            price = bar['close']
            if price < 10:  # skip penny stocks
                continue

            # ── Find HV day from ALL history before today ──────
            # Use last 5 years only
            five_yr_str = (datetime.strptime(bar_date, '%Y-%m-%d').date()
                           - timedelta(days=LOOKBACK)).strftime('%Y-%m-%d')
            hist_bars = [b for b in bars[:i] if b['date'] >= five_yr_str]
            if len(hist_bars) < 50:
                continue

            # Find highest volume day
            hv_bar = max(hist_bars, key=lambda b: b['volume'])
            hv_vol    = hv_bar['volume']
            hv_high   = hv_bar['high']
            hv_low    = hv_bar['low']
            hv_close  = hv_bar['close']
            hv_open   = hv_bar['open']
            hv_date   = hv_bar['date']

            if hv_low <= 0 or hv_vol <= 0:
                continue

            # ── Score the HV setup ─────────────────────────────
            hv_range = hv_high - hv_low
            if hv_range <= 0:
                continue

            # Candle position (bullish if close in upper half)
            cp = (hv_close - hv_low) / hv_range
            sc = 0
            if cp >= 0.5:           sc += 15
            if price >= hv_low:     sc += 20
            pct_above = (price - hv_low) / hv_low * 100
            if pct_above <= 3:      sc += 20
            elif pct_above <= 7:    sc += 12
            elif pct_above <= 12:   sc += 6

            days_since = (datetime.strptime(bar_date, '%Y-%m-%d').date() -
                          datetime.strptime(hv_date,  '%Y-%m-%d').date()).days
            if days_since <= 7:     sc += 20
            elif days_since <= 30:  sc += 14
            elif days_since <= 90:  sc += 8
            else:                   sc += 3

            if hv_vol > 100_000_000: sc += 10
            elif hv_vol > 50_000_000: sc += 7
            elif hv_vol > 10_000_000: sc += 4

            rng_pct = hv_range / hv_low * 100
            if rng_pct > 10:  sc += 10
            elif rng_pct > 5: sc += 5

            sc = min(sc, 100)
            if sc < MIN_SCORE:
                continue

            # ── Proximity check ────────────────────────────────
            if price < hv_low * 0.99:  # below HV Low = broken
                continue
            if pct_above > BULL_BUFFER:  # too far above
                continue

            # ── EMA alignment on daily (proxy for 15-min) ─────
            recent_closes = [b['close'] for b in bars[max(0,i-200):i]]
            ema21  = compute_ema(recent_closes, 21)
            ema50  = compute_ema(recent_closes, 50)
            ema100 = compute_ema(recent_closes, 100)
            ema200 = compute_ema(recent_closes, 200)

            if not all([ema21, ema50, ema100, ema200]):
                continue

            # All 4 EMAs aligned + price above all
            ema_ok = (price > ema21 > ema50 > ema100 > ema200)
            if not ema_ok:
                continue

            # ── Green candle check ─────────────────────────────
            if bar['close'] <= bar['open']:
                continue

            # ── Entry price ────────────────────────────────────
            entry = bar['close']

            # ── Stop loss ──────────────────────────────────────
            if SL_TYPE == 'hv_candle_low':
                sl = hv_low  # low of the HV candle itself
            else:
                sl = entry * (1 - SL_PCT / 100)

            if sl >= entry:
                continue
            risk = entry - sl
            if risk <= 0:
                continue
            sl_pct = risk / entry * 100

            # Skip if SL is too wide (> 15%)
            if sl_pct > 15:
                continue

            # ── Target ────────────────────────────────────────
            if TARGET_TYPE == 'hv_high':
                target = hv_high
            else:
                target = entry + risk * TARGET_RR

            reward = target - entry
            if reward <= 0:
                continue
            rr = round(reward / risk, 1)

            # Skip if R:R < 1
            if rr < 1.0:
                continue

            # ── Simulate trade ─────────────────────────────────
            entry_idx   = date_idx[sym].get(bar_date)
            if entry_idx is None:
                continue

            future_bars = bars[entry_idx + 1: entry_idx + 1 + MAX_HOLD]
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
                'score':      sc,
                'pct_above':  round(pct_above, 2),
            })

    return all_trades

# ═══════════════════════════════════════════════════════════════
#  APPLY NIFTY FILTER POST-HOC
# ═══════════════════════════════════════════════════════════════
def apply_nifty_filter(trades, nifty_trend):
    return [t for t in trades if nifty_trend.get(t['date'], True)]

# ═══════════════════════════════════════════════════════════════
#  ANALYSE
# ═══════════════════════════════════════════════════════════════
def analyse(config_name, config_desc, all_trades, years):
    if not all_trades:
        return f"\n{config_name}: No trades found\n", {}

    total   = len(all_trades)
    wins    = [t for t in all_trades if t['outcome'] == 'TARGET_HIT']
    losses  = [t for t in all_trades if t['outcome'] == 'SL_HIT']
    timeex  = [t for t in all_trades if t['outcome'] == 'TIME_EXIT']

    win_rate  = round(len(wins)   / total * 100, 1)
    loss_rate = round(len(losses) / total * 100, 1)
    time_rate = round(len(timeex) / total * 100, 1)

    avg_win   = round(sum(t['pnl_pct'] for t in wins)   / len(wins),   2) if wins   else 0
    avg_loss  = round(sum(t['pnl_pct'] for t in losses) / len(losses), 2) if losses else 0
    avg_time  = round(sum(t['pnl_pct'] for t in timeex) / len(timeex), 2) if timeex else 0
    total_pnl = round(sum(t['pnl_pct'] for t in all_trades), 2)
    exp_val   = round(total_pnl / total, 2)
    avg_sl    = round(sum(t['sl_pct'] for t in all_trades) / total, 2)
    avg_rr    = round(sum(t['rr']     for t in all_trades) / total, 2)
    avg_hold  = round(sum(t['days_held'] for t in all_trades) / total, 1)

    # Monthly breakdown
    monthly = defaultdict(list)
    for t in all_trades:
        monthly[t['date'][:7]].append(t)

    # Yearly
    yearly = defaultdict(list)
    for t in all_trades:
        yearly[t['date'][:4]].append(t)

    # Out of sample
    cutoff = (date.today() - timedelta(days=365*2)).strftime('%Y-%m-%d')
    oos    = [t for t in all_trades if t['date'] <  cutoff]
    ins    = [t for t in all_trades if t['date'] >= cutoff]

    # Best/worst stocks
    by_sym = defaultdict(list)
    for t in all_trades:
        by_sym[t['symbol']].append(t)
    sym_stats = []
    for sym, st in by_sym.items():
        sw   = sum(1 for t in st if t['outcome'] == 'TARGET_HIT')
        swr  = round(sw/len(st)*100, 1)
        spnl = round(sum(t['pnl_pct'] for t in st), 2)
        sym_stats.append((sym, len(st), swr, spnl))
    sym_stats.sort(key=lambda x: x[3], reverse=True)

    lines = []
    lines.append(f"\n{'═'*68}")
    lines.append(f"  {config_name}")
    lines.append(f"  {config_desc}")
    lines.append(f"{'═'*68}")

    lines.append(f"\n  OVERVIEW")
    lines.append(f"  {'Total trades':<35}: {total}")
    lines.append(f"  {'Trades per month':<35}: {round(total/(years*12), 1)}")
    lines.append(f"  {'Trades per week':<35}: {round(total/(years*52), 1)}")
    lines.append(f"  {'Avg hold (days)':<35}: {avg_hold}")

    lines.append(f"\n  OUTCOMES")
    lines.append(f"  {'Total trades':<35}: {total}")
    lines.append(f"  {'TARGET hit (WIN)':<35}: {len(wins)} ({win_rate}%)")
    lines.append(f"  {'SL hit (LOSS)':<35}: {len(losses)} ({loss_rate}%)")
    lines.append(f"  {'Time exit':<35}: {len(timeex)} ({time_rate}%)")

    lines.append(f"\n  P&L")
    lines.append(f"  {'Avg win %':<35}: +{avg_win}%")
    lines.append(f"  {'Avg loss %':<35}: {avg_loss}%")
    lines.append(f"  {'Avg time exit %':<35}: {avg_time}%")
    lines.append(f"  {'Avg SL distance':<35}: {avg_sl}%")
    lines.append(f"  {'Avg R:R':<35}: 1:{avg_rr}")
    lines.append(f"  {'Expected value per trade':<35}: {exp_val:+.2f}%")
    lines.append(f"  {'Total PnL (sum)':<35}: {total_pnl:+.2f}%")

    lines.append(f"\n  YEAR BY YEAR")
    lines.append(f"  {'Year':<8} {'Trades':>7} {'Wins':>6} {'Win%':>6} "
                 f"{'Avg%':>8} {'Total%':>10}")
    lines.append(f"  {'-'*50}")
    for yr in sorted(yearly.keys()):
        yt   = yearly[yr]
        yw   = sum(1 for t in yt if t['outcome'] == 'TARGET_HIT')
        ywr  = round(yw/len(yt)*100, 1)
        yav  = round(sum(t['pnl_pct'] for t in yt)/len(yt), 2)
        ytot = round(sum(t['pnl_pct'] for t in yt), 2)
        lines.append(f"  {yr:<8} {len(yt):>7} {yw:>6} {ywr:>5}% {yav:>+7.2f}% {ytot:>+9.2f}%")

    lines.append(f"\n  MONTHLY BREAKDOWN")
    lines.append(f"  {'Month':<10} {'Trades':>7} {'Wins':>6} {'Win%':>6} {'Avg%':>8}")
    lines.append(f"  {'-'*45}")
    for month in sorted(monthly.keys()):
        mt  = monthly[month]
        mw  = sum(1 for t in mt if t['outcome'] == 'TARGET_HIT')
        mwr = round(mw/len(mt)*100, 1)
        mav = round(sum(t['pnl_pct'] for t in mt)/len(mt), 2)
        lines.append(f"  {month:<10} {len(mt):>7} {mw:>6} {mwr:>5}% {mav:>+7.2f}%")

    lines.append(f"\n  OUT-OF-SAMPLE VALIDATION")
    lines.append(f"  {'-'*55}")
    for label, subset in [('Out-of-sample (yr 1-3)', oos),
                           ('In-sample     (yr 4-5)', ins)]:
        if not subset:
            continue
        sw   = sum(1 for t in subset if t['outcome'] == 'TARGET_HIT')
        swr  = round(sw/len(subset)*100, 1)
        sav  = round(sum(t['pnl_pct'] for t in subset)/len(subset), 2)
        stot = round(sum(t['pnl_pct'] for t in subset), 2)
        lines.append(f"  {label}: {len(subset)} trades | "
                     f"Win:{swr}% | Avg:{sav:+.2f}% | Total:{stot:+.2f}%")
    if oos and ins:
        oos_wr = sum(1 for t in oos if t['outcome']=='TARGET_HIT')/len(oos)*100
        ins_wr = sum(1 for t in ins if t['outcome']=='TARGET_HIT')/len(ins)*100
        diff   = abs(oos_wr - ins_wr)
        tag    = "ROBUST" if diff <= 10 else "some variance" if diff <= 20 else "HIGH VARIANCE"
        lines.append(f"  Win rate diff: {diff:.1f}% — {tag}")

    lines.append(f"\n  TOP 10 STOCKS BY TOTAL PnL")
    lines.append(f"  {'Symbol':<15} {'Trades':>7} {'Win%':>6} {'Total%':>9}")
    lines.append(f"  {'-'*42}")
    for sym, cnt, wr, pnl in sym_stats[:10]:
        lines.append(f"  {sym:<15} {cnt:>7} {wr:>5}% {pnl:>+8.2f}%")

    # Verdict
    lines.append(f"\n{'═'*68}")
    if exp_val >= 3 and win_rate >= 60:
        verdict = "EXCELLENT — Strong setup, trade with full confidence"
    elif exp_val >= 1.5 and win_rate >= 55:
        verdict = "GOOD — Profitable setup worth trading"
    elif exp_val >= 0:
        verdict = "MARGINAL — Slightly profitable, trade with caution"
    else:
        verdict = "UNPROFITABLE — Do not trade this configuration"

    lines.append(f"  VERDICT    : {verdict}")
    lines.append(f"  Win rate   : {win_rate}%")
    lines.append(f"  Exp value  : {exp_val:+.2f}% per trade")
    lines.append(f"  Avg R:R    : 1:{avg_rr}")
    lines.append(f"  Avg SL     : {avg_sl}%")
    lines.append(f"{'═'*68}\n")

    return '\n'.join(lines), {
        'name':      config_name,
        'total':     total,
        'win_rate':  win_rate,
        'exp_val':   exp_val,
        'avg_rr':    avg_rr,
        'avg_sl':    avg_sl,
        'avg_win':   avg_win,
        'avg_loss':  avg_loss,
        'total_pnl': total_pnl,
        'verdict':   verdict,
    }

# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
def main():
    os.makedirs(os.path.join(BASE_DIR, 'logs'), exist_ok=True)

    end_date   = date.today() - timedelta(days=7)
    start_date = end_date - timedelta(days=YEARS * 365)

    log("=" * 60)
    log("  HV SETUP BACKTEST — 5 YEARS")
    log(f"  Period : {start_date} to {end_date}")
    log("=" * 60)

    price_data, date_idx = load_data(start_date)
    nifty_trend          = build_nifty_trend(price_data)

    # ── Test configurations ────────────────────────────────────
    configs = [
        {
            'name': 'CONFIG 0 — BASELINE (current)',
            'desc': 'Buffer 15% | SL = 2% below entry | Target = HV High | Hold 10 days',
            'bull_buffer':    15.0,
            'sl_type':        'hv_low_pct',
            'sl_pct':         2.0,
            'target_type':    'hv_high',
            'target_rr':      2.0,
            'max_hold_days':  10,
            'use_nifty_filter': False,
        },
        {
            'name': 'CONFIG 1 — TIGHTER ENTRY',
            'desc': 'Buffer 5% | SL = HV candle low | Target = HV High | Hold 10 days',
            'bull_buffer':    5.0,
            'sl_type':        'hv_candle_low',
            'sl_pct':         5.0,
            'target_type':    'hv_high',
            'target_rr':      2.0,
            'max_hold_days':  10,
            'use_nifty_filter': False,
        },
        {
            'name': 'CONFIG 2 — NIFTY TREND FILTER',
            'desc': 'Buffer 10% | SL = HV candle low | Target = HV High | Nifty bull only',
            'bull_buffer':    10.0,
            'sl_type':        'hv_candle_low',
            'sl_pct':         5.0,
            'target_type':    'hv_high',
            'target_rr':      2.0,
            'max_hold_days':  10,
            'use_nifty_filter': True,
        },
        {
            'name': 'CONFIG 3 — FIXED R:R 2:1',
            'desc': 'Buffer 5% | SL = 5% from entry | Target = 2:1 R:R | Hold 10 days',
            'bull_buffer':    5.0,
            'sl_type':        'hv_low_pct',
            'sl_pct':         5.0,
            'target_type':    'rr_multiple',
            'target_rr':      2.0,
            'max_hold_days':  10,
            'use_nifty_filter': False,
        },
        {
            'name': 'CONFIG 4 — ALL COMBINED (recommended)',
            'desc': 'Buffer 5% | SL = HV candle low | Target = HV High | Nifty bull | Hold 10 days',
            'bull_buffer':    5.0,
            'sl_type':        'hv_candle_low',
            'sl_pct':         5.0,
            'target_type':    'hv_high',
            'target_rr':      2.0,
            'max_hold_days':  10,
            'use_nifty_filter': True,
        },
    ]

    all_output   = []
    summary_rows = []

    header = f"""
{'═'*68}
  HIGH VOLUME SETUP — COMPREHENSIVE BACKTEST
  Period  : {start_date} to {end_date} ({YEARS} years)
  Symbols : All NSE stocks in DB
  Entry   : Price near HV Low + 4 EMAs aligned + green candle
  Exit    : Target / SL / Time (10 days max)
  Run at  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
{'═'*68}
"""
    all_output.append(header)
    log(header)

    for cfg in configs:
        log(f"\nRunning {cfg['name']}...")
        trades = find_hv_setups(price_data, date_idx, start_date, end_date, cfg)

        if cfg['use_nifty_filter']:
            trades = apply_nifty_filter(trades, nifty_trend)

        log(f"  Found {len(trades)} trades")
        report, summary = analyse(cfg['name'], cfg['desc'], trades, YEARS)
        all_output.append(report)
        summary_rows.append(summary)

        log(f"  Win rate : {summary.get('win_rate', 0)}%")
        log(f"  Exp val  : {summary.get('exp_val', 0):+.2f}%/trade")
        log(f"  Avg R:R  : 1:{summary.get('avg_rr', 0)}")
        log(f"  Avg SL   : {summary.get('avg_sl', 0)}%")

    # ── Comparison table ───────────────────────────────────────
    comp = [
        f"\n{'═'*68}",
        f"  COMPARISON — ALL CONFIGURATIONS",
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

    profitable = [s for s in summary_rows if s and s.get('exp_val', 0) > 0]
    if profitable:
        best = max(profitable, key=lambda x: x['exp_val'])
        comp.append(f"\n  BEST CONFIG : {best['name']}")
        comp.append(f"  Win rate    : {best['win_rate']}%")
        comp.append(f"  Exp value   : {best['exp_val']:+.2f}% per trade")
        comp.append(f"  Avg R:R     : 1:{best['avg_rr']}")
        comp.append(f"  Avg SL      : {best['avg_sl']}%")
        comp.append(f"\n  RECOMMENDATION:")
        comp.append(f"  Entry  : When price within {best.get('bull_buffer', 5)}% of HV Low")
        comp.append(f"  SL     : Low of the HV candle itself")
        comp.append(f"  Target : HV High (the resistance level)")
        comp.append(f"  Hold   : Max 10 trading days (2 weeks)")
        comp.append(f"  Filter : Only when Nifty basket above 100-day EMA")
    else:
        comp.append(f"\n  No configuration was consistently profitable")
        comp.append(f"  Consider reviewing entry conditions")

    comp.append(f"{'═'*68}\n")
    comp_str = '\n'.join(comp)
    all_output.append(comp_str)
    log(comp_str)

    # Save results
    final = '\n'.join(all_output)
    with open(OUTPUT_FILE, 'w') as f:
        f.write(final)

    log(f"\nResults saved: {OUTPUT_FILE}")
    log(f"View: cat {OUTPUT_FILE}")
    log(f"Page: less {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
