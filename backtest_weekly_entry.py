#!/usr/bin/env python3
"""
Backtest: Weekly Camarilla Entry + HV Low alignment
Entry: Weekly L3 aligned with HV Low
SL: Weekly L4
Target: HV High
Test different: L3vsHV%, Above HV%, Volume filters
"""
import sqlite3
from datetime import date, timedelta, datetime
import os

BASE_DIR = os.path.expanduser('~/nse-scanner')
DB_PATH  = os.path.join(BASE_DIR, 'nse_data.db')

def get_week_start(d):
    return d - timedelta(days=d.weekday())

def calc_weekly_cam(high, low, close):
    rng = high - low
    return {
        'h4': close + rng * 1.1/2,
        'h3': close + rng * 0.55/2,
        'l3': close - rng * 0.55/2,
        'l4': close - rng * 1.1/2,
    }

def backtest(l3_vs_hv_max=2.0, above_hv_max=3.0,
             vol_min=0.0, min_upside=8.0,
             hv_age_min=7, hv_age_max=90,
             max_hold=15, position_size=20000):

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
        hv_low  = s['hv_low']
        hv_high = s['hv_high']
        upside  = s['upside_pct']

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

            # Get weekly Camarilla
            week_start      = get_week_start(curr_dt)
            prev_week_end   = week_start - timedelta(days=1)
            prev_week_start = week_start - timedelta(days=7)

            c.execute('''SELECT MAX(high) as wh, MIN(low) as wl
                         FROM daily_prices WHERE symbol=? AND date>=? AND date<=?''',
                      (sym, prev_week_start.strftime('%Y-%m-%d'),
                       prev_week_end.strftime('%Y-%m-%d')))
            wr = c.fetchone()
            c.execute('''SELECT close FROM daily_prices WHERE symbol=? AND date<=?
                         ORDER BY date DESC LIMIT 1''',
                      (sym, prev_week_end.strftime('%Y-%m-%d')))
            wc = c.fetchone()

            if not wr or not wr['wh'] or not wc:
                continue

            cam = calc_weekly_cam(float(wr['wh']), float(wr['wl']), float(wc['close']))
            wl3 = cam['l3']
            wl4 = cam['l4']
            wh3 = cam['h3']

            # L3 vs HV Low alignment
            l3_vs_hv = abs(wl3 - hv_low) / hv_low * 100
            if l3_vs_hv > l3_vs_hv_max:
                continue

            # Price action
            curr_low   = float(row['low'])
            curr_close = float(row['close'])
            curr_open  = float(row['open'])
            curr_vol   = float(row['volume'])

            # Touch weekly L3
            if curr_low > wl3 * 1.005:
                continue

            # Green candle
            if curr_close <= curr_open:
                continue

            # Above HV Low check
            pct_above = (curr_close - hv_low) / hv_low * 100
            if pct_above > above_hv_max:
                continue

            # Volume check
            if vol_min > 0:
                c.execute('''SELECT AVG(volume) FROM (
                             SELECT volume FROM daily_prices
                             WHERE symbol=? AND date < ?
                             ORDER BY date DESC LIMIT 20)''',
                          (sym, row['date']))
                avg_vol   = c.fetchone()[0] or 1
                vol_ratio = curr_vol / avg_vol
                if vol_ratio < vol_min:
                    continue

            # R:R
            entry  = wl3
            sl     = wl4
            target = hv_high
            risk   = entry - sl
            if risk <= 0: continue
            rr = round((target - entry) / risk, 1)
            if rr < 3.0: continue

            # Simulate
            result     = 'TIMEOUT'
            exit_price = float(prices[min(i+max_hold, len(prices)-1)]['close'])
            days_held  = 0
            t1_hit     = False

            for future in prices[i+1:i+max_hold+1]:
                days_held += 1
                fh = float(future['high'])
                fl = float(future['low'])

                if fl <= wl4:
                    result = 'SL'
                    exit_price = wl4
                    break
                elif not t1_hit and fh >= wh3:
                    t1_hit = True
                    result = 'T1'
                    exit_price = wh3
                    break
                elif fh >= hv_high:
                    result = 'TARGET'
                    exit_price = hv_high
                    break

            pnl_pct = round((exit_price - entry) / entry * 100, 2)
            pnl_rs  = round(pnl_pct/100 * position_size, 0)

            trades.append({
                'symbol'  : sym,
                'date'    : row['date'],
                'month'   : row['date'][:7],
                'entry'   : round(entry, 2),
                'sl'      : round(sl, 2),
                'wh3'     : round(wh3, 2),
                'target'  : round(hv_high, 2),
                'result'  : result,
                'pnl_pct' : pnl_pct,
                'pnl_rs'  : pnl_rs,
                'rr'      : rr,
                'days_held': days_held,
            })
            break

    conn.close()
    return trades

def analyze(trades, label, position_size=20000):
    if not trades:
        print(f"\n{label}: No trades")
        return None

    winners  = [t for t in trades if t['pnl_rs'] > 0]
    losers   = [t for t in trades if t['pnl_rs'] <= 0]
    t1_hits  = [t for t in trades if t['result'] == 'T1']
    tgt_hits = [t for t in trades if t['result'] == 'TARGET']
    sl_hits  = [t for t in trades if t['result'] == 'SL']
    timeouts = [t for t in trades if t['result'] == 'TIMEOUT']

    total_months = 64
    win_rate  = round(len(winners)/len(trades)*100, 1)
    avg_win_r = round(sum(t['pnl_rs'] for t in winners)/len(winners), 0) if winners else 0
    avg_los_r = round(sum(t['pnl_rs'] for t in losers)/len(losers), 0) if losers else 0
    ev_rs     = round(sum(t['pnl_rs'] for t in trades)/len(trades), 0)
    total_rs  = sum(t['pnl_rs'] for t in trades)
    per_month = round(len(trades)/total_months, 1)
    per_week  = round(len(trades)/(total_months*4.33), 2)
    avg_hold  = round(sum(t['days_held'] for t in trades)/len(trades), 1)

    # Max drawdown
    cum = 0; peak = 0; max_dd = 0
    for t in trades:
        cum += t['pnl_rs']
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    # Consecutive losses
    max_cl = 0; cl = 0
    for t in trades:
        if t['pnl_rs'] <= 0: cl += 1; max_cl = max(max_cl, cl)
        else: cl = 0

    # Monthly distribution
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

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"Trades/month  : {per_month} | /week: {per_week}")
    print(f"Zero months   : {zero_months}/65")
    print(f"Win rate      : {win_rate}%")
    print(f"T1 (W-H3)     : {len(t1_hits)} | TARGET(HV): {len(tgt_hits)} | SL: {len(sl_hits)} | TO: {len(timeouts)}")
    print(f"Avg win       : Rs +{avg_win_r:,}")
    print(f"Avg loss      : Rs {avg_los_r:,}")
    print(f"EV/trade      : Rs {ev_rs:,}")
    print(f"Monthly avg   : Rs {round(total_rs/total_months):,}")
    print(f"Total 5yr     : Rs {total_rs:+,}")
    print(f"Max drawdown  : Rs {max_dd:,.0f}")
    print(f"Max consec L  : {max_cl}")
    print(f"Avg hold      : {avg_hold} days")

    print(f"\nYear by Year:")
    for year in range(2021, 2027):
        yt = [t for t in trades if t['date'].startswith(str(year))]
        if not yt: continue
        yw = [t for t in yt if t['pnl_rs'] > 0]
        yp = sum(t['pnl_rs'] for t in yt)
        print(f"  {year}: {len(yt):>3} trades ({round(len(yt)/12,1)}/mo) | "
              f"WR:{round(len(yw)/len(yt)*100,1):>5}% | "
              f"P&L: Rs {yp:>+8,.0f}")

    return {
        'label': label,
        'trades': len(trades),
        'per_month': per_month,
        'zero_months': zero_months,
        'win_rate': win_rate,
        'ev_rs': ev_rs,
        'total_rs': total_rs,
        'max_dd': max_dd,
        'max_cl': max_cl,
    }

if __name__ == '__main__':
    print("Weekly Camarilla Entry Backtest (2021-2026)")
    print("Testing different configurations...")

    configs = [
        # (l3_vs_hv, above_hv, vol, label)
        (1.0, 1.0, 0.0, "W-L3vsHV=1% | Above=1% | No vol"),
        (1.0, 3.0, 0.0, "W-L3vsHV=1% | Above=3% | No vol"),
        (2.0, 3.0, 0.0, "W-L3vsHV=2% | Above=3% | No vol"),
        (3.0, 3.0, 0.0, "W-L3vsHV=3% | Above=3% | No vol"),
        (2.0, 3.0, 0.7, "W-L3vsHV=2% | Above=3% | Vol=0.7x"),
        (2.0, 3.0, 1.0, "W-L3vsHV=2% | Above=3% | Vol=1x"),
        (3.0, 5.0, 0.0, "W-L3vsHV=3% | Above=5% | No vol"),
        (4.0, 5.0, 0.0, "W-L3vsHV=4% | Above=5% | No vol"),
        (5.0, 5.0, 0.0, "W-L3vsHV=5% | Above=5% | No vol"),
    ]

    results = []
    for l3, above, vol, label in configs:
        t = backtest(l3_vs_hv_max=l3, above_hv_max=above, vol_min=vol)
        r = analyze(t, label)
        if r: results.append(r)

    print(f"\n{'='*75}")
    print(f"  COMPARISON TABLE")
    print(f"{'='*75}")
    print(f"{'Config':<38} {'/Mo':>4} {'0Mo':>4} {'WR%':>5} "
          f"{'EV':>7} {'Total':>9} {'MaxDD':>7} {'ML':>4}")
    print(f"{'─'*75}")
    for r in results:
        print(f"{r['label']:<38} {r['per_month']:>4} "
              f"{r['zero_months']:>4} {r['win_rate']:>4}% "
              f"Rs{r['ev_rs']:>5,} "
              f"Rs{r['total_rs']:>7,} "
              f"Rs{r['max_dd']:>5,.0f} "
              f"{r['max_cl']:>4}")
