#!/usr/bin/env python3
"""
Backtest: Hybrid Strategy
Entry  : Weekly L3 (aligned with HV Low)
SL     : Daily L4 (tight)
T1     : Weekly H3 (50% position)
T2     : HV High (50% position)
Period : 2021-2026
"""
import sqlite3
from datetime import date, timedelta, datetime
import os

BASE_DIR = os.path.expanduser('~/nse-scanner')
DB_PATH  = os.path.join(BASE_DIR, 'nse_data.db')

def get_week_start(d):
    return d - timedelta(days=d.weekday())

def backtest(l3_vs_hv_max=3.0, above_hv_max=5.0,
             min_upside=8.0, hv_age_min=7, hv_age_max=150,
             max_hold=30, position_size=20000):

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute('''
        SELECT h.symbol, h.hv_date, h.hv_low, h.hv_high,
               ROUND((h.hv_high-h.hv_low)/h.hv_low*100,1) as upside_pct
        FROM hv_summary h
        WHERE h.hv_low > 0
        AND h.hv_high > h.hv_low * 1.05
        AND (h.hv_close-h.hv_low)/(h.hv_high-h.hv_low) >= 0.5
        AND h.hv_date >= "2021-01-01"
        AND h.hv_date <= "2026-05-01"
        ORDER BY h.hv_date ASC
    ''')
    setups = c.fetchall()
    trades = []

    for s in setups:
        sym     = s['symbol']
        hv_date = s['hv_date']
        hv_low  = float(s['hv_low'])
        hv_high = float(s['hv_high'])
        upside  = float(s['upside_pct'])

        if upside < min_upside:
            continue

        c.execute('''SELECT date,open,high,low,close,volume
                     FROM daily_prices WHERE symbol=? AND date > ?
                     ORDER BY date ASC''', (sym, hv_date))
        prices = c.fetchall()
        if not prices:
            continue

        hv_dt = datetime.strptime(hv_date, '%Y-%m-%d').date()

        for i, row in enumerate(prices):
            curr_dt = datetime.strptime(row['date'], '%Y-%m-%d').date()
            age = (curr_dt - hv_dt).days

            if age < hv_age_min: continue
            if age > hv_age_max: break
            if i == 0: continue

            # ── Weekly Camarilla ──────────────────────────
            week_start    = get_week_start(curr_dt)
            prev_w_end    = week_start - timedelta(days=1)
            prev_w_st     = week_start - timedelta(days=7)

            c.execute('''SELECT MAX(high) as wh, MIN(low) as wl
                         FROM daily_prices WHERE symbol=? AND date>=? AND date<=?''',
                      (sym, prev_w_st.strftime('%Y-%m-%d'),
                       prev_w_end.strftime('%Y-%m-%d')))
            wr = c.fetchone()
            c.execute('''SELECT close FROM daily_prices WHERE symbol=? AND date<=?
                         ORDER BY date DESC LIMIT 1''',
                      (sym, prev_w_end.strftime('%Y-%m-%d')))
            wc = c.fetchone()

            if not wr or not wr['wh'] or not wc: continue

            w_rng   = float(wr['wh']) - float(wr['wl'])
            w_close = float(wc['close'])
            if w_rng <= 0: continue

            wl3 = round(w_close - w_rng * 0.55/2, 2)
            wl4 = round(w_close - w_rng * 1.1/2,  2)
            wh3 = round(w_close + w_rng * 0.55/2, 2)

            # ── Daily L4 for SL ───────────────────────────
            prev = prices[i-1]
            d_rng = float(prev['high']) - float(prev['low'])
            d_l4  = round(float(prev['close']) - d_rng * 1.1/2, 2)

            # ── Entry filters ─────────────────────────────
            l3_vs_hv = abs(wl3 - hv_low) / hv_low * 100
            if l3_vs_hv > l3_vs_hv_max: continue

            curr_low   = float(row['low'])
            curr_close = float(row['close'])
            curr_open  = float(row['open'])

            # Touch weekly L3
            if curr_low > wl3 * 1.005: continue

            # Green candle
            if curr_close <= curr_open: continue

            # Not too far above HV Low
            pct_above = (curr_close - hv_low) / hv_low * 100
            if pct_above > above_hv_max: continue

            # SL must be below entry
            if d_l4 >= wl3: continue

            # HV High must be above entry
            if hv_high <= wl3: continue

            # R:R check
            risk = wl3 - d_l4
            if risk <= 0: continue
            rr_t1 = round((wh3 - wl3) / risk, 1)
            rr_t2 = round((hv_high - wl3) / risk, 1)
            if rr_t2 < 3.0: continue

            # ── Position sizing ───────────────────────────
            shares     = int(position_size / wl3)
            if shares < 1: continue
            l1_shares  = shares // 2
            l2_shares  = shares - l1_shares

            # ── Simulate trade ────────────────────────────
            p1_result  = 'TIMEOUT'
            p1_exit    = curr_close
            p2_result  = 'TIMEOUT'
            p2_exit    = curr_close
            days_held  = 0
            t1_hit_day = None

            for j, future in enumerate(prices[i+1:i+max_hold+1]):
                days_held += 1
                fh = float(future['high'])
                fl = float(future['low'])
                fc = float(future['close'])

                # Check SL first (Daily L4)
                if fl <= d_l4:
                    if p1_result == 'TIMEOUT':
                        p1_result = 'SL'
                        p1_exit   = d_l4
                    if p2_result == 'TIMEOUT':
                        p2_result = 'SL'
                        p2_exit   = d_l4
                    break

                # T1: Weekly H3
                if p1_result == 'TIMEOUT' and fh >= wh3:
                    p1_result  = 'T1'
                    p1_exit    = wh3
                    t1_hit_day = j

                # T2: HV High
                if p2_result == 'TIMEOUT' and fh >= hv_high:
                    p2_result = 'T2'
                    p2_exit   = hv_high
                    break

            # Timeout exits
            if p1_result == 'TIMEOUT':
                p1_exit = float(prices[min(i+max_hold, len(prices)-1)]['close'])
            if p2_result == 'TIMEOUT':
                p2_exit = float(prices[min(i+max_hold, len(prices)-1)]['close'])

            # ── Calculate P&L ─────────────────────────────
            p1_pnl_pct = round((p1_exit - wl3) / wl3 * 100, 2)
            p2_pnl_pct = round((p2_exit - wl3) / wl3 * 100, 2)
            p1_pnl_rs  = round(p1_pnl_pct/100 * position_size * 0.5, 0)
            p2_pnl_rs  = round(p2_pnl_pct/100 * position_size * 0.5, 0)
            total_pnl  = round(p1_pnl_rs + p2_pnl_rs, 0)

            # Overall result
            if p1_result == 'SL' and p2_result == 'SL':
                overall = 'FULL_LOSS'
            elif p1_result == 'T1' and p2_result == 'T2':
                overall = 'FULL_WIN'
            elif p1_result == 'T1':
                overall = 'T1_ONLY'
            elif p1_result == 'SL':
                overall = 'FULL_LOSS'
            else:
                overall = 'TIMEOUT'

            trades.append({
                'symbol'    : sym,
                'date'      : row['date'],
                'month'     : row['date'][:7],
                'entry'     : round(wl3, 2),
                'sl'        : round(d_l4, 2),
                'wh3'       : round(wh3, 2),
                'hv_high'   : round(hv_high, 2),
                'p1_result' : p1_result,
                'p2_result' : p2_result,
                'overall'   : overall,
                'p1_pnl_rs' : p1_pnl_rs,
                'p2_pnl_rs' : p2_pnl_rs,
                'total_pnl' : total_pnl,
                'rr_t1'     : rr_t1,
                'rr_t2'     : rr_t2,
                'days_held' : days_held,
                'sl_pct'    : round(risk/wl3*100, 2),
                'l3_vs_hv'  : round(l3_vs_hv, 2),
            })
            break

    conn.close()
    return trades

def analyze(trades, label, position_size=20000):
    if not trades:
        print(f"\n{label}: No trades")
        return None

    total_months = 64

    full_wins   = [t for t in trades if t['overall'] == 'FULL_WIN']
    t1_only     = [t for t in trades if t['overall'] == 'T1_ONLY']
    full_losses = [t for t in trades if t['overall'] == 'FULL_LOSS']
    timeouts    = [t for t in trades if t['overall'] == 'TIMEOUT']
    profitable  = [t for t in trades if t['total_pnl'] > 0]

    win_rate    = round(len(profitable)/len(trades)*100, 1)
    ev_rs       = round(sum(t['total_pnl'] for t in trades)/len(trades), 0)
    total_rs    = sum(t['total_pnl'] for t in trades)
    per_month   = round(len(trades)/total_months, 1)
    avg_sl_pct  = round(sum(t['sl_pct'] for t in trades)/len(trades), 2)
    avg_rr_t2   = round(sum(t['rr_t2'] for t in trades)/len(trades), 1)

    # P&L breakdown
    avg_full_win= round(sum(t['total_pnl'] for t in full_wins)/len(full_wins), 0) if full_wins else 0
    avg_t1_only = round(sum(t['total_pnl'] for t in t1_only)/len(t1_only), 0) if t1_only else 0
    avg_loss    = round(sum(t['total_pnl'] for t in full_losses)/len(full_losses), 0) if full_losses else 0

    # Drawdown
    cum = 0; peak = 0; max_dd = 0
    for t in trades:
        cum += t['total_pnl']
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    # Consecutive losses
    max_cl = 0; cl = 0
    for t in trades:
        if t['total_pnl'] <= 0: cl += 1; max_cl = max(max_cl, cl)
        else: cl = 0

    # Monthly
    months = {}
    for t in trades:
        m = t['month']
        if m not in months: months[m] = []
        months[m].append(t)

    all_months = set()
    d = date(2021, 1, 1)
    while d <= date(2026, 5, 1):
        all_months.add(d.strftime('%Y-%m'))
        if d.month == 12: d = date(d.year+1, 1, 1)
        else: d = date(d.year, d.month+1, 1)
    zero_months = len(all_months - set(months.keys()))

    print(f"\n{'='*65}")
    print(f"  {label}")
    print(f"{'='*65}")
    print(f"Period        : Jan 2021 — May 2026 | Rs {position_size:,}/trade")
    print(f"")
    print(f"FREQUENCY:")
    print(f"  Total trades : {len(trades)} ({per_month}/month)")
    print(f"  Zero months  : {zero_months}/65")
    print(f"")
    print(f"OUTCOMES:")
    print(f"  Full wins (T1+T2) : {len(full_wins)} ({round(len(full_wins)/len(trades)*100,1)}%)")
    print(f"  T1 only (H3 hit)  : {len(t1_only)} ({round(len(t1_only)/len(trades)*100,1)}%)")
    print(f"  Full losses (SL)  : {len(full_losses)} ({round(len(full_losses)/len(trades)*100,1)}%)")
    print(f"  Timeouts          : {len(timeouts)} ({round(len(timeouts)/len(trades)*100,1)}%)")
    print(f"  Profitable trades : {len(profitable)} ({win_rate}%)")
    print(f"")
    print(f"RETURNS:")
    print(f"  Avg full win  : Rs +{avg_full_win:,}")
    print(f"  Avg T1 only   : Rs +{avg_t1_only:,}")
    print(f"  Avg loss      : Rs {avg_loss:,}")
    print(f"  EV/trade      : Rs {ev_rs:,}")
    print(f"  Monthly avg   : Rs {round(total_rs/total_months):,}")
    print(f"  Total 5yr     : Rs {total_rs:+,}")
    print(f"")
    print(f"RISK:")
    print(f"  Avg SL %      : {avg_sl_pct}%")
    print(f"  Avg R:R (T2)  : 1:{avg_rr_t2}")
    print(f"  Max drawdown  : Rs {max_dd:,.0f}")
    print(f"  Max consec L  : {max_cl}")
    print(f"")
    print(f"YEAR BY YEAR:")
    for year in range(2021, 2027):
        yt = [t for t in trades if t['date'].startswith(str(year))]
        if not yt: continue
        yp = [t for t in yt if t['total_pnl'] > 0]
        ytotal = sum(t['total_pnl'] for t in yt)
        yw = [t for t in yt if t['overall'] == 'FULL_WIN']
        yt1= [t for t in yt if t['overall'] == 'T1_ONLY']
        ysl= [t for t in yt if t['overall'] == 'FULL_LOSS']
        print(f"  {year}: {len(yt):>3} trades | "
              f"WR:{round(len(yp)/len(yt)*100,1):>5}% | "
              f"FW:{len(yw)} T1:{len(yt1)} SL:{len(ysl)} | "
              f"P&L: Rs {ytotal:>+8,.0f}")

    return {
        'label'     : label,
        'trades'    : len(trades),
        'per_month' : per_month,
        'zero_months': zero_months,
        'win_rate'  : win_rate,
        'ev_rs'     : ev_rs,
        'total_rs'  : total_rs,
        'max_dd'    : max_dd,
        'max_cl'    : max_cl,
        'full_wins' : len(full_wins),
        't1_only'   : len(t1_only),
        'full_losses': len(full_losses),
    }

if __name__ == '__main__':
    print("Hybrid Strategy Backtest (2021-2026)")
    print("Entry=W-L3 | SL=Daily-L4 | T1=W-H3(50%) | T2=HV-High(50%)")
    print("Testing different thresholds...")

    configs = [
        (2.0, 3.0, 'L3vsHV=2% | Above=3%'),
        (3.0, 3.0, 'L3vsHV=3% | Above=3%'),
        (3.0, 5.0, 'L3vsHV=3% | Above=5%'),
        (4.0, 5.0, 'L3vsHV=4% | Above=5%'),
        (5.0, 5.0, 'L3vsHV=5% | Above=5%'),
    ]

    results = []
    for l3, above, label in configs:
        t = backtest(l3_vs_hv_max=l3, above_hv_max=above)
        r = analyze(t, label)
        if r: results.append(r)

    print(f"\n{'='*80}")
    print(f"  COMPARISON TABLE")
    print(f"{'='*80}")
    print(f"{'Config':<28} {'/Mo':>4} {'0Mo':>4} {'WR%':>5} "
          f"{'FW':>4} {'T1':>4} {'SL':>4} "
          f"{'EV':>8} {'Total':>10} {'MaxDD':>8} {'ML':>4}")
    print(f"{'─'*80}")
    for r in results:
        print(f"{r['label']:<28} {r['per_month']:>4} "
              f"{r['zero_months']:>4} {r['win_rate']:>4}% "
              f"{r['full_wins']:>4} {r['t1_only']:>4} {r['full_losses']:>4} "
              f"Rs{r['ev_rs']:>6,} "
              f"Rs{r['total_rs']:>8,} "
              f"Rs{r['max_dd']:>6,.0f} "
              f"{r['max_cl']:>4}")
