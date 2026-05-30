#!/usr/bin/env python3
"""
Backtest: Last 2 months (Apr-May 2026)
Hybrid Strategy: W-L3 entry | Daily L4 SL | W-H3 T1 | HV High T2
"""
import sqlite3
from datetime import date, timedelta, datetime
import os

BASE_DIR = os.path.expanduser('~/nse-scanner')
DB_PATH  = os.path.join(BASE_DIR, 'nse_data.db')

def get_week_start(d):
    return d - timedelta(days=d.weekday())

def backtest_recent(start_date='2026-03-26'):
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
        ORDER BY h.hv_date ASC
    ''')
    setups = c.fetchall()
    trades = []

    today = date.today()

    for s in setups:
        sym     = s['symbol']
        hv_date = s['hv_date']
        hv_low  = float(s['hv_low'])
        hv_high = float(s['hv_high'])
        upside  = float(s['upside_pct'])

        if upside < 8.0:
            continue

        c.execute('''SELECT date,open,high,low,close,volume
                     FROM daily_prices WHERE symbol=? AND date >= ?
                     ORDER BY date ASC''', (sym, start_date))
        prices = c.fetchall()
        if not prices:
            continue

        hv_dt = datetime.strptime(hv_date, '%Y-%m-%d').date()

        for i, row in enumerate(prices):
            curr_dt = datetime.strptime(row['date'], '%Y-%m-%d').date()
            age = (curr_dt - hv_dt).days

            if age < 7: continue
            if age > 150: break
            if i == 0: continue

            # Weekly Camarilla
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

            # Daily L4
            prev  = prices[i-1]
            d_rng = float(prev['high']) - float(prev['low'])
            d_l4  = round(float(prev['close']) - d_rng * 1.1/2, 2)

            # Filters
            l3_vs_hv = abs(wl3 - hv_low) / hv_low * 100
            if l3_vs_hv > 5.0: continue

            curr_low   = float(row['low'])
            curr_close = float(row['close'])
            curr_open  = float(row['open'])

            if curr_low > wl3 * 1.005: continue
            if curr_close <= curr_open: continue

            pct_above = (curr_close - hv_low) / hv_low * 100
            if pct_above > 5.0: continue

            if d_l4 >= wl3: continue
            if hv_high <= wl3: continue

            risk = wl3 - d_l4
            if risk <= 0: continue
            rr_t2 = round((hv_high - wl3) / risk, 1)
            if rr_t2 < 3.0: continue

            # Simulate
            position = 20000
            shares   = int(position / wl3)
            if shares < 1: continue
            l1_sh = shares // 2
            l2_sh = shares - l1_sh

            p1_result = 'OPEN'
            p2_result = 'OPEN'
            p1_exit   = curr_close
            p2_exit   = curr_close
            days_held = 0

            for future in prices[i+1:i+31]:
                days_held += 1
                fh = float(future['high'])
                fl = float(future['low'])

                if fl <= d_l4:
                    if p1_result == 'OPEN':
                        p1_result = 'SL'; p1_exit = d_l4
                    if p2_result == 'OPEN':
                        p2_result = 'SL'; p2_exit = d_l4
                    break

                if p1_result == 'OPEN' and fh >= wh3:
                    p1_result = 'T1'; p1_exit = wh3

                if p2_result == 'OPEN' and fh >= hv_high:
                    p2_result = 'T2'; p2_exit = hv_high
                    break

            # Still open = use latest price
            if p1_result == 'OPEN':
                latest = prices[min(i+days_held, len(prices)-1)]
                p1_exit = float(latest['close'])
                p1_result = 'STILL_OPEN'
            if p2_result == 'OPEN':
                latest = prices[min(i+days_held, len(prices)-1)]
                p2_exit = float(latest['close'])
                p2_result = 'STILL_OPEN'

            p1_pnl = round((p1_exit - wl3) / wl3 * 100 * position * 0.5 / 100, 0)
            p2_pnl = round((p2_exit - wl3) / wl3 * 100 * position * 0.5 / 100, 0)
            total  = p1_pnl + p2_pnl

            if p1_result == 'SL':
                overall = 'FULL_LOSS'
            elif p1_result == 'T1' and p2_result == 'T2':
                overall = 'FULL_WIN'
            elif p1_result == 'T1':
                overall = 'T1_ONLY'
            elif p1_result == 'STILL_OPEN':
                overall = 'STILL_OPEN'
            else:
                overall = 'OTHER'

            trades.append({
                'symbol'   : sym,
                'date'     : row['date'],
                'entry'    : round(wl3, 2),
                'sl'       : round(d_l4, 2),
                'wh3'      : round(wh3, 2),
                'hv_high'  : round(hv_high, 2),
                'p1_result': p1_result,
                'p2_result': p2_result,
                'overall'  : overall,
                'p1_pnl'   : p1_pnl,
                'p2_pnl'   : p2_pnl,
                'total_pnl': total,
                'days'     : days_held,
                'sl_pct'   : round(risk/wl3*100, 2),
            })
            break

    conn.close()
    return trades

if __name__ == '__main__':
    print("Last 2 months backtest (Apr-May 2026)")
    print("Hybrid: W-L3 | Daily L4 SL | W-H3 T1 | HV High T2")
    print("="*60)

    trades = backtest_recent('2026-03-26')

    if not trades:
        print("No trades found")
    else:
        print(f"\nTotal trades: {len(trades)}")
        print()
        print(f"{'Date':<12} {'Symbol':<12} {'Entry':>8} {'SL':>8} "
              f"{'T1':>8} {'T2':>8} {'P1':>8} {'P2':>8} "
              f"{'Total':>8} {'Result'}")
        print('-'*100)

        for t in trades:
            print(f"{t['date']:<12} {t['symbol']:<12} "
                  f"Rs{t['entry']:>7.1f} Rs{t['sl']:>7.1f} "
                  f"Rs{t['wh3']:>7.1f} Rs{t['hv_high']:>7.1f} "
                  f"Rs{t['p1_pnl']:>+7.0f} Rs{t['p2_pnl']:>+7.0f} "
                  f"Rs{t['total_pnl']:>+7.0f} {t['overall']}")

        print()
        total_pnl = sum(t['total_pnl'] for t in trades)
        winners   = [t for t in trades if t['total_pnl'] > 0]
        losers    = [t for t in trades if t['total_pnl'] < 0]
        open_pos  = [t for t in trades if t['overall'] == 'STILL_OPEN']
        full_wins = [t for t in trades if t['overall'] == 'FULL_WIN']
        t1_only   = [t for t in trades if t['overall'] == 'T1_ONLY']
        full_loss = [t for t in trades if t['overall'] == 'FULL_LOSS']

        print(f"{'='*60}")
        print(f"SUMMARY:")
        print(f"  Total trades  : {len(trades)}")
        print(f"  Full wins     : {len(full_wins)} (T1+T2 both)")
        print(f"  T1 only       : {len(t1_only)} (H3 hit)")
        print(f"  Full losses   : {len(full_loss)} (SL hit)")
        print(f"  Still open    : {len(open_pos)}")
        print(f"  Win rate      : {round(len(winners)/len(trades)*100,1)}%")
        print(f"  Total P&L     : Rs {total_pnl:+,.0f}")
        print(f"  Per trade avg : Rs {round(total_pnl/len(trades)):+,}")
        print()
        if open_pos:
            print(f"STILL OPEN POSITIONS:")
            for t in open_pos:
                print(f"  {t['symbol']}: Entry Rs {t['entry']} | "
                      f"Current P&L Rs {t['total_pnl']:+,.0f}")
