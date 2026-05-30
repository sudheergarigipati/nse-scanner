import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict
import os

print("Starting winner analysis...", flush=True)

# ── Two separate DBs ──────────────────────────────────────────
src  = sqlite3.connect('nse_data.db')
src.row_factory = sqlite3.Row
sc   = src.cursor()

res  = sqlite3.connect('winner_analysis.db')
res.row_factory = sqlite3.Row
rc   = res.cursor()

# Create results table
rc.execute('''
    CREATE TABLE IF NOT EXISTS signals (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol      TEXT,
        date        TEXT,
        hv_date     TEXT,
        hv_age      INTEGER,
        entry       REAL,
        sl          REAL,
        target      REAL,
        exit_price  REAL,
        exit_date   TEXT,
        result      TEXT,
        pnl_pct     REAL,
        max_gain    REAL,
        beyond_hv   INTEGER,
        pct_beyond  REAL,
        rr          REAL,
        days_held   INTEGER,
        vol_ratio   REAL,
        candle_str  REAL,
        hv_vol_ratio REAL,
        shares      INTEGER,
        pnl_rs      REAL,
        UNIQUE(symbol, date)
    )
''')
rc.execute('''
    CREATE TABLE IF NOT EXISTS progress (
        id       INTEGER PRIMARY KEY,
        last_date TEXT
    )
''')
res.commit()

# Check last processed date (resume support)
rc.execute('SELECT last_date FROM progress WHERE id=1')
r = rc.fetchone()
resume_from = r['last_date'] if r else '2024-05-01'
print(f"Resuming from: {resume_from}", flush=True)

# ── Load prices into memory in BATCHES of 50 symbols ─────────
sc.execute('SELECT DISTINCT date FROM daily_prices WHERE date >= ? AND date <= ? ORDER BY date', ('2024-05-01','2026-05-27'))
trading_days = [r['date'] for r in sc.fetchall()]

sc.execute('SELECT symbol FROM daily_prices GROUP BY symbol HAVING COUNT(*) >= 252')
all_valid = [r['symbol'] for r in sc.fetchall()]
print(f"Valid symbols: {len(all_valid)} | Trading days: {len(trading_days)}", flush=True)

RISK_PER_TRADE = 1000
MAX_POS_VAL    = 20000
MIN_RR         = 5.0
MAX_L3_HV_PCT  = 4.0
VOL_MULTIPLIER = 0.5
BATCH_SIZE     = 50  # process 50 symbols at a time

def get_camarilla(h, l, cl):
    rng = h - l
    return cl - rng*1.1/4, cl - rng*1.1/2

total_processed = 0
total_signals   = 0

# Process in batches of 50 symbols
for batch_start in range(0, len(all_valid), BATCH_SIZE):
    batch = all_valid[batch_start:batch_start+BATCH_SIZE]
    batch_num = batch_start // BATCH_SIZE + 1
    total_batches = (len(all_valid) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"Batch {batch_num}/{total_batches}: {batch[0]} to {batch[-1]}...", flush=True)

    # Load only this batch's price data
    placeholders = ','.join('?' * len(batch))
    sc.execute(f'''
        SELECT symbol, date, open, high, low, close, volume
        FROM daily_prices
        WHERE symbol IN ({placeholders})
        AND date >= '2023-01-01'
        ORDER BY symbol, date
    ''', batch)
    rows = sc.fetchall()

    prices = defaultdict(list)
    for r in rows:
        prices[r['symbol']].append(dict(r))

    price_idx = {}
    for sym, bars in prices.items():
        price_idx[sym] = {b['date']: i for i, b in enumerate(bars)}

    # Avg volume cache for this batch
    avg_vol_cache = {}
    for sym, bars in prices.items():
        for i in range(20, len(bars)):
            avg = sum(b['volume'] for b in bars[i-20:i]) / 20
            avg_vol_cache[(sym, bars[i]['date'])] = avg

    batch_signals = []

    for i, today in enumerate(trading_days):
        if today < resume_from: continue
        if i == 0: continue
        today_dt = datetime.strptime(today, '%Y-%m-%d')
        date_min = (today_dt - timedelta(days=150)).strftime('%Y-%m-%d')
        date_max = (today_dt - timedelta(days=7)).strftime('%Y-%m-%d')

        for sym in batch:
            if sym not in price_idx: continue
            idx = price_idx[sym].get(today)
            if idx is None or idx < 1: continue
            bars = prices[sym]
            tb = bars[idx]
            pb = bars[idx-1]
            if tb['date'] != today: continue

            # Find HV point-in-time
            hv_bar = None; hv_vol = 0
            for b in bars:
                if b['date'] < date_min: continue
                if b['date'] > date_max: break
                if b['high'] == b['low']: continue
                if b['close'] < b['low'] + (b['high']-b['low'])*0.5: continue
                if b['volume'] > hv_vol:
                    hv_vol = b['volume']
                    hv_bar = b
            if not hv_bar: continue

            hv_high = hv_bar['high']
            hv_low  = hv_bar['low']
            hv_date = hv_bar['date']
            hv_age  = (today_dt - datetime.strptime(hv_date, '%Y-%m-%d')).days

            l3, l4  = get_camarilla(pb['high'], pb['low'], pb['close'])
            avg_vol = avg_vol_cache.get((sym, today), 0)

            if not (tb['low'] <= l3):                             continue
            if not (tb['close'] > tb['open']):                    continue
            if not (tb['close'] >= l3):                           continue
            if not (abs(l3-hv_low)/hv_low*100 <= MAX_L3_HV_PCT): continue
            if not (tb['volume'] >= avg_vol * VOL_MULTIPLIER):    continue
            if not (tb['close'] >= 50):                           continue

            risk   = l3 - l4
            reward = hv_high - l3
            if risk <= 0 or reward <= 0: continue
            rr = reward / risk
            if rr < MIN_RR: continue

            shares = min(int(RISK_PER_TRADE/risk), int(MAX_POS_VAL/l3))
            if shares < 1: continue

            # Track outcome — next 30 days
            result     = 'OPEN'
            exit_price = 0
            exit_date  = ''
            max_high   = tb['close']
            days_held  = 0
            beyond_hv  = 0
            pct_beyond = 0.0

            for j in range(idx+1, min(idx+31, len(bars))):
                future = bars[j]
                days_held += 1
                max_high = max(max_high, future['high'])
                if future['low'] <= l4:
                    result     = 'SL'
                    exit_price = l4
                    exit_date  = future['date']
                    break
                if future['high'] >= hv_high:
                    result     = 'WIN'
                    exit_price = hv_high
                    exit_date  = future['date']
                    if future['high'] > hv_high:
                        beyond_hv  = 1
                        pct_beyond = round((future['high']-hv_high)/hv_high*100, 2)
                    break

            if result == 'OPEN':
                last_idx   = min(idx+30, len(bars)-1)
                exit_price = bars[last_idx]['close']
                exit_date  = bars[last_idx]['date']

            pnl_pct    = round((exit_price - l3) / l3 * 100, 2)
            max_gain   = round((max_high - l3) / l3 * 100, 2)
            vol_ratio  = round(tb['volume'] / avg_vol, 2) if avg_vol > 0 else 0
            candle_str = round((tb['close']-tb['open'])/(tb['high']-tb['low'])*100, 1) if tb['high'] != tb['low'] else 0
            hv_vol_r   = round(hv_vol / avg_vol, 2) if avg_vol > 0 else 0

            batch_signals.append((
                sym, today, hv_date, hv_age,
                round(l3,2), round(l4,2), round(hv_high,2),
                round(exit_price,2), exit_date, result,
                pnl_pct, max_gain, beyond_hv, pct_beyond,
                round(rr,1), days_held, vol_ratio, candle_str,
                hv_vol_r, shares,
                round((exit_price-l3)*shares, 0)
            ))

    # Save batch to DB
    rc.executemany('''
        INSERT OR IGNORE INTO signals
        (symbol,date,hv_date,hv_age,entry,sl,target,
         exit_price,exit_date,result,pnl_pct,max_gain,
         beyond_hv,pct_beyond,rr,days_held,vol_ratio,
         candle_str,hv_vol_ratio,shares,pnl_rs)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ''', batch_signals)

    # Save progress
    rc.execute('INSERT OR REPLACE INTO progress (id,last_date) VALUES (1,?)', (trading_days[-1],))
    res.commit()

    total_signals += len(batch_signals)
    total_processed += len(batch)
    print(f"  Saved {len(batch_signals)} signals | Total so far: {total_signals}", flush=True)

# ── Print analysis from DB ────────────────────────────────────
print(f"\n{'='*70}", flush=True)
print(f"  WINNER ANALYSIS RESULTS", flush=True)
print(f"{'='*70}", flush=True)

rc.execute('SELECT COUNT(*) as n FROM signals')
total = rc.fetchone()['n']
rc.execute('SELECT COUNT(*) as n FROM signals WHERE result=?', ('WIN',))
wins = rc.fetchone()['n']
rc.execute('SELECT COUNT(*) as n FROM signals WHERE result=?', ('SL',))
losses = rc.fetchone()['n']
rc.execute('SELECT COUNT(*) as n FROM signals WHERE beyond_hv=1')
beyond = rc.fetchone()['n']

print(f"  Total signals : {total}", flush=True)
print(f"  Wins (hit HV) : {wins}  ({round(wins/total*100,1)}%)", flush=True)
print(f"  SL hits       : {losses} ({round(losses/total*100,1)}%)", flush=True)
print(f"  Beyond HV     : {beyond}  ({round(beyond/total*100,1)}%)", flush=True)

# HV Age analysis
print(f"\n  HV AGE ANALYSIS:", flush=True)
for a1,a2 in [(7,30),(31,60),(61,90),(91,120),(121,150)]:
    rc.execute('SELECT COUNT(*) as n, AVG(pnl_rs) as ap FROM signals WHERE hv_age>=? AND hv_age<=?',(a1,a2))
    r=rc.fetchone()
    rc.execute('SELECT COUNT(*) as w FROM signals WHERE hv_age>=? AND hv_age<=? AND result=?',(a1,a2,'WIN'))
    w=rc.fetchone()['w']
    if r['n']==0: continue
    wr=round(w/r['n']*100,1)
    print(f"    Age {a1:>3}-{a2:>3}d: {r['n']:>5} signals | WR {wr:>5}% | Avg P&L Rs {round(r['ap'] or 0):>+7,}", flush=True)

# Volume ratio
print(f"\n  VOLUME RATIO on signal day:", flush=True)
for v1,v2 in [(0,0.75),(0.75,1.0),(1.0,1.5),(1.5,2.0),(2.0,99)]:
    rc.execute('SELECT COUNT(*) as n, AVG(pnl_rs) as ap FROM signals WHERE vol_ratio>=? AND vol_ratio<?',(v1,v2))
    r=rc.fetchone()
    rc.execute('SELECT COUNT(*) as w FROM signals WHERE vol_ratio>=? AND vol_ratio<? AND result=?',(v1,v2,'WIN'))
    w=rc.fetchone()['w']
    if r['n']==0: continue
    wr=round(w/r['n']*100,1)
    print(f"    Vol {v1:.2f}x-{v2:.2f}x: {r['n']:>5} signals | WR {wr:>5}% | Avg P&L Rs {round(r['ap'] or 0):>+7,}", flush=True)

# RR analysis
print(f"\n  R:R ANALYSIS:", flush=True)
for r1,r2 in [(5,8),(8,12),(12,20),(20,50),(50,999)]:
    rc.execute('SELECT COUNT(*) as n, AVG(pnl_rs) as ap FROM signals WHERE rr>=? AND rr<?',(r1,r2))
    r=rc.fetchone()
    rc.execute('SELECT COUNT(*) as w FROM signals WHERE rr>=? AND rr<? AND result=?',(r1,r2,'WIN'))
    w=rc.fetchone()['w']
    if r['n']==0: continue
    wr=round(w/r['n']*100,1)
    print(f"    R:R {r1:>3}-{r2:>3}x: {r['n']:>5} signals | WR {wr:>5}% | Avg P&L Rs {round(r['ap'] or 0):>+7,}", flush=True)

# HV strength
print(f"\n  HV DAY STRENGTH:", flush=True)
for v1,v2 in [(1,2),(2,3),(3,5),(5,10),(10,99)]:
    rc.execute('SELECT COUNT(*) as n, AVG(pnl_rs) as ap FROM signals WHERE hv_vol_ratio>=? AND hv_vol_ratio<?',(v1,v2))
    r=rc.fetchone()
    rc.execute('SELECT COUNT(*) as w FROM signals WHERE hv_vol_ratio>=? AND hv_vol_ratio<? AND result=?',(v1,v2,'WIN'))
    w=rc.fetchone()['w']
    if r['n']==0: continue
    wr=round(w/r['n']*100,1)
    print(f"    HV {v1}x-{v2}x: {r['n']:>5} signals | WR {wr:>5}% | Avg P&L Rs {round(r['ap'] or 0):>+7,}", flush=True)

# Top 20 beyond HV winners
print(f"\n  TOP 20 BEYOND-HV WINNERS:", flush=True)
print(f"  {'Symbol':<12} {'Date':<12} {'Entry':>8} {'Target':>8} {'Beyond%':>8} {'HVAge':>6} {'RR':>5} {'VolR':>5} {'HVVolR':>7} {'Days':>5}", flush=True)
print(f"  {'-'*80}", flush=True)
rc.execute('''
    SELECT * FROM signals WHERE beyond_hv=1
    ORDER BY pct_beyond DESC LIMIT 20
''')
for t in rc.fetchall():
    print(f"  {t['symbol']:<12} {t['date']:<12} {t['entry']:>8.2f} {t['target']:>8.2f} "
          f"{t['pct_beyond']:>7.1f}% {t['hv_age']:>6} {t['rr']:>5} "
          f"{t['vol_ratio']:>5.1f} {t['hv_vol_ratio']:>7.1f}x {t['days_held']:>5}", flush=True)

# Winner vs Loser profile
print(f"\n  WINNERS vs LOSERS PROFILE:", flush=True)
print(f"  {'Metric':<25} {'Winners':>12} {'Losers':>12}", flush=True)
print(f"  {'-'*50}", flush=True)
for metric in ['hv_age','rr','vol_ratio','hv_vol_ratio','candle_str','days_held']:
    rc.execute(f'SELECT AVG({metric}) as v FROM signals WHERE result=?',('WIN',))
    wv = rc.fetchone()['v'] or 0
    rc.execute(f'SELECT AVG({metric}) as v FROM signals WHERE result=?',('SL',))
    lv = rc.fetchone()['v'] or 0
    print(f"  {metric:<25} {round(wv,2):>12} {round(lv,2):>12}", flush=True)

src.close()
res.close()
print("\nDone!", flush=True)
