#!/usr/bin/env python3
"""
Backtest: Weekly L3 aligned with HV Low
Entry  : Weekly L3 (when aligned with HV Low)
SL     : Weekly L4
Target1: Weekly H3
Target2: Weekly H4
Target3: HV High
"""
import sqlite3
from datetime import timedelta, datetime
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

def backtest(l3_vs_hv_max=2.0, vol_min=0.7, min_upside=8.0,
             hv_age_min=7, hv_age_max=90, max_hold=15):

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
    
    trades_t1 = []  # Target = Weekly H3
    trades_t2 = []  # Target = Weekly H4
    trades_t3 = []  # Target = HV High

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

            cam = calc_camarilla(float(wr['wh']), float(wr['wl']), float(wc['close']))
            l3 = cam['l3']
            l4 = cam['l4']
            h3 = cam['h3']
            h4 = cam['h4']

            # L3 alignment with HV Low
            l3_vs_hv = abs(l3 - hv_low) / hv_low * 100
            if l3_vs_hv > l3_vs_hv_max:
                continue

            # Price touches L3
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

            entry = l3
            sl    = l4
            risk  = entry - sl
            if risk <= 0:
                continue

            # Simulate all 3 targets
            for target, trade_list in [(h3, trades_t1),
                                        (h4, trades_t2),
                                        (hv_high, trades_t3)]:
                reward = target - entry
                rr     = round(reward/risk, 1)

                result     = 'TIMEOUT'
                exit_price = float(prices[min(i+max_hold, len(prices)-1)]['close'])
                days_held  = 0

                for future in prices[i+1:i+max_hold+1]:
                    days_held += 1
                    if float(future['low']) <= l4:
                        result = 'SL'
                        exit_price = l4
                        break
                    elif float(future['high']) >= target:
                        result = 'TARGET'
                        exit_price = target
                        break

                pnl_pct = round((exit_price - entry) / entry * 100, 2)
                trade_list.append({
                    'result'    : result,
                    'pnl_pct'   : pnl_pct,
                    'rr'        : rr,
                    'days_held' : days_held,
                    'entry_date': row['date'],
                })
            break

    conn.close()

    print(f"\n{'='*60}")
    print(f"  Weekly L3 + HV Low Alignment Backtest")
    print(f"  L3vsHV<={l3_vs_hv_max}% | Vol>={vol_min}x | Upside>={min_upside}%")
    print(f"{'='*60}")

    for label, trades in [
        ("Target = Weekly H3 (mean reversion)", trades_t1),
        ("Target = Weekly H4 (breakout)", trades_t2),
        ("Target = HV High (full move)", trades_t3),
    ]:
        if not trades:
            continue
        winners = [t for t in trades if t['result'] == 'TARGET']
        losers  = [t for t in trades if t['result'] == 'SL']
        timeout = [t for t in trades if t['result'] == 'TIMEOUT']

        win_rate  = round(len(winners)/len(trades)*100, 1)
        avg_win   = round(sum(t['pnl_pct'] for t in winners)/len(winners), 2) if winners else 0
        avg_loss  = round(sum(t['pnl_pct'] for t in losers)/len(losers), 2) if losers else 0
        ev        = round(sum(t['pnl_pct'] for t in trades)/len(trades), 2)
        avg_rr    = round(sum(t['rr'] for t in trades)/len(trades), 1)
        per_month = round(len(trades)/60, 1)

        print(f"\n{label}")
        print(f"{'─'*50}")
        print(f"Trades/month : {per_month}")
        print(f"Win rate     : {win_rate}%")
        print(f"Avg win      : +{avg_win}%")
        print(f"Avg loss     : {avg_loss}%")
        print(f"EV/trade     : {ev}%")
        print(f"Avg R:R      : 1:{avg_rr}")
        print(f"Targets      : {len(winners)}")
        print(f"SL hits      : {len(losers)}")
        print(f"Timeouts     : {len(timeout)}")

        print(f"\nYear by Year:")
        for year in range(2021, 2027):
            yt = [t for t in trades if t['entry_date'].startswith(str(year))]
            if not yt: continue
            yw = [t for t in yt if t['result'] == 'TARGET']
            yp = round(sum(t['pnl_pct'] for t in yt), 2)
            print(f"  {year}: {len(yt):>3} trades | "
                  f"WR:{round(len(yw)/len(yt)*100,1):>5}% | "
                  f"EV:{round(yp/len(yt),2):>6}%/trade")

if __name__ == '__main__':
    print("Running Weekly L3 + HV backtest...")
    # Test different thresholds
    for l3_max in [1.0, 2.0, 3.0]:
        backtest(l3_vs_hv_max=l3_max, vol_min=0.7)
