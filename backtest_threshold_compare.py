#!/usr/bin/env python3
"""
Backtest: Compare different L3 vs HV thresholds
Find sweet spot between signal frequency and quality
"""
import sqlite3
from datetime import timedelta, datetime
import os

BASE_DIR = os.path.expanduser('~/nse-scanner')
DB_PATH  = os.path.join(BASE_DIR, 'nse_data.db')

def calc_camarilla(high, low, close):
    rng = high - low
    return {
        'l3': close - rng * 0.55/2,
        'l4': close - rng * 1.1/2,
    }

def backtest(l3_vs_hv_max=1.0, vol_min=1.0, min_rr=5.0,
             min_upside=8.0, hv_age_min=7, hv_age_max=90, max_hold=15):

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute('''
        SELECT h.symbol, h.hv_date, h.hv_low, h.hv_high, h.hv_close,
               ROUND((h.hv_high-h.hv_low)/h.hv_low*100,1) as upside_pct
        FROM hv_summary h
        WHERE h.hv_low > 0
        AND h.hv_high > h.hv_low * 1.05
        AND (h.hv_close-h.hv_low)/(h.hv_high-h.hv_low) >= 0.5
        AND h.hv_date >= "2021-01-01"
        AND h.hv_date <= "2026-05-01"
        ORDER BY h.hv_date ASC
    ''')
    hv_setups = c.fetchall()
    trades = []

    for s in hv_setups:
        sym    = s['symbol']
        hv_date= s['hv_date']
        hv_low = s['hv_low']
        hv_high= s['hv_high']
        upside = s['upside_pct']

        if upside < min_upside:
            continue

        c.execute('''SELECT date,open,high,low,close,volume
                     FROM daily_prices
                     WHERE symbol=? AND date > ?
                     ORDER BY date ASC''', (sym, hv_date))
        prices = c.fetchall()
        if not prices:
            continue

        hv_dt = datetime.strptime(hv_date, '%Y-%m-%d').date()

        for i, row in enumerate(prices):
            curr_dt = datetime.strptime(row['date'], '%Y-%m-%d').date()
            age = (curr_dt - hv_dt).days

            if age < hv_age_min:
                continue
            if age > hv_age_max:
                break

            if i == 0:
                continue

            prev = prices[i-1]
            cam  = calc_camarilla(float(prev['high']), float(prev['low']), float(prev['close']))
            l3   = cam['l3']
            l4   = cam['l4']

            l3_vs_hv = abs(l3 - hv_low) / hv_low * 100
            if l3_vs_hv > l3_vs_hv_max:
                continue

            curr_low   = float(row['low'])
            curr_close = float(row['close'])
            curr_open  = float(row['open'])
            curr_vol   = float(row['volume'])

            if curr_low > l3 * 1.005:
                continue
            if curr_close <= curr_open:
                continue

            c.execute('''SELECT AVG(volume) FROM (
                         SELECT volume FROM daily_prices
                         WHERE symbol=? AND date < ?
                         ORDER BY date DESC LIMIT 20)''',
                      (sym, row['date']))
            avg_vol   = c.fetchone()[0] or 1
            vol_ratio = curr_vol / avg_vol
            if vol_ratio < vol_min:
                continue

            entry  = l3
            sl     = l4
            target = hv_high
            risk   = entry - sl
            reward = target - entry
            if risk <= 0:
                continue
            rr = round(reward/risk, 1)
            if rr < min_rr:
                continue

            result     = 'OPEN'
            exit_price = curr_close
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

            if result == 'OPEN':
                result = 'TIMEOUT'
                exit_price = float(prices[min(i+max_hold, len(prices)-1)]['close'])

            pnl_pct = round((exit_price - entry) / entry * 100, 2)
            trades.append({
                'result'    : result,
                'pnl_pct'   : pnl_pct,
                'rr'        : rr,
                'days_held' : days_held,
                'entry_date': row['date'],
            })
            break

    conn.close()

    if not trades:
        return None

    winners = [t for t in trades if t['result'] == 'TARGET']
    losers  = [t for t in trades if t['result'] == 'SL']

    win_rate = round(len(winners)/len(trades)*100, 1)
    avg_win  = round(sum(t['pnl_pct'] for t in winners)/len(winners), 2) if winners else 0
    avg_loss = round(sum(t['pnl_pct'] for t in losers)/len(losers), 2) if losers else 0
    ev       = round(sum(t['pnl_pct'] for t in trades)/len(trades), 2)
    avg_rr   = round(sum(t['rr'] for t in trades)/len(trades), 1)
    per_month= round(len(trades)/60, 1)

    return {
        'trades'   : len(trades),
        'per_month': per_month,
        'win_rate' : win_rate,
        'avg_win'  : avg_win,
        'avg_loss' : avg_loss,
        'ev'       : ev,
        'avg_rr'   : avg_rr,
    }

if __name__ == '__main__':
    print("Running threshold comparison... ~3 minutes")
    print()

    configs = [
        # (l3_vs_hv, vol_min, min_rr, label)
        (1.0, 1.0, 5.0, "Current (L3vsHV=1%, Vol=1x, RR=5)"),
        (2.0, 1.0, 5.0, "L3vsHV=2%, Vol=1x, RR=5"),
        (3.0, 1.0, 5.0, "L3vsHV=3%, Vol=1x, RR=5"),
        (2.0, 0.7, 5.0, "L3vsHV=2%, Vol=0.7x, RR=5"),
        (2.0, 0.7, 3.0, "L3vsHV=2%, Vol=0.7x, RR=3"),
        (3.0, 0.7, 3.0, "L3vsHV=3%, Vol=0.7x, RR=3"),
        (5.0, 0.5, 3.0, "L3vsHV=5%, Vol=0.5x, RR=3"),
    ]

    results = []
    for l3, vol, rr, label in configs:
        print(f"Testing: {label}")
        r = backtest(l3_vs_hv_max=l3, vol_min=vol, min_rr=rr)
        if r:
            results.append((label, r))
            print(f"  Trades: {r['trades']} | Per month: {r['per_month']} | "
                  f"WR: {r['win_rate']}% | EV: {r['ev']}%")

    print(f"\n{'='*80}")
    print(f"  FULL COMPARISON")
    print(f"{'='*80}")
    print(f"{'Config':<42} {'Trades':>6} {'/Month':>6} {'WR%':>5} "
          f"{'AvgWin':>7} {'AvgLoss':>8} {'EV%':>6} {'RR':>5}")
    print(f"{'─'*80}")
    for label, r in results:
        print(f"{label:<42} {r['trades']:>6} {r['per_month']:>6} "
              f"{r['win_rate']:>4}% {r['avg_win']:>6}% "
              f"{r['avg_loss']:>7}% {r['ev']:>5}% {r['avg_rr']:>5}")
