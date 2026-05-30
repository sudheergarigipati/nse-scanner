#!/usr/bin/env python3
"""
Backtest: Weekly Camarilla Options Strategy
BankNifty + Nifty50
Period: 2021-2026
Logic:
  Entry: Price touches weekly L3 (buy CE) or H3 (buy PE)
  Confirm: Green/Red 15min candle
  SL: L4 broken (for CE) or H4 broken (for PE)
  Target: Opposite side (H3 for CE, L3 for PE)
  Time: 9:30 AM - 1:30 PM only
  Skip: Thursday (expiry day)
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

def backtest(symbol, start_year=2021):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Get all daily data
    c.execute('''SELECT date, open, high, low, close, volume
                 FROM daily_prices
                 WHERE symbol=? AND date >= ?
                 ORDER BY date ASC''',
              (symbol, f'{start_year}-01-01'))
    rows = c.fetchall()
    conn.close()

    if not rows:
        print(f'{symbol}: No data found')
        return

    # Group by week
    weeks = {}
    for r in rows:
        d = datetime.strptime(r['date'], '%Y-%m-%d').date()
        ws = get_week_start(d).strftime('%Y-%m-%d')
        if ws not in weeks:
            weeks[ws] = []
        weeks[ws].append(r)

    print(f"\n{'='*60}")
    print(f"  {symbol} — Weekly Camarilla Backtest")
    print(f"  Period: {start_year}-2026")
    print(f"{'='*60}")

    trades      = []
    week_list   = sorted(weeks.keys())
    total_weeks = len(week_list)

    for i, ws in enumerate(week_list):
        if i == 0:
            continue  # need previous week

        # Get previous week OHLC
        prev_ws   = week_list[i-1]
        prev_days = weeks[prev_ws]
        if not prev_days:
            continue

        prev_high  = max(float(d['high'])  for d in prev_days)
        prev_low   = min(float(d['low'])   for d in prev_days)
        prev_close = float(prev_days[-1]['close'])

        cam = calc_camarilla(prev_high, prev_low, prev_close)
        l3, l4, h3, h4 = cam['l3'], cam['l4'], cam['h3'], cam['h4']

        # This week's days
        this_days = weeks[ws]

        for day in this_days:
            d = datetime.strptime(day['date'], '%Y-%m-%d').date()

            # Skip Thursday (expiry day)
            if d.weekday() == 3:
                continue

            high  = float(day['high'])
            low   = float(day['low'])
            close = float(day['close'])
            open_ = float(day['open'])

            # CE signal: Low touched L3, closed above L3, green candle
            if (low <= l3 * 1.005 and
                close > l3 and
                close > open_):

                entry  = l3
                sl     = l4
                target = h3
                risk   = entry - sl
                reward = target - entry

                if risk <= 0:
                    continue

                rr = round(reward / risk, 1)

                # Simulate: did it hit target or SL?
                # Check remaining days this week
                day_idx = this_days.index(day)
                result  = 'OPEN'
                exit_price = close
                days_held  = 0

                for future_day in this_days[day_idx+1:]:
                    days_held += 1
                    fh = float(future_day['high'])
                    fl = float(future_day['low'])
                    fc = float(future_day['close'])

                    if fl <= l4:
                        result     = 'SL'
                        exit_price = l4
                        break
                    elif fh >= h3:
                        result     = 'TARGET'
                        exit_price = h3
                        break

                if result == 'OPEN':
                    # Exit at week end
                    exit_price = float(this_days[-1]['close'])
                    result = 'EXPIRED'

                pnl_pts = exit_price - entry
                pnl_pct = round(pnl_pts / entry * 100, 2)

                trades.append({
                    'date'      : day['date'],
                    'week'      : ws,
                    'type'      : 'CE',
                    'entry'     : round(entry, 2),
                    'sl'        : round(sl, 2),
                    'target'    : round(target, 2),
                    'exit'      : round(exit_price, 2),
                    'result'    : result,
                    'pnl_pts'   : round(pnl_pts, 2),
                    'pnl_pct'   : pnl_pct,
                    'rr'        : rr,
                    'days_held' : days_held,
                })

            # PE signal: High touched H3, closed below H3, red candle
            if (high >= h3 * 0.995 and
                close < h3 and
                close < open_):

                entry  = h3
                sl     = h4
                target = l3
                risk   = sl - entry
                reward = entry - target

                if risk <= 0:
                    continue

                rr = round(reward / risk, 1)

                day_idx = this_days.index(day)
                result  = 'OPEN'
                exit_price = close
                days_held  = 0

                for future_day in this_days[day_idx+1:]:
                    days_held += 1
                    fh = float(future_day['high'])
                    fl = float(future_day['low'])
                    fc = float(future_day['close'])

                    if fh >= h4:
                        result     = 'SL'
                        exit_price = h4
                        break
                    elif fl <= l3:
                        result     = 'TARGET'
                        exit_price = l3
                        break

                if result == 'OPEN':
                    exit_price = float(this_days[-1]['close'])
                    result = 'EXPIRED'

                pnl_pts = entry - exit_price
                pnl_pct = round(pnl_pts / entry * 100, 2)

                trades.append({
                    'date'      : day['date'],
                    'week'      : ws,
                    'type'      : 'PE',
                    'entry'     : round(entry, 2),
                    'sl'        : round(sl, 2),
                    'target'    : round(target, 2),
                    'exit'      : round(exit_price, 2),
                    'result'    : result,
                    'pnl_pts'   : round(pnl_pts, 2),
                    'pnl_pct'   : pnl_pct,
                    'rr'        : rr,
                    'days_held' : days_held,
                })

    # ── Results ───────────────────────────────────────────────
    if not trades:
        print("No trades found")
        return

    winners  = [t for t in trades if t['result'] == 'TARGET']
    losers   = [t for t in trades if t['result'] == 'SL']
    expired  = [t for t in trades if t['result'] == 'EXPIRED']
    ce_trades = [t for t in trades if t['type'] == 'CE']
    pe_trades = [t for t in trades if t['type'] == 'PE']

    win_rate  = round(len(winners)/len(trades)*100, 1)
    avg_win   = round(sum(t['pnl_pts'] for t in winners)/len(winners), 1) if winners else 0
    avg_loss  = round(sum(t['pnl_pts'] for t in losers)/len(losers), 1) if losers else 0
    avg_exp   = round(sum(t['pnl_pts'] for t in expired)/len(expired), 1) if expired else 0
    avg_rr    = round(sum(t['rr'] for t in trades)/len(trades), 1)
    avg_hold  = round(sum(t['days_held'] for t in trades)/len(trades), 1)
    total_pts = round(sum(t['pnl_pts'] for t in trades), 1)
    ev        = round(total_pts/len(trades), 1)

    print(f"\nTotal trades : {len(trades)}")
    print(f"CE trades    : {len(ce_trades)}")
    print(f"PE trades    : {len(pe_trades)}")
    print(f"\nWin rate     : {win_rate}%")
    print(f"Targets hit  : {len(winners)}")
    print(f"SL hit       : {len(losers)}")
    print(f"Expired      : {len(expired)}")
    print(f"\nAvg win (pts): +{avg_win}")
    print(f"Avg loss(pts): {avg_loss}")
    print(f"Avg expired  : {avg_exp}")
    print(f"Avg R:R      : 1:{avg_rr}")
    print(f"Avg hold     : {avg_hold} days")
    print(f"\nTotal P&L pts: {total_pts}")
    print(f"EV/trade     : {ev} pts")

    # Year by year
    print(f"\n{'─'*40}")
    print(f"Year by Year:")
    print(f"{'─'*40}")
    for year in range(2021, 2027):
        yr_trades = [t for t in trades if t['date'].startswith(str(year))]
        if not yr_trades:
            continue
        yr_winners = [t for t in yr_trades if t['result'] == 'TARGET']
        yr_pts = round(sum(t['pnl_pts'] for t in yr_trades), 1)
        yr_wr  = round(len(yr_winners)/len(yr_trades)*100, 1)
        print(f"{year}: {len(yr_trades):>3} trades | "
              f"WR: {yr_wr:>5}% | "
              f"Total: {yr_pts:>8} pts | "
              f"EV: {round(yr_pts/len(yr_trades),1):>6} pts/trade")

    # Save results
    result_file = os.path.join(BASE_DIR, f'backtest_{symbol}_options.json')
    with open(result_file, 'w') as f:
        json.dump({
            'symbol'    : symbol,
            'trades'    : trades,
            'summary'   : {
                'total'   : len(trades),
                'win_rate': win_rate,
                'avg_win' : avg_win,
                'avg_loss': avg_loss,
                'avg_rr'  : avg_rr,
                'total_pts': total_pts,
                'ev'      : ev,
            }
        }, f, indent=2)
    print(f"\nDetailed results saved to: backtest_{symbol}_options.json")

if __name__ == '__main__':
    backtest('BANKNIFTY')
    backtest('NIFTY50')
