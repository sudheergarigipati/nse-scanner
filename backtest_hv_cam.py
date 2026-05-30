import sqlite3
import pandas as pd
import numpy as np
import json
from datetime import datetime, timedelta

print('=== HV + CAMARILLA BACKTEST ===', flush=True)
print('Period  : 2021 to 2026 (5 years)', flush=True)
print('Strategy: HV Low + Daily Camarilla L3 alignment', flush=True)
print('Entry   : Price touches L3 when L3 within 2% of HV Low', flush=True)
print('SL      : Camarilla L4', flush=True)
print('Target  : HV High', flush=True)
print('', flush=True)

conn = sqlite3.connect('nse_data.db')
conn.row_factory = sqlite3.Row

# Get all HV setups with bullish candle
c = conn.cursor()
c.execute('''
    SELECT symbol, hv_date, hv_low, hv_high, hv_close, hv_volume,
           ROUND((hv_close-hv_low)/(hv_high-hv_low)*100,1) as candle_pos,
           ROUND((hv_high-hv_low)/hv_low*100,1) as hv_range_pct
    FROM hv_summary
    WHERE (hv_close-hv_low)/(hv_high-hv_low) >= 0.5
    AND hv_low > 0
    AND hv_volume > 0
    AND hv_high > hv_low * 1.05
    ORDER BY hv_date
''')
hv_setups = c.fetchall()
print(f'HV setups to test: {len(hv_setups)}', flush=True)

def get_prices(symbol, start_date, end_date=None):
    query = '''
        SELECT date, open, high, low, close, volume
        FROM daily_prices
        WHERE symbol = ?
        AND date >= ?
    '''
    params = [symbol, start_date]
    if end_date:
        query += ' AND date <= ?'
        params.append(end_date)
    query += ' ORDER BY date'
    c.execute(query, params)
    rows = c.fetchall()
    if not rows:
        return None
    df = pd.DataFrame([dict(r) for r in rows])
    df['date'] = pd.to_datetime(df['date'])
    df.set_index('date', inplace=True)
    return df

def calc_camarilla(prev_high, prev_low, prev_close):
    rng = prev_high - prev_low
    h4  = prev_close + rng * 1.1 / 2
    h3  = prev_close + rng * 0.55 / 2
    l3  = prev_close - rng * 0.55 / 2
    l4  = prev_close - rng * 1.1 / 2
    return h3, h4, l3, l4

results  = []
skipped  = 0
no_data  = 0

for idx_s, s in enumerate(hv_setups):
    sym          = s['symbol']
    hv_date      = s['hv_date']
    hv_low       = s['hv_low']
    hv_high      = s['hv_high']
    candle_pos   = s['candle_pos']
    hv_range_pct = s['hv_range_pct']

    if idx_s % 50 == 0:
        print(f'Progress: {idx_s}/{len(hv_setups)} | Signals: {len(results)} | Skipped: {skipped}', flush=True)

    # Get price data from HV date onwards
    df = get_prices(sym, hv_date)
    if df is None or len(df) < 10:
        no_data += 1
        continue

    df_list = list(df.iterrows())

    # Scan each day after HV day for entry signal
    for i in range(1, len(df_list)):
        date_idx, row = df_list[i]

        # Need previous day for Camarilla
        prev_date, prev_row = df_list[i-1]

        # Calculate Camarilla from previous day
        h3, h4, l3, l4 = calc_camarilla(
            float(prev_row['high']),
            float(prev_row['low']),
            float(prev_row['close'])
        )

        curr_low   = float(row['low'])
        curr_close = float(row['close'])
        curr_open  = float(row['open'])
        curr_high  = float(row['high'])
        curr_vol   = float(row['volume'])

        # Calculate average volume (20 day)
        start_vol = max(0, i-20)
        avg_vol = df['volume'].iloc[start_vol:i].mean()
        vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 0

        # Check alignment: L3 within 2% of HV Low
        l3_vs_hvlow = abs(l3 - hv_low) / hv_low * 100
        if l3_vs_hvlow > 2.0:
            continue

        # Check entry conditions
        price_at_l3  = curr_low <= l3 * 1.005
        green_candle = curr_close > curr_open
        pct_above_hv = (curr_close - hv_low) / hv_low * 100

        if not (price_at_l3 and green_candle and pct_above_hv <= 3.0):
            continue

        # Valid signal! Calculate trade parameters
        entry_price = l3
        sl_price    = round(l4, 2)
        target_h3   = round(h3, 2)  # short target
        target_hv   = hv_high        # full target
        sl_dist     = entry_price - sl_price
        rr_h3       = round((target_h3 - entry_price) / sl_dist, 1) if sl_dist > 0 else 0
        rr_hv       = round((target_hv - entry_price) / sl_dist, 1) if sl_dist > 0 else 0

        # Skip if R:R is poor
        if rr_hv < 1.5:
            skipped += 1
            continue

        # Track outcome from next day
        outcome_h3 = 'OPEN'
        outcome_hv = 'OPEN'
        exit_price = None
        exit_days  = 0

        for j in range(i+1, min(i+21, len(df_list))):
            jdate, jrow = df_list[j]
            jlow  = float(jrow['low'])
            jhigh = float(jrow['high'])
            days  = j - i

            if jlow <= sl_price:
                outcome_h3 = 'SL_HIT'
                outcome_hv = 'SL_HIT'
                exit_price = sl_price
                exit_days  = days
                break
            if jhigh >= target_hv:
                outcome_hv = 'TARGET_HIT'
                if outcome_h3 == 'OPEN':
                    outcome_h3 = 'TARGET_HIT'
                exit_price = target_hv
                exit_days  = days
                break
            if jhigh >= target_h3 and outcome_h3 == 'OPEN':
                outcome_h3 = 'TARGET_HIT'
            if days >= 15:
                outcome_hv = 'TIME_EXIT'
                if outcome_h3 == 'OPEN':
                    outcome_h3 = 'TIME_EXIT'
                exit_price = float(jrow['close'])
                exit_days  = days
                break

        if outcome_hv != 'OPEN':
            pnl_hv = round((exit_price - entry_price) / entry_price * 100, 2)
            pnl_h3 = round((target_h3 - entry_price) / entry_price * 100, 2) if outcome_h3 == 'TARGET_HIT' else pnl_hv

            results.append({
                'symbol'      : sym,
                'hv_date'     : hv_date,
                'signal_date' : str(date_idx.date()),
                'candle_pos'  : candle_pos,
                'hv_range_pct': hv_range_pct,
                'vol_ratio'   : round(vol_ratio, 1),
                'pct_above'   : round(pct_above_hv, 2),
                'l3_vs_hvlow' : round(l3_vs_hvlow, 2),
                'entry'       : round(entry_price, 2),
                'sl'          : sl_price,
                'l3'          : round(l3, 2),
                'l4'          : round(l4, 2),
                'h3'          : target_h3,
                'hv_high'     : target_hv,
                'rr_h3'       : rr_h3,
                'rr_hv'       : rr_hv,
                'outcome_hv'  : outcome_hv,
                'outcome_h3'  : outcome_h3,
                'exit_price'  : round(exit_price, 2),
                'exit_days'   : exit_days,
                'pnl_hv'      : pnl_hv,
                'pnl_h3'      : pnl_h3,
            })
        break  # one signal per stock per HV setup

# Save results
with open('backtest_hv_cam_results.json', 'w') as f:
    json.dump(results, f, indent=2)

conn.close()

total = len(results)
print(f'', flush=True)
print(f'=== HV + CAMARILLA RESULTS ===', flush=True)
print(f'Total signals : {total}', flush=True)
print(f'No data       : {no_data}', flush=True)
print(f'Skipped (RR)  : {skipped}', flush=True)

if total > 0:
    # HV High target results
    winners_hv = [r for r in results if r['outcome_hv'] == 'TARGET_HIT']
    losers_hv  = [r for r in results if r['outcome_hv'] == 'SL_HIT']
    texits_hv  = [r for r in results if r['outcome_hv'] == 'TIME_EXIT']
    wr_hv      = len(winners_hv)/total*100
    avg_w_hv   = sum(r['pnl_hv'] for r in winners_hv)/len(winners_hv) if winners_hv else 0
    avg_l_hv   = sum(r['pnl_hv'] for r in losers_hv)/len(losers_hv) if losers_hv else 0
    avg_t_hv   = sum(r['pnl_hv'] for r in texits_hv)/len(texits_hv) if texits_hv else 0
    avg_d      = sum(r['exit_days'] for r in results)/total
    ev_hv      = (wr_hv/100*avg_w_hv)+(len(losers_hv)/total*avg_l_hv)+(len(texits_hv)/total*avg_t_hv)
    avg_rr     = sum(r['rr_hv'] for r in results)/total

    print(f'', flush=True)
    print(f'--- Target = HV High ---', flush=True)
    print(f'Winners  : {len(winners_hv)} ({wr_hv:.1f}%)', flush=True)
    print(f'Losers   : {len(losers_hv)} ({len(losers_hv)/total*100:.1f}%)', flush=True)
    print(f'TimeExit : {len(texits_hv)} ({len(texits_hv)/total*100:.1f}%)', flush=True)
    print(f'Avg win  : +{avg_w_hv:.2f}%', flush=True)
    print(f'Avg loss : {avg_l_hv:.2f}%', flush=True)
    print(f'Avg time : {avg_t_hv:.2f}%', flush=True)
    print(f'Avg R:R  : {avg_rr:.1f}', flush=True)
    print(f'Avg days : {avg_d:.1f}', flush=True)
    print(f'Exp val  : {ev_hv:.2f}% per trade', flush=True)

    # H3 target results
    winners_h3 = [r for r in results if r['outcome_h3'] == 'TARGET_HIT']
    losers_h3  = [r for r in results if r['outcome_h3'] == 'SL_HIT']
    texits_h3  = [r for r in results if r['outcome_h3'] == 'TIME_EXIT']
    wr_h3      = len(winners_h3)/total*100
    avg_w_h3   = sum(r['pnl_h3'] for r in winners_h3)/len(winners_h3) if winners_h3 else 0
    avg_l_h3   = sum(r['pnl_hv'] for r in losers_h3)/len(losers_h3) if losers_h3 else 0
    avg_t_h3   = sum(r['pnl_hv'] for r in texits_h3)/len(texits_h3) if texits_h3 else 0
    ev_h3      = (wr_h3/100*avg_w_h3)+(len(losers_h3)/total*avg_l_h3)+(len(texits_h3)/total*avg_t_h3)

    print(f'', flush=True)
    print(f'--- Target = Camarilla H3 (short target) ---', flush=True)
    print(f'Winners  : {len(winners_h3)} ({wr_h3:.1f}%)', flush=True)
    print(f'Losers   : {len(losers_h3)} ({len(losers_h3)/total*100:.1f}%)', flush=True)
    print(f'TimeExit : {len(texits_h3)} ({len(texits_h3)/total*100:.1f}%)', flush=True)
    print(f'Avg win  : +{avg_w_h3:.2f}%', flush=True)
    print(f'Avg loss : {avg_l_h3:.2f}%', flush=True)
    print(f'Exp val  : {ev_h3:.2f}% per trade', flush=True)

    print(f'', flush=True)
    print(f'=== FILTER ANALYSIS ===', flush=True)

    # Volume filter
    print('Volume filter:', flush=True)
    for vmin in [1.0, 1.5, 2.0, 2.5, 3.0]:
        f = [r for r in results if r['vol_ratio'] >= vmin]
        if len(f) >= 5:
            w  = [r for r in f if r['outcome_hv']=='TARGET_HIT']
            l  = [r for r in f if r['outcome_hv']=='SL_HIT']
            wr2= len(w)/len(f)*100
            aw = sum(r['pnl_hv'] for r in w)/len(w) if w else 0
            al = sum(r['pnl_hv'] for r in l)/len(l) if l else 0
            ev2= (wr2/100*aw)+((len(l)/len(f))*al)
            print(f'  Vol>={vmin}x: {len(f):>4} trades | Win:{wr2:.0f}% | EV:{ev2:.2f}%', flush=True)

    # L3 vs HV Low alignment
    print('L3 vs HV Low alignment:', flush=True)
    for align in [0.5, 1.0, 1.5, 2.0]:
        f = [r for r in results if r['l3_vs_hvlow'] <= align]
        if len(f) >= 5:
            w  = [r for r in f if r['outcome_hv']=='TARGET_HIT']
            l  = [r for r in f if r['outcome_hv']=='SL_HIT']
            wr2= len(w)/len(f)*100
            aw = sum(r['pnl_hv'] for r in w)/len(w) if w else 0
            al = sum(r['pnl_hv'] for r in l)/len(l) if l else 0
            ev2= (wr2/100*aw)+((len(l)/len(f))*al)
            print(f'  L3 within {align}% of HV Low: {len(f):>4} trades | Win:{wr2:.0f}% | EV:{ev2:.2f}%', flush=True)

    # Candle position
    print('HV Candle position:', flush=True)
    for cmin in [50, 60, 70, 80]:
        f = [r for r in results if r['candle_pos'] >= cmin]
        if len(f) >= 5:
            w  = [r for r in f if r['outcome_hv']=='TARGET_HIT']
            l  = [r for r in f if r['outcome_hv']=='SL_HIT']
            wr2= len(w)/len(f)*100
            aw = sum(r['pnl_hv'] for r in w)/len(w) if w else 0
            al = sum(r['pnl_hv'] for r in l)/len(l) if l else 0
            ev2= (wr2/100*aw)+((len(l)/len(f))*al)
            print(f'  Candle>={cmin}%: {len(f):>4} trades | Win:{wr2:.0f}% | EV:{ev2:.2f}%', flush=True)

    # HV Range
    print('HV Range (upside):', flush=True)
    for rng in [10, 15, 20]:
        f = [r for r in results if r['hv_range_pct'] >= rng]
        if len(f) >= 5:
            w  = [r for r in f if r['outcome_hv']=='TARGET_HIT']
            l  = [r for r in f if r['outcome_hv']=='SL_HIT']
            wr2= len(w)/len(f)*100
            aw = sum(r['pnl_hv'] for r in w)/len(w) if w else 0
            al = sum(r['pnl_hv'] for r in l)/len(l) if l else 0
            ev2= (wr2/100*aw)+((len(l)/len(f))*al)
            print(f'  Range>={rng}%: {len(f):>4} trades | Win:{wr2:.0f}% | EV:{ev2:.2f}%', flush=True)

    # Best combination
    print('', flush=True)
    print('=== BEST COMBINATION ===', flush=True)
    best_ev  = -999
    best_cfg = None
    for vmin in [1.0, 1.5, 2.0]:
        for align in [0.5, 1.0, 1.5, 2.0]:
            for cmin in [50, 60, 70]:
                f = [r for r in results if
                     r['vol_ratio']   >= vmin and
                     r['l3_vs_hvlow'] <= align and
                     r['candle_pos']  >= cmin]
                if len(f) < 10:
                    continue
                w  = [r for r in f if r['outcome_hv']=='TARGET_HIT']
                l  = [r for r in f if r['outcome_hv']=='SL_HIT']
                wr2= len(w)/len(f)*100
                aw = sum(r['pnl_hv'] for r in w)/len(w) if w else 0
                al = sum(r['pnl_hv'] for r in l)/len(l) if l else 0
                ev2= (wr2/100*aw)+((len(l)/len(f))*al)
                if ev2 > best_ev:
                    best_ev  = ev2
                    best_cfg = (vmin, align, cmin, len(f), wr2, aw, al)

    if best_cfg:
        vmin,align,cmin,n,wr2,aw,al = best_cfg
        print(f'Volume      >= {vmin}x', flush=True)
        print(f'L3 vs HVLow <= {align}%', flush=True)
        print(f'HV Candle   >= {cmin}%', flush=True)
        print(f'Trades      : {n}', flush=True)
        print(f'Win rate    : {wr2:.1f}%', flush=True)
        print(f'Avg win     : +{aw:.2f}%', flush=True)
        print(f'Avg loss    : {al:.2f}%', flush=True)
        print(f'Exp value   : {best_ev:.2f}% per trade', flush=True)

    # Sample trades
    print('', flush=True)
    print('Sample trades (first 20):', flush=True)
    print(f'{"Symbol":<12} {"Signal":<12} {"Vol":>5} {"L3vsHV":>7} {"Entry":>9} {"SL":>9} {"Target":>9} {"Days":>5} {"PnL%":>7} Outcome', flush=True)
    print('-'*95, flush=True)
    for r in sorted(results, key=lambda x: x['signal_date'])[:20]:
        print(f'{r["symbol"]:<12} {r["signal_date"]:<12} {r["vol_ratio"]:>4.1f}x '
              f'{r["l3_vs_hvlow"]:>6.2f}% {r["entry"]:>9.2f} {r["sl"]:>9.2f} '
              f'{r["hv_high"]:>9.2f} {r["exit_days"]:>5} {r["pnl_hv"]:>6.2f}% {r["outcome_hv"]}', flush=True)

print('DONE!', flush=True)
