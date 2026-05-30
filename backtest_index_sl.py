#!/usr/bin/env python3
"""
Backtest: Index Options SL Analysis
Test different SL strategies:
1. SL at H4 (for PUT) / L4 (for CALL)
2. SL at fixed % (1%, 2%, 3% of index)
3. SL at next level
4. Trail SL

Signals:
  PUT: H3 rejection, H4 rejection, H4 false breakout
  CALL: L3 bounce, L4 bounce, L4 false breakdown

Period: 2024-2026 (2 years)
"""
import sqlite3
from datetime import date, timedelta, datetime
import os

BASE_DIR = os.path.expanduser('~/nse-scanner')
DB_PATH  = os.path.join(BASE_DIR, 'nse_data.db')

def get_week_start(d):
    return d - timedelta(days=d.weekday())

def calc_camarilla(high, low, close):
    rng = high - low
    return {
        'h4': round(close + rng * 1.1/2, 2),
        'h3': round(close + rng * 0.55/2, 2),
        'l3': round(close - rng * 0.55/2, 2),
        'l4': round(close - rng * 1.1/2, 2),
    }

def backtest_sl(symbol, sl_type='level', sl_pct=1.0, target_type='h3'):
    """
    sl_type: 'level' (H4/L4), 'pct' (fixed %), 'half_range'
    target_type: 'h3' (first target), 'l3' (full target), 'both' (50/50)
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Get 2 years of daily data
    c.execute('''SELECT date, open, high, low, close
                 FROM daily_prices
                 WHERE symbol=? AND date >= "2024-01-01"
                 ORDER BY date ASC''', (symbol,))
    prices = c.fetchall()
    conn.close()

    if not prices:
        return None

    # Group by week
    weeks = {}
    for r in prices:
        d  = datetime.strptime(r['date'], '%Y-%m-%d').date()
        ws = get_week_start(d).strftime('%Y-%m-%d')
        if ws not in weeks:
            weeks[ws] = []
        weeks[ws].append(r)

    trades = []
    week_list = sorted(weeks.keys())

    for i, ws in enumerate(week_list):
        if i == 0:
            continue
        prev_days = weeks[week_list[i-1]]
        if not prev_days:
            continue

        prev_high  = max(float(d['high']) for d in prev_days)
        prev_low   = min(float(d['low'])  for d in prev_days)
        prev_close = float(prev_days[-1]['close'])
        cam = calc_camarilla(prev_high, prev_low, prev_close)
        l3, l4, h3, h4 = cam['l3'], cam['l4'], cam['h3'], cam['h4']

        this_days = weeks[ws]
        prev_close_intraday = float(this_days[0]['open'])

        for j, day in enumerate(this_days):
            # Skip Thursday (expiry)
            d = datetime.strptime(day['date'], '%Y-%m-%d').date()
            if d.weekday() == 3:
                continue

            high  = float(day['high'])
            low   = float(day['low'])
            close = float(day['close'])
            open_ = float(day['open'])

            signals = []

            # PUT signals
            # S1: H3 rejection
            if (high >= h3 * 0.998 and close < h3 and close < open_):
                signals.append(('PUT', 'H3_REJ', close, h3, h4, l3, l4))

            # S2: H4 rejection
            if (high >= h4 * 0.998 and close < h4 and close < open_):
                signals.append(('PUT', 'H4_REJ', close, h3, h4, l3, l4))

            # S4: H4 false breakout
            if (prev_close_intraday > h4 and close < h4 and close < open_):
                signals.append(('PUT', 'H4_FALSE', close, h3, h4, l3, l4))

            # CALL signals
            # S5: L3 bounce
            if (low <= l3 * 1.002 and close > l3 and close > open_):
                signals.append(('CALL', 'L3_BNC', close, h3, h4, l3, l4))

            # S6: L4 bounce
            if (low <= l4 * 1.002 and close > l4 and close > open_):
                signals.append(('CALL', 'L4_BNC', close, h3, h4, l3, l4))

            # S8: L4 false breakdown
            if (prev_close_intraday < l4 and close > l4 and close > open_):
                signals.append(('CALL', 'L4_FALSE', close, h3, h4, l3, l4))

            for sig_type, sig_name, entry, wh3, wh4, wl3, wl4 in signals:
                # Calculate SL based on type
                if sig_type == 'PUT':
                    if sl_type == 'level':
                        sl = wh4  # SL above H4
                    elif sl_type == 'pct':
                        sl = entry * (1 + sl_pct/100)
                    elif sl_type == 'half_range':
                        sl = entry + (wh4 - wh3) / 2

                    # Targets
                    t1 = wh3  # first target (H3)
                    t2 = wl3  # full target (L3)

                else:  # CALL
                    if sl_type == 'level':
                        sl = wl4  # SL below L4
                    elif sl_type == 'pct':
                        sl = entry * (1 - sl_pct/100)
                    elif sl_type == 'half_range':
                        sl = entry - (wl3 - wl4) / 2

                    # Targets
                    t1 = wh3  # first target H3 (above entry)
                    t2 = wh4  # full target H4

                # Simulate on remaining days this week
                result    = 'TIMEOUT'
                exit_price= close
                days_held = 0
                t1_hit    = False

                for future in this_days[j+1:]:
                    days_held += 1
                    fh = float(future['high'])
                    fl = float(future['low'])
                    fc = float(future['close'])

                    if sig_type == 'PUT':
                        # Check SL
                        if fh >= sl:
                            result = 'SL'
                            exit_price = sl
                            break
                        # Check T1
                        if not t1_hit and fl <= t1:
                            t1_hit = True
                            if target_type == 'h3':
                                result = 'T1'
                                exit_price = t1
                                break
                        # Check T2
                        if t1_hit and fl <= t2:
                            result = 'T2'
                            exit_price = t2
                            break
                    else:  # CALL
                        if fl <= sl:
                            result = 'SL'
                            exit_price = sl
                            break
                        if not t1_hit and fh >= t1:
                            t1_hit = True
                            if target_type == 'h3':
                                result = 'T1'
                                exit_price = t1
                                break
                        if t1_hit and fh >= t2:
                            result = 'T2'
                            exit_price = t2
                            break

                if result == 'TIMEOUT':
                    exit_price = float(this_days[-1]['close'])

                # Calculate P&L in points
                if sig_type == 'PUT':
                    pnl = entry - exit_price
                else:
                    pnl = exit_price - entry

                risk   = abs(entry - sl)
                reward = abs(t2 - entry)
                rr     = round(reward/risk, 1) if risk > 0 else 0

                trades.append({
                    'date'     : day['date'],
                    'sig_type' : sig_type,
                    'sig_name' : sig_name,
                    'entry'    : round(entry, 2),
                    'sl'       : round(sl, 2),
                    't1'       : round(t1, 2),
                    't2'       : round(t2, 2),
                    'exit'     : round(exit_price, 2),
                    'result'   : result,
                    'pnl_pts'  : round(pnl, 2),
                    'risk_pts' : round(risk, 2),
                    'rr'       : rr,
                    'days_held': days_held,
                })

            prev_close_intraday = close

    return trades

def analyze(trades, label):
    if not trades:
        print(f"{label}: No trades")
        return

    winners  = [t for t in trades if t['pnl_pts'] > 0]
    losers   = [t for t in trades if t['pnl_pts'] <= 0]
    t1_hits  = [t for t in trades if t['result'] in ['T1', 'T2']]
    sl_hits  = [t for t in trades if t['result'] == 'SL']
    timeouts = [t for t in trades if t['result'] == 'TIMEOUT']

    win_rate  = round(len(winners)/len(trades)*100, 1)
    avg_win   = round(sum(t['pnl_pts'] for t in winners)/len(winners), 1) if winners else 0
    avg_loss  = round(sum(t['pnl_pts'] for t in losers)/len(losers), 1) if losers else 0
    ev        = round(sum(t['pnl_pts'] for t in trades)/len(trades), 1)
    avg_risk  = round(sum(t['risk_pts'] for t in trades)/len(trades), 1)
    per_month = round(len(trades)/24, 1)

    # By signal type
    put_trades  = [t for t in trades if t['sig_type'] == 'PUT']
    call_trades = [t for t in trades if t['sig_type'] == 'CALL']

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"Total trades  : {len(trades)} ({per_month}/month)")
    print(f"PUT trades    : {len(put_trades)}")
    print(f"CALL trades   : {len(call_trades)}")
    print(f"")
    print(f"Win rate      : {win_rate}%")
    print(f"Target hits   : {len(t1_hits)}")
    print(f"SL hits       : {len(sl_hits)}")
    print(f"Timeouts      : {len(timeouts)}")
    print(f"")
    print(f"Avg win (pts) : +{avg_win}")
    print(f"Avg loss (pts): {avg_loss}")
    print(f"Avg risk (pts): {avg_risk}")
    print(f"EV/trade (pts): {ev}")
    print(f"")

    # By signal name
    print(f"By Signal Type:")
    for sig in ['H3_REJ', 'H4_REJ', 'H4_FALSE', 'L3_BNC', 'L4_BNC', 'L4_FALSE']:
        st = [t for t in trades if t['sig_name'] == sig]
        if not st:
            continue
        sw = [t for t in st if t['pnl_pts'] > 0]
        sev = round(sum(t['pnl_pts'] for t in st)/len(st), 1)
        print(f"  {sig:<12}: {len(st):>3} trades | "
              f"WR:{round(len(sw)/len(st)*100,1):>5}% | "
              f"EV:{sev:>6} pts")

    print(f"\nYear by Year:")
    for year in ['2024', '2025', '2026']:
        yt = [t for t in trades if t['date'].startswith(year)]
        if not yt: continue
        yw = [t for t in yt if t['pnl_pts'] > 0]
        yev = round(sum(t['pnl_pts'] for t in yt)/len(yt), 1)
        print(f"  {year}: {len(yt):>3} trades | "
              f"WR:{round(len(yw)/len(yt)*100,1):>5}% | "
              f"EV:{yev:>6} pts/trade")

if __name__ == '__main__':
    print("Running Index Options SL Backtest (2024-2026)...")
    print("Testing different SL and Target combinations...")

    for sym in ['BANKNIFTY', 'NIFTY50']:
        print(f"\n{'#'*60}")
        print(f"  {sym}")
        print(f"{'#'*60}")

        # Test 1: SL at level (H4/L4), Target = H3/L3 only
        t = backtest_sl(sym, sl_type='level', target_type='h3')
        analyze(t, f"{sym} | SL=Level(H4/L4) | Target=First(H3/L3)")

        # Test 2: SL at level, Target = full (L3/H3)
        t = backtest_sl(sym, sl_type='level', target_type='l3')
        analyze(t, f"{sym} | SL=Level(H4/L4) | Target=Full(L3/H3)")

        # Test 3: SL at 1%
        t = backtest_sl(sym, sl_type='pct', sl_pct=1.0, target_type='h3')
        analyze(t, f"{sym} | SL=1% | Target=First")

        # Test 4: SL at 1.5%
        t = backtest_sl(sym, sl_type='pct', sl_pct=1.5, target_type='h3')
        analyze(t, f"{sym} | SL=1.5% | Target=First")

        # Test 5: SL at 2%
        t = backtest_sl(sym, sl_type='pct', sl_pct=2.0, target_type='h3')
        analyze(t, f"{sym} | SL=2% | Target=First")

        # Test 6: Half range SL
        t = backtest_sl(sym, sl_type='half_range', target_type='h3')
        analyze(t, f"{sym} | SL=HalfRange | Target=First")
