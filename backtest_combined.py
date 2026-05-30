#!/usr/bin/env python3
"""
Backtest: Combined Strategy
50% position exits at Weekly H3
50% position exits at HV High
Entry: Daily L3 aligned with HV Low
SL   : Daily L4
"""
import sqlite3
from datetime import timedelta, datetime
import os

BASE_DIR = os.path.expanduser('~/nse-scanner')
DB_PATH  = os.path.join(BASE_DIR, 'nse_data.db')

def get_week_start(d):
    return d - timedelta(days=d.weekday())

def calc_cam_daily(high, low, close):
    rng = high - low
    return {
        'l3': close - rng * 0.55/2,
        'l4': close - rng * 1.1/2,
    }

def calc_cam_weekly(high, low, close):
    rng = high - low
    return {
        'h3': close + rng * 0.55/2,
        'h4': close + rng * 1.1/2,
        'l3': close - rng * 0.55/2,
        'l4': close - rng * 1.1/2,
    }

def backtest(l3_vs_hv_max=2.0, vol_min=1.0, min_rr=5.0,
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
        sym     = s['symbol']
        hv_date = s['hv_date']
        hv_low  = s['hv_low']
        hv_high = s['hv_high']
        upside  = s['upside_pct']

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

            # Daily Camarilla
            prev = prices[i-1]
            dcam = calc_cam_daily(
                float(prev['high']),
                float(prev['low']),
                float(prev['close'])
            )
            l3 = dcam['l3']
            l4 = dcam['l4']

            # L3 vs HV Low check
            l3_vs_hv = abs(l3 - hv_low) / hv_low * 100
            if l3_vs_hv > l3_vs_hv_max:
                continue

            # Price action checks
            curr_low   = float(row['low'])
            curr_close = float(row['close'])
            curr_open  = float(row['open'])
            curr_vol   = float(row['volume'])

            if curr_low > l3 * 1.005:
                continue
            if curr_close <= curr_open:
                continue

            # Volume check
            c.execute('''SELECT AVG(volume) FROM (
                         SELECT volume FROM daily_prices
                         WHERE symbol=? AND date < ?
                         ORDER BY date DESC LIMIT 20)''',
                      (sym, row['date']))
            avg_vol   = c.fetchone()[0] or 1
            vol_ratio = curr_vol / avg_vol
            if vol_ratio < vol_min:
                continue

            # Weekly Camarilla for H3 target
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

            wcam   = calc_cam_weekly(float(wr['wh']), float(wr['wl']), float(wc['close']))
            w_h3   = wcam['h3']
            w_h4   = wcam['h4']

            entry  = l3
            sl     = l4
            risk   = entry - sl
            if risk <= 0:
                continue

            # R:R check based on HV High
            rr = round((hv_high - entry) / risk, 1)
            if rr < min_rr:
                continue

            # Simulate PART 1: exit at Weekly H3
            p1_result = 'TIMEOUT'
            p1_exit   = curr_close
            p1_days   = 0

            for future in prices[i+1:i+max_hold+1]:
                p1_days += 1
                if float(future['low']) <= l4:
                    p1_result = 'SL'
                    p1_exit   = l4
                    break
                elif float(future['high']) >= w_h3:
                    p1_result = 'TARGET'
                    p1_exit   = w_h3
                    break

            if p1_result == 'TIMEOUT':
                p1_exit = float(prices[min(i+max_hold, len(prices)-1)]['close'])

            # Simulate PART 2: exit at HV High
            p2_result = 'TIMEOUT'
            p2_exit   = curr_close
            p2_days   = 0

            for future in prices[i+1:i+max_hold+1]:
                p2_days += 1
                if float(future['low']) <= l4:
                    p2_result = 'SL'
                    p2_exit   = l4
                    break
                elif float(future['high']) >= hv_high:
                    p2_result = 'TARGET'
                    p2_exit   = hv_high
                    break

            if p2_result == 'TIMEOUT':
                p2_exit = float(prices[min(i+max_hold, len(prices)-1)]['close'])

            # Combined P&L (50/50 split)
            p1_pnl = (p1_exit - entry) / entry * 100
            p2_pnl = (p2_exit - entry) / entry * 100
            combined_pnl = round((p1_pnl + p2_pnl) / 2, 2)

            # Overall result
            if p1_result == 'SL' and p2_result == 'SL':
                overall = 'FULL_LOSS'
            elif p1_result == 'TARGET' and p2_result == 'TARGET':
                overall = 'FULL_WIN'
            elif p1_result == 'TARGET':
                overall = 'PARTIAL_WIN'
            else:
                overall = 'OTHER'

            trades.append({
                'symbol'    : sym,
                'entry_date': row['date'],
                'entry'     : round(entry, 2),
                'sl'        : round(sl, 2),
                'w_h3'      : round(w_h3, 2),
                'hv_high'   : round(hv_high, 2),
                'p1_result' : p1_result,
                'p2_result' : p2_result,
                'p1_pnl'    : round(p1_pnl, 2),
                'p2_pnl'    : round(p2_pnl, 2),
                'combined_pnl': combined_pnl,
                'overall'   : overall,
                'rr'        : rr,
            })
            break

    conn.close()

    if not trades:
        print("No trades found")
        return

    full_wins    = [t for t in trades if t['overall'] == 'FULL_WIN']
    partial_wins = [t for t in trades if t['overall'] == 'PARTIAL_WIN']
    full_losses  = [t for t in trades if t['overall'] == 'FULL_LOSS']
    other        = [t for t in trades if t['overall'] == 'OTHER']

    profitable = [t for t in trades if t['combined_pnl'] > 0]
    loss_trades= [t for t in trades if t['combined_pnl'] <= 0]

    win_rate  = round(len(profitable)/len(trades)*100, 1)
    avg_win   = round(sum(t['combined_pnl'] for t in profitable)/len(profitable), 2) if profitable else 0
    avg_loss  = round(sum(t['combined_pnl'] for t in loss_trades)/len(loss_trades), 2) if loss_trades else 0
    ev        = round(sum(t['combined_pnl'] for t in trades)/len(trades), 2)
    per_month = round(len(trades)/60, 1)

    print(f"\n{'='*60}")
    print(f"  COMBINED STRATEGY (50% → W-H3, 50% → HV High)")
    print(f"  L3vsHV<={l3_vs_hv_max}% | Vol>={vol_min}x | MinRR={min_rr}")
    print(f"{'='*60}")
    print(f"Total trades  : {len(trades)}")
    print(f"Per month     : {per_month}")
    print(f"")
    print(f"Full wins     : {len(full_wins)} (both targets hit)")
    print(f"Partial wins  : {len(partial_wins)} (H3 hit, HV High not)")
    print(f"Full losses   : {len(full_losses)} (both SL hit)")
    print(f"Other         : {len(other)}")
    print(f"")
    print(f"Profitable    : {len(profitable)} ({win_rate}%)")
    print(f"Avg win       : +{avg_win}%")
    print(f"Avg loss      : {avg_loss}%")
    print(f"EV/trade      : {ev}%")
    print(f"")

    # Compare with individual strategies
    daily_ev  = 3.20  # from previous backtest
    weekly_ev = 2.60  # from previous backtest
    print(f"Comparison:")
    print(f"  Daily only (L3→HV High) : EV {daily_ev}%")
    print(f"  Weekly only (L3→H3)     : EV {weekly_ev}%")
    print(f"  Combined (50/50)        : EV {ev}%")

    print(f"\nYear by Year:")
    for year in range(2021, 2027):
        yt = [t for t in trades if t['entry_date'].startswith(str(year))]
        if not yt:
            continue
        yp = [t for t in yt if t['combined_pnl'] > 0]
        ytotal = round(sum(t['combined_pnl'] for t in yt), 2)
        print(f"  {year}: {len(yt):>3} trades | "
              f"WR:{round(len(yp)/len(yt)*100,1):>5}% | "
              f"EV:{round(ytotal/len(yt),2):>6}%/trade")

if __name__ == '__main__':
    print("Running Combined Strategy Backtest...")
    
    # Test different thresholds
    for l3_max, vol in [(1.0, 1.0), (2.0, 1.0), (2.0, 0.7)]:
        backtest(l3_vs_hv_max=l3_max, vol_min=vol)
