import sqlite3, json, pandas as pd
from datetime import date

print('=== DEEP ANALYSIS OF HV + CAMARILLA RESULTS ===', flush=True)
print('', flush=True)

# Load backtest results
with open('backtest_hv_cam_results.json') as f:
    results = json.load(f)

total    = len(results)
winners  = [r for r in results if r['outcome_hv'] == 'TARGET_HIT']
losers   = [r for r in results if r['outcome_hv'] == 'SL_HIT']
texits   = [r for r in results if r['outcome_hv'] == 'TIME_EXIT']

print(f'Total trades: {total}', flush=True)
print(f'', flush=True)

# ── Analysis 1: Volume combinations ───────────────────────────
print('=== 1. VOLUME ANALYSIS ===', flush=True)
for vmin in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]:
    f  = [r for r in results if r['vol_ratio'] >= vmin]
    if len(f) < 5: continue
    w  = [r for r in f if r['outcome_hv']=='TARGET_HIT']
    l  = [r for r in f if r['outcome_hv']=='SL_HIT']
    wr = len(w)/len(f)*100
    aw = sum(r['pnl_hv'] for r in w)/len(w) if w else 0
    al = sum(r['pnl_hv'] for r in l)/len(l) if l else 0
    ev = (wr/100*aw)+((len(l)/len(f))*al)
    print(f'  Vol>={vmin}x: {len(f):>4} trades | Win:{wr:>5.1f}% | AvgWin:{aw:>6.2f}% | AvgLoss:{al:>6.2f}% | EV:{ev:>6.2f}%', flush=True)

# ── Analysis 2: L3 alignment ───────────────────────────────────
print('', flush=True)
print('=== 2. L3 vs HV LOW ALIGNMENT ===', flush=True)
for align in [0.25, 0.5, 0.75, 1.0, 1.5, 2.0]:
    f  = [r for r in results if r['l3_vs_hvlow'] <= align]
    if len(f) < 5: continue
    w  = [r for r in f if r['outcome_hv']=='TARGET_HIT']
    l  = [r for r in f if r['outcome_hv']=='SL_HIT']
    wr = len(w)/len(f)*100
    aw = sum(r['pnl_hv'] for r in w)/len(w) if w else 0
    al = sum(r['pnl_hv'] for r in l)/len(l) if l else 0
    ev = (wr/100*aw)+((len(l)/len(f))*al)
    print(f'  Within {align}%: {len(f):>4} trades | Win:{wr:>5.1f}% | AvgWin:{aw:>6.2f}% | AvgLoss:{al:>6.2f}% | EV:{ev:>6.2f}%', flush=True)

# ── Analysis 3: Days after HV ──────────────────────────────────
print('', flush=True)
print('=== 3. HV AGE AT SIGNAL ===', flush=True)
conn = sqlite3.connect('nse_data.db')
c    = conn.cursor()
for r in results:
    c.execute('SELECT hv_date FROM hv_summary WHERE symbol=?', (r['symbol'],))
    row = c.fetchone()
    if row:
        hv  = pd.to_datetime(row[0])
        sig = pd.to_datetime(r['signal_date'])
        r['hv_age'] = (sig - hv).days
    else:
        r['hv_age'] = 0
conn.close()

for age_max in [15, 30, 45, 60, 90]:
    f  = [r for r in results if r.get('hv_age',0) <= age_max]
    if len(f) < 5: continue
    w  = [r for r in f if r['outcome_hv']=='TARGET_HIT']
    l  = [r for r in f if r['outcome_hv']=='SL_HIT']
    wr = len(w)/len(f)*100
    aw = sum(r['pnl_hv'] for r in w)/len(w) if w else 0
    al = sum(r['pnl_hv'] for r in l)/len(l) if l else 0
    ev = (wr/100*aw)+((len(l)/len(f))*al)
    print(f'  HV age <={age_max}d: {len(f):>4} trades | Win:{wr:>5.1f}% | AvgWin:{aw:>6.2f}% | EV:{ev:>6.2f}%', flush=True)

# ── Analysis 4: Combined best filters ─────────────────────────
print('', flush=True)
print('=== 4. COMBINED FILTER MATRIX ===', flush=True)
print(f'{"Vol":>5} {"Align":>6} {"Candle":>7} {"Age":>5} {"N":>5} {"Win%":>6} {"AvgW":>7} {"AvgL":>7} {"EV":>7}', flush=True)
print('-'*65, flush=True)

combos = []
for vmin in [0.5, 1.0, 1.5, 2.0]:
    for align in [0.5, 1.0, 1.5, 2.0]:
        for cmin in [50, 60, 70]:
            for age in [30, 60, 90]:
                f = [r for r in results if
                     r['vol_ratio']   >= vmin and
                     r['l3_vs_hvlow'] <= align and
                     r['candle_pos']  >= cmin and
                     r.get('hv_age',0) <= age]
                if len(f) < 10: continue
                w  = [r for r in f if r['outcome_hv']=='TARGET_HIT']
                l  = [r for r in f if r['outcome_hv']=='SL_HIT']
                wr = len(w)/len(f)*100
                aw = sum(r['pnl_hv'] for r in w)/len(w) if w else 0
                al = sum(r['pnl_hv'] for r in l)/len(l) if l else 0
                ev = (wr/100*aw)+((len(l)/len(f))*al)
                combos.append((ev, vmin, align, cmin, age, len(f), wr, aw, al))

combos.sort(reverse=True)
for ev,vmin,align,cmin,age,n,wr,aw,al in combos[:15]:
    print(f'{vmin:>5.1f} {align:>6.1f} {cmin:>7} {age:>5} {n:>5} {wr:>5.1f}% {aw:>6.2f}% {al:>6.2f}% {ev:>6.2f}%', flush=True)

# ── Analysis 5: Holding period ────────────────────────────────
print('', flush=True)
print('=== 5. HOLDING PERIOD ANALYSIS ===', flush=True)
for hold in [5, 7, 10, 15, 20]:
    f_hold = []
    for r in results:
        outcome = r['outcome_hv']
        days    = r['exit_days']
        pnl     = r['pnl_hv']
        if outcome == 'TARGET_HIT' and days <= hold:
            f_hold.append({'outcome': 'TARGET_HIT', 'pnl': pnl})
        elif outcome == 'SL_HIT':
            f_hold.append({'outcome': 'SL_HIT', 'pnl': pnl})
        elif days >= hold:
            # Force exit at hold day
            f_hold.append({'outcome': 'TIME_EXIT', 'pnl': pnl})
        else:
            f_hold.append({'outcome': outcome, 'pnl': pnl})
    if not f_hold: continue
    w  = [r for r in f_hold if r['outcome']=='TARGET_HIT']
    l  = [r for r in f_hold if r['outcome']=='SL_HIT']
    wr = len(w)/len(f_hold)*100
    aw = sum(r['pnl'] for r in w)/len(w) if w else 0
    al = sum(r['pnl'] for r in l)/len(l) if l else 0
    ev = (wr/100*aw)+((len(l)/len(f_hold))*al)
    print(f'  Hold {hold:>2}d: Win:{wr:>5.1f}% | AvgWin:{aw:>6.2f}% | EV:{ev:>6.2f}%', flush=True)

# ── Analysis 6: Market condition (Nifty trend) ───────────────
print('', flush=True)
print('=== 6. YEAR BY YEAR PERFORMANCE ===', flush=True)
for yr in [2021, 2022, 2023, 2024, 2025, 2026]:
    f  = [r for r in results if r['signal_date'].startswith(str(yr))]
    if len(f) < 3: continue
    w  = [r for r in f if r['outcome_hv']=='TARGET_HIT']
    l  = [r for r in f if r['outcome_hv']=='SL_HIT']
    wr = len(w)/len(f)*100
    aw = sum(r['pnl_hv'] for r in w)/len(w) if w else 0
    al = sum(r['pnl_hv'] for r in l)/len(l) if l else 0
    ev = (wr/100*aw)+((len(l)/len(f))*al)
    print(f'  {yr}: {len(f):>4} trades | Win:{wr:>5.1f}% | AvgWin:{aw:>6.2f}% | EV:{ev:>6.2f}%', flush=True)

print('', flush=True)
print('=== ANALYSIS DONE ===', flush=True)

# ── Part 2: Current stocks with L3 near HV Low ───────────────
print('', flush=True)
print('=== CURRENT OPPORTUNITIES (L3 near HV Low today) ===', flush=True)

conn = sqlite3.connect('nse_data.db')
conn.row_factory = sqlite3.Row
c    = conn.cursor()

# Get all HV setups with bullish candle within 90 days
from datetime import date, timedelta
today    = date.today()
min_date = (today - timedelta(days=90)).strftime('%Y-%m-%d')

c.execute('''
    SELECT h.symbol, h.hv_date, h.hv_low, h.hv_high, h.hv_close,
           h.latest_close,
           ROUND((h.hv_close-h.hv_low)/(h.hv_high-h.hv_low)*100,1) as candle_pos,
           ROUND((h.hv_high-h.hv_low)/h.hv_low*100,1) as hv_range,
           julianday(?) - julianday(h.hv_date) as age
    FROM hv_summary h
    WHERE h.hv_date >= ?
    AND (h.hv_close-h.hv_low)/(h.hv_high-h.hv_low) >= 0.5
    AND h.hv_low > 0
    ORDER BY h.hv_date DESC
''', (str(today), min_date))

hv_stocks = c.fetchall()

# For each stock get yesterday's OHLC to calculate Camarilla
opportunities = []
for s in hv_stocks:
    sym    = s['symbol']
    hv_low = s['hv_low']
    hv_high= s['hv_high']

    # Get last 2 days of price data
    c.execute('''
        SELECT date, open, high, low, close, volume
        FROM daily_prices
        WHERE symbol = ?
        ORDER BY date DESC
        LIMIT 3
    ''', (sym,))
    rows = c.fetchall()
    if len(rows) < 2:
        continue

    today_row = rows[0]
    prev_row  = rows[1]

    # Calculate Camarilla from yesterday
    rng = float(prev_row['high']) - float(prev_row['low'])
    l3  = float(prev_row['close']) - rng * 0.55 / 2
    l4  = float(prev_row['close']) - rng * 1.1  / 2
    h3  = float(prev_row['close']) + rng * 0.55 / 2
    h4  = float(prev_row['close']) + rng * 1.1  / 2

    # Check alignment
    l3_vs_hv = abs(l3 - hv_low) / hv_low * 100
    curr_price = float(today_row['close'])
    pct_above  = (curr_price - hv_low) / hv_low * 100

    # Get average volume
    c.execute('''
        SELECT AVG(volume) FROM (
            SELECT volume FROM daily_prices
            WHERE symbol = ? ORDER BY date DESC LIMIT 20
        )
    ''', (sym,))
    avg_vol = c.fetchone()[0] or 1
    vol_ratio = float(today_row['volume']) / avg_vol

    opportunities.append({
        'symbol'    : sym,
        'hv_date'   : s['hv_date'],
        'age'       : int(s['age']),
        'candle_pos': s['candle_pos'],
        'hv_low'    : hv_low,
        'hv_high'   : hv_high,
        'hv_range'  : s['hv_range'],
        'current'   : curr_price,
        'pct_above' : round(pct_above, 2),
        'l3'        : round(l3, 2),
        'l4'        : round(l4, 2),
        'h3'        : round(h3, 2),
        'l3_vs_hv'  : round(l3_vs_hv, 2),
        'vol_ratio' : round(vol_ratio, 2),
    })

conn.close()

# Filter and sort by best opportunities
opps = [o for o in opportunities if o['l3_vs_hv'] <= 2.0 and o['pct_above'] <= 5.0]
opps.sort(key=lambda x: x['l3_vs_hv'])

print(f'Stocks where Camarilla L3 aligns with HV Low today: {len(opps)}', flush=True)
print(f'', flush=True)
print(f'{"Symbol":<12} {"Age":>4} {"Candle":>7} {"HV Low":>9} {"L3":>9} {"L4":>9} {"Current":>9} {"L3vsHV":>7} {"Vol":>6} {"Upside":>7}', flush=True)
print('-'*95, flush=True)

for o in opps[:20]:
    upside = round((o['hv_high'] - o['current'])/o['current']*100, 1)
    print(f'{o["symbol"]:<12} {o["age"]:>3}d {o["candle_pos"]:>6}% '
          f'{o["hv_low"]:>9.2f} {o["l3"]:>9.2f} {o["l4"]:>9.2f} '
          f'{o["current"]:>9.2f} {o["l3_vs_hv"]:>6.2f}% '
          f'{o["vol_ratio"]:>5.1f}x {upside:>6.1f}%', flush=True)

print('', flush=True)
print('COMPLETE!', flush=True)
