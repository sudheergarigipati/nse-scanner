#!/usr/bin/env python3
"""
Full 5-year backtest comparing L3vsHV thresholds
Shows: signals/month, win rate, EV, drawdown
"""
import sqlite3
from datetime import date, timedelta, datetime
import os

BASE_DIR = os.path.expanduser('~/nse-scanner')
DB_PATH  = os.path.join(BASE_DIR, 'nse_data.db')

def calc_cam(high, low, close):
    rng = high - low
    return {
        'l3': close - rng * 0.55/2,
        'l4': close - rng * 1.1/2,
    }

def backtest(l3_vs_hv_max=2.0, vol_min=1.0, min_rr=5.0,
             min_upside=8.0, hv_age_min=7, hv_age_max=90,
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
            if i == 0: continue

            prev = prices[i-1]
            cam  = calc_cam(float(prev['high']), float(prev['low']), float(prev['close']))
            l3   = cam['l3']
            l4   = cam['l4']

            l3_vs_hv = abs(l3 - hv_low) / hv_low * 100
            if l3_vs_hv > l3_vs_hv_max:
                continue

            curr_low   = float(row['low'])
            curr_close = float(row['close'])
            curr_open  = float(row['open'])
            curr_vol   = float(row['volume'])

            if curr_low > l3 * 1.005: continue
            if curr_close <= curr_open: continue

            c.execute('''SELECT AVG(volume) FROM (
                         SELECT volume FROM daily_prices
                         WHERE symbol=? AND date < ?
                         ORDER BY date DESC LIMIT 20)''',
                      (sym, row['date']))
            avg_vol   = c.fetchone()[0] or 1
            vol_ratio = curr_vol / avg_vol
            if vol_ratio < vol_min: continue

            entry = l3
            sl    = l4
            risk  = entry - sl
            if risk <= 0: continue
            rr = round((hv_high - entry) / risk, 1)
            if rr < min_rr: continue

            # Simulate trade
            result     = 'TIMEOUT'
            exit_price = float(prices[min(i+max_hold, len(prices)-1)]['close'])
            days_held  = 0

            for future in prices[i+1:i+max_hold+1]:
                days_held += 1
                if float(future['low']) <= l4:
                    result = 'SL'
                    exit_price = l4
                    break
                elif float(future['high']) >= hv_high:
                    result = 'TARGET'
                    exit_price = hv_high
                    break

            pnl_pct = round((exit_price - entry) / entry * 100, 2)
            pnl_rs  = round(pnl_pct/100 * position_size, 0)

            trades.append({
                'symbol'    : sym,
                'date'      : row['date'],
                'month'     : row['date'][:7],
                'entry'     : round(entry, 2),
                'sl'        : round(sl, 2),
                'target'    : round(hv_high, 2),
                'exit'      : round(exit_price, 2),
                'result'    : result,
                'pnl_pct'   : pnl_pct,
                'pnl_rs'    : pnl_rs,
                'rr'        : rr,
                'days_held' : days_held,
                'l3_vs_hv'  : round(l3_vs_hv, 2),
            })
            break

    conn.close()
    return trades

def analyze(trades, label, position_size=20000):
    if not trades:
        print(f"\n{label}: No trades found")
        return

    winners  = [t for t in trades if t['result'] == 'TARGET']
    losers   = [t for t in trades if t['result'] == 'SL']
    timeouts = [t for t in trades if t['result'] == 'TIMEOUT']

    total_months = 64  # Jan 2021 to May 2026
    total_weeks  = total_months * 4.33

    win_rate  = round(len(winners)/len(trades)*100, 1)
    avg_win_p = round(sum(t['pnl_pct'] for t in winners)/len(winners), 2) if winners else 0
    avg_los_p = round(sum(t['pnl_pct'] for t in losers)/len(losers), 2) if losers else 0
    avg_win_r = round(sum(t['pnl_rs'] for t in winners)/len(winners), 0) if winners else 0
    avg_los_r = round(sum(t['pnl_rs'] for t in losers)/len(losers), 0) if losers else 0
    ev_pct    = round(sum(t['pnl_pct'] for t in trades)/len(trades), 2)
    ev_rs     = round(sum(t['pnl_rs'] for t in trades)/len(trades), 0)
    total_rs  = sum(t['pnl_rs'] for t in trades)
    avg_hold  = round(sum(t['days_held'] for t in trades)/len(trades), 1)
    per_month = round(len(trades)/total_months, 1)
    per_week  = round(len(trades)/total_weeks, 2)

    # Max drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    for t in trades:
        cumulative += t['pnl_rs']
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Consecutive losses
    max_consec_loss = 0
    curr_consec = 0
    for t in trades:
        if t['pnl_rs'] <= 0:
            curr_consec += 1
            max_consec_loss = max(max_consec_loss, curr_consec)
        else:
            curr_consec = 0

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"Period        : Jan 2021 — May 2026 (64 months)")
    print(f"Position size : Rs {position_size:,}")
    print(f"")
    print(f"FREQUENCY:")
    print(f"  Total trades : {len(trades)}")
    print(f"  Per month    : {per_month}")
    print(f"  Per week     : {per_week}")
    print(f"")
    print(f"PERFORMANCE:")
    print(f"  Win rate     : {win_rate}%")
    print(f"  Targets hit  : {len(winners)}")
    print(f"  SL hit       : {len(losers)}")
    print(f"  Timeouts     : {len(timeouts)}")
    print(f"  Avg hold     : {avg_hold} days")
    print(f"")
    print(f"RETURNS (per trade):")
    print(f"  Avg win      : +{avg_win_p}% (Rs +{avg_win_r:,})")
    print(f"  Avg loss     : {avg_los_p}% (Rs {avg_los_r:,})")
    print(f"  EV/trade     : {ev_pct}% (Rs {ev_rs:,})")
    print(f"")
    print(f"TOTAL RETURNS:")
    print(f"  Total P&L    : Rs {total_rs:+,.0f}")
    print(f"  Monthly avg  : Rs {round(total_rs/total_months):+,}")
    print(f"")
    print(f"RISK:")
    print(f"  Max drawdown : Rs {max_dd:,.0f}")
    print(f"  Max consec L : {max_consec_loss} losses in a row")
    print(f"")

    # Monthly breakdown
    months = {}
    for t in trades:
        m = t['month']
        if m not in months:
            months[m] = []
        months[m].append(t)

    print(f"YEAR BY YEAR:")
    for year in range(2021, 2027):
        yt = [t for t in trades if t['date'].startswith(str(year))]
        if not yt: continue
        yw = [t for t in yt if t['result'] == 'TARGET']
        yp = round(sum(t['pnl_rs'] for t in yt), 0)
        wr = round(len(yw)/len(yt)*100, 1)
        pm = round(len(yt)/12, 1)
        print(f"  {year}: {len(yt):>3} trades ({pm}/mo) | "
              f"WR:{wr:>5}% | "
              f"P&L: Rs {yp:>+8,.0f}")

    # Monthly signals distribution
    monthly_counts = [len(v) for v in months.values()]
    if monthly_counts:
        print(f"\nMONTHLY SIGNAL DISTRIBUTION:")
        print(f"  Min signals/month: {min(monthly_counts)}")
        print(f"  Max signals/month: {max(monthly_counts)}")
        print(f"  Avg signals/month: {round(sum(monthly_counts)/len(monthly_counts),1)}")
        zero_months = sum(1 for v in months.values() if len(v) == 0)
        print(f"  Months with 0 signals: checking...")

        # Check all months in period
        all_months = set()
        d = date(2021, 1, 1)
        while d <= date(2026, 5, 1):
            all_months.add(d.strftime('%Y-%m'))
            d = date(d.year + (d.month//12), (d.month%12)+1, 1)
        zero_months = len(all_months - set(months.keys()))
        print(f"  Months with 0 signals: {zero_months} out of {len(all_months)}")

    return {
        'trades': len(trades),
        'per_month': per_month,
        'win_rate': win_rate,
        'ev_rs': ev_rs,
        'total_rs': total_rs,
        'max_dd': max_dd,
        'max_consec_loss': max_consec_loss,
    }

if __name__ == '__main__':
    print("Running full 5-year backtest...")
    print("Testing L3vsHV thresholds: 1%, 2%, 3%, 4%, 5%")

    results = []
    for threshold in [1.0, 2.0, 3.0, 4.0, 5.0]:
        t = backtest(l3_vs_hv_max=threshold, vol_min=1.0, min_rr=5.0)
        r = analyze(t, f"L3vsHV<={threshold}% | Vol>=1x | RR>=5")
        if r:
            r['threshold'] = threshold
            results.append(r)

    # Summary table
    print(f"\n{'='*70}")
    print(f"  COMPARISON TABLE")
    print(f"{'='*70}")
    print(f"{'Threshold':<12} {'Trades':>6} {'/Month':>6} {'WR%':>5} "
          f"{'EV/Rs':>8} {'Total':>10} {'MaxDD':>8} {'MaxL':>5}")
    print(f"{'─'*70}")
    for r in results:
        print(f"L3vsHV<={r['threshold']}%  {r['trades']:>6} "
              f"{r['per_month']:>6} {r['win_rate']:>4}% "
              f"Rs{r['ev_rs']:>6,} "
              f"Rs{r['total_rs']:>8,} "
              f"Rs{r['max_dd']:>6,.0f} "
              f"{r['max_consec_loss']:>5}")
