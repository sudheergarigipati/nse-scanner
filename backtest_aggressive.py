#!/usr/bin/env python3
"""
Backtest: Aggressive configurations to hit Rs 5,000-10,000/month
Test different position sizes and target strategies
"""
import sqlite3
from datetime import date, timedelta, datetime
import os

BASE_DIR = os.path.expanduser('~/nse-scanner')
DB_PATH  = os.path.join(BASE_DIR, 'nse_data.db')

def get_week_start(d):
    return d - timedelta(days=d.weekday())

def backtest(position_size=20000, total_capital=50000,
             target_mode='hybrid',  # 'hybrid', 't2_only', 'weekly_only'
             l3_vs_hv_max=5.0, above_hv_max=5.0,
             min_upside=8.0, hv_age_max=150, max_hold=30):

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

    # First pass — all raw signals
    raw_signals = []
    for s in setups:
        sym     = s['symbol']
        hv_date = s['hv_date']
        hv_low  = float(s['hv_low'])
        hv_high = float(s['hv_high'])
        upside  = float(s['upside_pct'])

        if upside < min_upside: continue

        c.execute('''SELECT date,open,high,low,close,volume
                     FROM daily_prices WHERE symbol=? AND date > ?
                     ORDER BY date ASC''', (sym, hv_date))
        prices = c.fetchall()
        if not prices: continue

        hv_dt = datetime.strptime(hv_date, '%Y-%m-%d').date()

        for i, row in enumerate(prices):
            curr_dt = datetime.strptime(row['date'], '%Y-%m-%d').date()
            age = (curr_dt - hv_dt).days

            if age < 7: continue
            if age > hv_age_max: break
            if i == 0: continue

            # Weekly cam
            week_start = get_week_start(curr_dt)
            prev_w_end = week_start - timedelta(days=1)
            prev_w_st  = week_start - timedelta(days=7)

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
            w_rng = float(wr['wh']) - float(wr['wl'])
            w_close = float(wc['close'])
            if w_rng <= 0: continue

            wl3 = round(w_close - w_rng * 0.55/2, 2)
            wl4 = round(w_close - w_rng * 1.1/2,  2)
            wh3 = round(w_close + w_rng * 0.55/2, 2)

            # Daily L4
            prev  = prices[i-1]
            d_rng = float(prev['high']) - float(prev['low'])
            d_l4  = round(float(prev['close']) - d_rng * 1.1/2, 2)

            l3_vs_hv = abs(wl3 - hv_low) / hv_low * 100
            if l3_vs_hv > l3_vs_hv_max: continue

            curr_low   = float(row['low'])
            curr_close = float(row['close'])
            curr_open  = float(row['open'])

            if curr_low > wl3 * 1.005: continue
            if curr_close <= curr_open: continue

            pct_above = (curr_close - hv_low) / hv_low * 100
            if pct_above > above_hv_max: continue
            if d_l4 >= wl3: continue
            if hv_high <= wl3: continue

            risk = wl3 - d_l4
            if risk <= 0: continue
            rr = round((hv_high - wl3) / risk, 1)
            if rr < 3.0: continue

            raw_signals.append({
                'signal_date': row['date'],
                'symbol'     : sym,
                'entry'      : wl3,
                'sl'         : d_l4,
                'wh3'        : wh3,
                'hv_high'    : hv_high,
                'prices_idx' : i,
                'prices'     : prices,
            })
            break

    conn.close()
    raw_signals.sort(key=lambda x: x['signal_date'])

    # Second pass — with capital
    capital     = total_capital
    open_trades = []
    all_trades  = []
    skipped     = 0

    for sig in raw_signals:
        sig_date = sig['signal_date']
        sym      = sig['symbol']

        # Close finished trades
        still_open = []
        for ot in open_trades:
            if ot['exit_date'] <= sig_date:
                capital += ot['capital_used'] + ot['total_pnl']
                all_trades.append(ot)
            else:
                still_open.append(ot)
        open_trades = still_open

        if any(ot['symbol'] == sym for ot in open_trades):
            skipped += 1
            continue
        if capital < position_size:
            skipped += 1
            continue

        prices  = sig['prices']
        i       = sig['prices_idx']
        wl3     = sig['entry']
        d_l4    = sig['sl']
        wh3     = sig['wh3']
        hv_high = sig['hv_high']
        shares  = int(position_size / wl3)
        if shares < 1: continue

        # Targets based on mode
        if target_mode == 'hybrid':
            l1_sh = shares // 2
            l2_sh = shares - l1_sh
        elif target_mode == 't2_only':
            l1_sh = 0
            l2_sh = shares
        elif target_mode == 'weekly_only':
            l1_sh = shares
            l2_sh = 0

        p1_result = 'TIMEOUT'
        p2_result = 'TIMEOUT'
        p1_exit   = float(prices[min(i+max_hold, len(prices)-1)]['close'])
        p2_exit   = p1_exit
        exit_date = prices[min(i+max_hold, len(prices)-1)]['date']
        days_held = 0

        for future in prices[i+1:i+max_hold+1]:
            days_held += 1
            fh = float(future['high'])
            fl = float(future['low'])

            if fl <= d_l4:
                p1_result = 'SL'; p1_exit = d_l4
                p2_result = 'SL'; p2_exit = d_l4
                exit_date = future['date']
                break

            if p1_result == 'TIMEOUT' and l1_sh > 0 and fh >= wh3:
                p1_result = 'T1'; p1_exit = wh3

            if p2_result == 'TIMEOUT' and l2_sh > 0 and fh >= hv_high:
                p2_result = 'T2'; p2_exit = hv_high
                exit_date = future['date']
                break

        p1_pnl = round((p1_exit - wl3) * l1_sh, 0)
        p2_pnl = round((p2_exit - wl3) * l2_sh, 0)
        total  = p1_pnl + p2_pnl

        capital_used = shares * wl3
        capital -= capital_used

        if p1_result == 'SL': overall = 'FULL_LOSS'
        elif p1_result == 'T1' and p2_result == 'T2': overall = 'FULL_WIN'
        elif p1_result == 'T1': overall = 'T1_ONLY'
        elif p2_result == 'T2': overall = 'T2_WIN'
        else: overall = 'TIMEOUT'

        open_trades.append({
            'symbol'      : sym,
            'signal_date' : sig_date,
            'exit_date'   : exit_date,
            'capital_used': round(capital_used, 0),
            'total_pnl'   : total,
            'overall'     : overall,
            'days_held'   : days_held,
        })

    for ot in open_trades:
        capital += ot['capital_used'] + ot['total_pnl']
        all_trades.append(ot)

    return all_trades, skipped, capital

def analyze(trades, skipped, final_capital, label,
            total_capital=50000, position_size=20000):
    if not trades:
        print(f"\n{label}: No trades")
        return None

    total_months = 64
    winners  = [t for t in trades if t['total_pnl'] > 0]
    losers   = [t for t in trades if t['total_pnl'] <= 0]
    total_pnl= sum(t['total_pnl'] for t in trades)
    win_rate = round(len(winners)/len(trades)*100,1)
    ev       = round(total_pnl/len(trades),0)
    per_month= round(len(trades)/total_months,1)

    # Monthly
    months = {}
    for t in trades:
        m = t['signal_date'][:7]
        if m not in months: months[m] = []
        months[m].append(t)

    monthly_pnls = [sum(t['total_pnl'] for t in v)
                    for v in months.values()]
    neg = [m for m in monthly_pnls if m < 0]
    pos = [m for m in monthly_pnls if m > 0]
    all_m = set()
    d = date(2021,1,1)
    while d <= date(2026,5,1):
        all_m.add(d.strftime('%Y-%m'))
        if d.month == 12: d = date(d.year+1,1,1)
        else: d = date(d.year, d.month+1, 1)
    zero = len(all_m - set(months.keys()))

    # Max drawdown
    cum=0; peak=0; max_dd=0
    for t in trades:
        cum += t['total_pnl']
        peak = max(peak, cum)
        max_dd = max(max_dd, peak-cum)

    # Consecutive losses
    max_cl=0; cl=0
    for t in trades:
        if t['total_pnl']<=0: cl+=1; max_cl=max(max_cl,cl)
        else: cl=0

    avg_pos  = round(len(pos)/len(pos)*1 if pos else 0)
    avg_pos_m= round(sum(pos)/len(pos),0) if pos else 0
    avg_neg_m= round(sum(neg)/len(neg),0) if neg else 0

    print(f"\n{'='*65}")
    print(f"  {label}")
    print(f"  Capital: Rs {total_capital:,} | Position: Rs {position_size:,}/trade")
    print(f"{'='*65}")
    print(f"Trades/month  : {per_month} | Skipped: {skipped}")
    print(f"Win rate      : {win_rate}%")
    print(f"EV/trade      : Rs {ev:,}")
    print(f"Total 5yr P&L : Rs {total_pnl:+,}")
    print(f"Final capital : Rs {final_capital:,.0f}")
    print(f"Return        : {round((final_capital-total_capital)/total_capital*100,1)}%")
    print(f"Max drawdown  : Rs {max_dd:,.0f}")
    print(f"Max consec L  : {max_cl}")
    print(f"")
    print(f"Monthly stats:")
    print(f"  Profitable  : {len(pos)} months | Avg: Rs {avg_pos_m:+,}")
    print(f"  Loss months : {len(neg)} months | Avg: Rs {avg_neg_m:,}")
    print(f"  Zero months : {zero}")
    print(f"  Monthly avg : Rs {round(total_pnl/total_months):+,}")
    print(f"")

    # Year by year
    print(f"Year by Year:")
    for year in range(2021,2027):
        yt = [t for t in trades if t['signal_date'].startswith(str(year))]
        if not yt: continue
        yp = sum(t['total_pnl'] for t in yt)
        yw = [t for t in yt if t['total_pnl']>0]
        print(f"  {year}: {len(yt):>3} trades | "
              f"WR:{round(len(yw)/len(yt)*100,1):>5}% | "
              f"P&L: Rs {yp:>+8,.0f} | "
              f"Monthly avg: Rs {round(yp/12):>+6,}")

    return {
        'label'    : label,
        'trades'   : len(trades),
        'per_month': per_month,
        'win_rate' : win_rate,
        'ev'       : ev,
        'total_pnl': total_pnl,
        'monthly'  : round(total_pnl/total_months),
        'max_dd'   : max_dd,
        'max_cl'   : max_cl,
        'pos_months': len(pos),
        'neg_months': len(neg),
        'avg_pos_m' : avg_pos_m,
        'avg_neg_m' : avg_neg_m,
        'final'    : final_capital,
    }

if __name__ == '__main__':
    print("AGGRESSIVE BACKTEST — Target Rs 5,000-10,000/month")
    print("="*65)

    configs = [
        # (position, capital, mode, label)
        (20000, 50000,  'hybrid',      'Rs20k/trade | Rs50k | Hybrid T1+T2'),
        (30000, 50000,  'hybrid',      'Rs30k/trade | Rs50k | Hybrid T1+T2'),
        (40000, 50000,  'hybrid',      'Rs40k/trade | Rs50k | Hybrid T1+T2'),
        (20000, 50000,  't2_only',     'Rs20k/trade | Rs50k | T2 only (HV High)'),
        (30000, 50000,  't2_only',     'Rs30k/trade | Rs50k | T2 only (HV High)'),
        (40000, 50000,  't2_only',     'Rs40k/trade | Rs50k | T2 only (HV High)'),
        (20000, 100000, 'hybrid',      'Rs20k/trade | Rs1L  | Hybrid T1+T2'),
        (20000, 100000, 't2_only',     'Rs20k/trade | Rs1L  | T2 only (HV High)'),
        (30000, 100000, 't2_only',     'Rs30k/trade | Rs1L  | T2 only (HV High)'),
        (50000, 100000, 't2_only',     'Rs50k/trade | Rs1L  | T2 only (HV High)'),
    ]

    results = []
    for pos, cap, mode, label in configs:
        t, sk, final = backtest(
            position_size=pos,
            total_capital=cap,
            target_mode=mode
        )
        r = analyze(t, sk, final, label, cap, pos)
        if r: results.append(r)

    # Summary table
    print(f"\n{'='*90}")
    print(f"  COMPARISON TABLE")
    print(f"{'='*90}")
    print(f"{'Config':<38} {'/Mo':>4} {'WR%':>5} {'EV':>7} "
          f"{'Monthly':>8} {'Total':>10} {'MaxDD':>8} "
          f"{'+Mo':>4} {'-Mo':>4} {'Final':>10}")
    print(f"{'─'*90}")
    for r in results:
        hit = '✅' if r['monthly'] >= 5000 else '⚠️' if r['monthly'] >= 3000 else '❌'
        print(f"{r['label']:<38} {r['per_month']:>4} "
              f"{r['win_rate']:>4}% Rs{r['ev']:>5,} "
              f"Rs{r['monthly']:>6,} "
              f"Rs{r['total_pnl']:>8,} "
              f"Rs{r['max_dd']:>6,.0f} "
              f"{r['pos_months']:>4} {r['neg_months']:>4} "
              f"Rs{r['final']:>8,.0f} {hit}")
