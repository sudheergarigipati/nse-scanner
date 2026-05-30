#!/usr/bin/env python3
"""
Backtest: HV + WEEKLY Camarilla for Stocks
Compare with Daily Camarilla results
Period: 2021-2026, 794 stocks
"""
import sqlite3
import json
from datetime import date, timedelta, datetime
import os

BASE_DIR = os.path.expanduser('~/nse-scanner')
DB_PATH  = os.path.join(BASE_DIR, 'nse_data.db')

def get_week_start(d):
    return d - timedelta(days=d.weekday())

def calc_camarilla(high, low, close):
    rng = high - low
    return {
        'h4': close + rng * 1.1/2,
        'h3': close + rng * 0.55/2,
        'l3': close - rng * 0.55/2,
        'l4': close - rng * 1.1/2,
    }

def backtest(use_weekly=True, 
             l3_vs_hv_max=1.0,
             vol_min=1.0,
             min_rr=5.0,
             min_upside=8.0,
             hv_age_min=7,
             hv_age_max=90,
             max_hold=15):

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Get all HV setups
    c.execute('''
        SELECT h.symbol, h.hv_date, h.hv_low, h.hv_high, h.hv_close,
               ROUND((h.hv_close-h.hv_low)/(h.hv_high-h.hv_low)*100,1) as candle_pos,
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
    skipped_rr = 0
    skipped_upside = 0
    skipped_l3 = 0
    skipped_vol = 0
    skipped_touch = 0
    skipped_candle = 0

    for s in hv_setups:
        sym      = s['symbol']
        hv_date  = s['hv_date']
        hv_low   = s['hv_low']
        hv_high  = s['hv_high']
        upside   = s['upside_pct']

        # Check upside
        if upside < min_upside:
            skipped_upside += 1
            continue

        # Get all daily prices after HV day
        c.execute('''SELECT date, open, high, low, close, volume
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

            # Age filter
            if age < hv_age_min:
                continue
            if age > hv_age_max:
                break

            if use_weekly:
                # Get previous week OHLC
                week_start = get_week_start(curr_dt)
                prev_week_end   = week_start - timedelta(days=1)
                prev_week_start = week_start - timedelta(days=7)

                c.execute('''SELECT MAX(high) as wh, MIN(low) as wl FROM daily_prices
                             WHERE symbol=? AND date>=? AND date<=?''',
                          (sym, prev_week_start.strftime('%Y-%m-%d'),
                           prev_week_end.strftime('%Y-%m-%d')))
                wr = c.fetchone()
                c.execute('''SELECT close FROM daily_prices WHERE symbol=? AND date<=?
                             ORDER BY date DESC LIMIT 1''',
                          (sym, prev_week_end.strftime('%Y-%m-%d')))
                wc = c.fetchone()

                if not wr or not wr['wh'] or not wc:
                    continue

                cam = calc_camarilla(float(wr['wh']), float(wr['wl']), float(wc['close']))
            else:
                # Daily Camarilla from previous day
                if i == 0:
                    continue
                prev = prices[i-1]
                cam = calc_camarilla(
                    float(prev['high']),
                    float(prev['low']),
                    float(prev['close'])
                )

            l3 = cam['l3']
            l4 = cam['l4']

            # L3 vs HV Low check
            l3_vs_hv = abs(l3 - hv_low) / hv_low * 100
            if l3_vs_hv > l3_vs_hv_max:
                skipped_l3 += 1
                continue

            # Price action
            curr_low   = float(row['low'])
            curr_close = float(row['close'])
            curr_open  = float(row['open'])
            curr_vol   = float(row['volume'])

            # Touch L3
            if curr_low > l3 * 1.005:
                skipped_touch += 1
                continue

            # Green candle
            if curr_close <= curr_open:
                skipped_candle += 1
                continue

            # Volume check
            c.execute('''SELECT AVG(volume) FROM (
                         SELECT volume FROM daily_prices
                         WHERE symbol=? AND date < ?
                         ORDER BY date DESC LIMIT 20)''',
                      (sym, row['date']))
            avg_vol = c.fetchone()[0] or 1
            vol_ratio = curr_vol / avg_vol

            if vol_ratio < vol_min:
                skipped_vol += 1
                continue

            # R:R check
            entry  = l3
            sl     = l4
            target = hv_high
            risk   = entry - sl
            reward = target - entry

            if risk <= 0:
                continue

            rr = round(reward / risk, 1)
            if rr < min_rr:
                skipped_rr += 1
                continue

            # Simulate trade
            entry_date = row['date']
            result     = 'OPEN'
            exit_price = curr_close
            days_held  = 0

            # Check future prices
            for j, future in enumerate(prices[i+1:i+max_hold+1]):
                days_held += 1
                fh = float(future['high'])
                fl = float(future['low'])

                if fl <= l4:
                    result = 'SL'
                    exit_price = l4
                    break
                elif fh >= hv_high:
                    result = 'TARGET'
                    exit_price = hv_high
                    break

            if result == 'OPEN':
                result = 'TIMEOUT'
                exit_price = float(prices[min(i+max_hold, len(prices)-1)]['close'])

            pnl_pct = round((exit_price - entry) / entry * 100, 2)

            trades.append({
                'symbol'    : sym,
                'hv_date'   : hv_date,
                'entry_date': entry_date,
                'entry'     : round(entry, 2),
                'sl'        : round(sl, 2),
                'target'    : round(target, 2),
                'exit'      : round(exit_price, 2),
                'result'    : result,
                'pnl_pct'   : pnl_pct,
                'rr'        : rr,
                'days_held' : days_held,
                'l3_vs_hv'  : round(l3_vs_hv, 2),
                'vol_ratio' : round(vol_ratio, 1),
            })
            break  # One trade per HV setup per day

    conn.close()

    if not trades:
        print("No trades found")
        return

    winners  = [t for t in trades if t['result'] == 'TARGET']
    losers   = [t for t in trades if t['result'] == 'SL']
    timeout  = [t for t in trades if t['result'] == 'TIMEOUT']

    win_rate  = round(len(winners)/len(trades)*100, 1)
    avg_win   = round(sum(t['pnl_pct'] for t in winners)/len(winners), 2) if winners else 0
    avg_loss  = round(sum(t['pnl_pct'] for t in losers)/len(losers), 2) if losers else 0
    avg_time  = round(sum(t['pnl_pct'] for t in timeout)/len(timeout), 2) if timeout else 0
    total_pnl = round(sum(t['pnl_pct'] for t in trades), 2)
    ev        = round(total_pnl/len(trades), 2)
    avg_hold  = round(sum(t['days_held'] for t in trades)/len(trades), 1)
    avg_rr    = round(sum(t['rr'] for t in trades)/len(trades), 1)

    label = "WEEKLY Camarilla" if use_weekly else "DAILY Camarilla"
    print(f"\n{'='*55}")
    print(f"  {label} — HV Stock Backtest")
    print(f"  Period: 2021-2026 | L3vsHV<={l3_vs_hv_max}% | MinRR={min_rr}")
    print(f"{'='*55}")
    print(f"Total trades  : {len(trades)}")
    print(f"Targets hit   : {len(winners)}")
    print(f"SL hit        : {len(losers)}")
    print(f"Timeout       : {len(timeout)}")
    print(f"")
    print(f"Win rate      : {win_rate}%")
    print(f"Avg win       : +{avg_win}%")
    print(f"Avg loss      : {avg_loss}%")
    print(f"Avg timeout   : {avg_time}%")
    print(f"Avg R:R       : 1:{avg_rr}")
    print(f"Avg hold      : {avg_hold} days")
    print(f"")
    print(f"EV/trade      : {ev}%")
    print(f"Total P&L     : {total_pnl}%")

    print(f"\nYear by Year:")
    for year in range(2021, 2027):
        yt = [t for t in trades if t['entry_date'].startswith(str(year))]
        if not yt: continue
        yw = [t for t in yt if t['result'] == 'TARGET']
        yp = round(sum(t['pnl_pct'] for t in yt), 2)
        print(f"  {year}: {len(yt):>3} trades | "
              f"WR:{round(len(yw)/len(yt)*100,1):>5}% | "
              f"EV:{round(yp/len(yt),2):>6}%/trade")

    print(f"\nSkip reasons:")
    print(f"  Upside < {min_upside}%  : {skipped_upside}")
    print(f"  L3 vs HV > {l3_vs_hv_max}%: {skipped_l3}")
    print(f"  Volume < {vol_min}x   : {skipped_vol}")
    print(f"  No L3 touch    : {skipped_touch}")
    print(f"  Bearish candle : {skipped_candle}")
    print(f"  R:R < {min_rr}       : {skipped_rr}")

    return {
        'trades': len(trades),
        'win_rate': win_rate,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'ev': ev,
        'avg_rr': avg_rr,
    }

if __name__ == '__main__':
    print("Running backtests... this may take 2-3 minutes")
    
    # Daily Camarilla (current setup)
    daily = backtest(use_weekly=False)
    
    # Weekly Camarilla
    weekly = backtest(use_weekly=True)

    # Summary comparison
    if daily and weekly:
        print(f"\n{'='*55}")
        print(f"  COMPARISON SUMMARY")
        print(f"{'='*55}")
        print(f"{'Metric':<20} {'Daily':>12} {'Weekly':>12}")
        print(f"{'─'*45}")
        print(f"{'Trades':<20} {daily['trades']:>12} {weekly['trades']:>12}")
        print(f"{'Win Rate':<20} {daily['win_rate']:>11}% {weekly['win_rate']:>11}%")
        print(f"{'Avg Win':<20} {daily['avg_win']:>11}% {weekly['avg_win']:>11}%")
        print(f"{'Avg Loss':<20} {daily['avg_loss']:>11}% {weekly['avg_loss']:>11}%")
        print(f"{'EV/trade':<20} {daily['ev']:>11}% {weekly['ev']:>11}%")
        print(f"{'Avg R:R':<20} {daily['avg_rr']:>11} {weekly['avg_rr']:>11}")
