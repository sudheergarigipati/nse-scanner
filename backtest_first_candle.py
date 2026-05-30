"""
CLEAN TEST — First Candle Green vs EOD Green
Question: Can we replace EOD green candle filter
          with FIRST 15-min candle green?
          (checkable at 9:30 AM in real time!)

4 categories:
  A: EOD green + 1st candle green → baseline
  B: EOD green + 1st candle RED   → see impact
  C: EOD RED   + 1st candle green → can this work?
  D: EOD RED   + 1st candle RED   → worst case
"""
import sqlite3, json, time
from datetime import datetime, timedelta
from collections import defaultdict

print("="*70, flush=True)
print("  FIRST CANDLE GREEN — CLEAN BACKTEST", flush=True)
print("  Can we replace EOD green with 9:30 AM check?", flush=True)
print("="*70, flush=True)

conn_d  = sqlite3.connect('/home/ubuntu/nse-scanner/nse_data.db')
conn_15 = sqlite3.connect('/home/ubuntu/nse-scanner/intraday_15min.db')
conn_d.row_factory  = sqlite3.Row
conn_15.row_factory = sqlite3.Row
cd  = conn_d.cursor()
c15 = conn_15.cursor()

bl = json.load(open('cautionary_stocks.json')).get('stocks',[])
cd.execute('SELECT symbol FROM daily_prices GROUP BY symbol HAVING COUNT(*) >= 252')
valid_symbols = {r['symbol'] for r in cd.fetchall()}

c15.execute("SELECT DISTINCT DATE(datetime) as dt FROM prices_15min ORDER BY dt")
available_days = [r['dt'] for r in c15.fetchall()]

print(f"Available days : {len(available_days)}", flush=True)
print(f"Date range     : {available_days[0]} to {available_days[-1]}", flush=True)

def get_cam(h, l, cl):
    rng = h - l
    return cl - rng*1.1/4, cl - rng*1.1/2

# Store ALL trades with their category
all_trades = []

start_time = datetime.now()
print(f"\nScanning all signals...", flush=True)

for day_idx, today in enumerate(available_days):
    if day_idx == 0: continue
    today_dt = datetime.strptime(today, '%Y-%m-%d')

    if day_idx % 10 == 0:
        elapsed = (datetime.now()-start_time).seconds
        print(f"  Day {day_idx}/{len(available_days)}: {today} | "
              f"Trades so far: {len(all_trades)} | Time: {elapsed}s", flush=True)
        time.sleep(0.05)

    date_min = (today_dt-timedelta(days=60)).strftime('%Y-%m-%d')
    date_max = (today_dt-timedelta(days=7)).strftime('%Y-%m-%d')

    # Load 15-min candles
    c15.execute('''
        SELECT symbol, datetime, open, high, low, close, volume
        FROM prices_15min WHERE DATE(datetime)=?
        ORDER BY symbol, datetime
    ''', (today,))
    all_today = c15.fetchall()
    sym_candles = defaultdict(list)
    for bar in all_today:
        sym_candles[bar['symbol']].append(bar)

    # Get HV candidates
    cd.execute('''
        SELECT d1.symbol, d1.high as hv_high, d1.low as hv_low,
               d1.volume as hv_vol
        FROM daily_prices d1
        WHERE d1.date>=? AND d1.date<=?
        AND d1.close>=(d1.low+(d1.high-d1.low)*0.5)
        AND d1.volume=(SELECT MAX(volume) FROM daily_prices d2
            WHERE d2.symbol=d1.symbol AND d2.date>=? AND d2.date<=?)
        AND d1.low>0 AND d1.volume>0
    ''', (date_min, date_max, date_min, date_max))
    candidates = [r for r in cd.fetchall()
                  if r['symbol'] in valid_symbols
                  and r['symbol'] not in bl]

    for row in candidates:
        sym     = row['symbol']
        hv_high = row['hv_high']
        hv_low  = row['hv_low']
        hv_vol  = row['hv_vol']

        # Yesterday's candle
        cd.execute('''
            SELECT high,low,close FROM daily_prices
            WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1
        ''', (sym, today))
        prev = cd.fetchone()
        if not prev: continue

        l3, l4 = get_cam(prev['high'], prev['low'], prev['close'])

        # Avg volume
        cd.execute('''
            SELECT AVG(volume) as av FROM (
                SELECT volume FROM daily_prices
                WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 20
            )
        ''', (sym, today))
        avg_vol = cd.fetchone()['av'] or 0
        if avg_vol == 0: continue

        # Base filters (non-candle)
        hv_vol_r = round(hv_vol/avg_vol, 2)
        if hv_vol_r > 10.0: continue
        if abs(l3-hv_low)/hv_low*100 > 4.0: continue
        if l3 < 50: continue
        risk = l3-l4; reward = hv_high-l3
        if risk<=0 or reward<=0: continue
        rr = round(reward/risk, 1)
        if not(5.0<=rr<=20.0): continue
        shares = min(int(1000/risk), int(20000/l3))
        if shares < 1: continue

        # Volume filter
        cd.execute('''
            SELECT open, close, volume FROM daily_prices
            WHERE symbol=? AND date=?
        ''', (sym, today))
        day_bar = cd.fetchone()
        if not day_bar: continue
        day_vol_r = round(day_bar['volume']/avg_vol, 2)
        if day_vol_r < 1.5: continue  # keep volume filter

        # EOD green/red
        eod_green = day_bar['close'] > day_bar['open']

        # Get 15-min candles
        candles = sym_candles.get(sym, [])
        if len(candles) < 2: continue

        # First candle green/red
        first_candle = candles[0]
        first_green  = first_candle['close'] > first_candle['open']

        # Second candle info (9:30 AM)
        second_candle = candles[1] if len(candles)>1 else None
        second_green  = (second_candle['close'] > second_candle['open']) \
                        if second_candle else False

        # Approach B entry (regardless of candle color)
        for ci, bar in enumerate(candles[:-1]):
            if bar['low'] > l3: continue

            next_bar  = candles[ci+1]
            next_open = next_bar['open']
            if next_open <= l3:        continue
            if next_open > l3*1.02:    continue
            if next_open >= hv_high:   continue
            if shares*next_open > 60000: continue

            # Entry time
            entry_ts   = next_bar['datetime'][11:16]
            entry_mins = int(entry_ts[:2])*60 + int(entry_ts[3:])

            # Check result
            c15.execute('''
                SELECT datetime, high, low
                FROM prices_15min WHERE symbol=? AND datetime>?
                ORDER BY datetime LIMIT 2000
            ''', (sym, next_bar['datetime']))
            future = c15.fetchall()

            result='OPEN'; pnl=0; days_held=0
            prev_date = today
            for fb in future:
                bar_date = fb['datetime'][:10]
                if bar_date != prev_date:
                    days_held += 1
                    prev_date  = bar_date
                if fb['low'] <= l4:
                    result='SL'
                    pnl=round((l4-next_open)*shares,0)
                    break
                if fb['high'] >= hv_high:
                    result='WIN'
                    pnl=round((hv_high-next_open)*shares,0)
                    break
            if result=='OPEN' and future:
                try:
                    pnl=round((future[-1]['close']-next_open)*shares,0)
                except:
                    pnl=0

            all_trades.append({
                'sym'         : sym,
                'date'        : today,
                'entry'       : next_open,
                'l3'          : l3,
                'l4'          : l4,
                'target'      : hv_high,
                'shares'      : shares,
                'rr'          : rr,
                'entry_ts'    : entry_ts,
                'entry_mins'  : entry_mins,
                'eod_green'   : eod_green,
                'first_green' : first_green,
                'second_green': second_green,
                'day_vol_r'   : day_vol_r,
                'result'      : result,
                'pnl'         : pnl,
                'days_held'   : days_held,
                'month'       : today[:7],
            })
            break

print(f"\nTotal trades found: {len(all_trades)}", flush=True)

# ── ANALYSIS ──────────────────────────────────────────────────
def analyse(trades, label):
    if not trades:
        print(f"  {label}: No trades", flush=True)
        return
    closed = [t for t in trades if t['result'] in ['WIN','SL']]
    if not closed: return
    wins   = [t for t in closed if t['result']=='WIN']
    sl     = [t for t in closed if t['result']=='SL']
    wr     = round(len(wins)/len(closed)*100,1)
    avg_w  = round(sum(t['pnl'] for t in wins)/len(wins),0) if wins else 0
    avg_l  = round(sum(t['pnl'] for t in sl)/len(sl),0) if sl else 0
    total  = round(sum(t['pnl'] for t in trades),0)
    avg_p  = round(total/len(closed),0)
    print(f"  {label:<40} Trades={len(closed):>4} WR={wr:>5}% "
          f"AvgWin={avg_w:>+7,.0f} AvgLoss={avg_l:>+7,.0f} "
          f"AvgPnL={avg_p:>+7,.0f} Total={total:>+10,.0f}", flush=True)

print(f"\n{'='*90}", flush=True)
print(f"  ANALYSIS 1 — EOD GREEN vs FIRST CANDLE GREEN", flush=True)
print(f"{'='*90}", flush=True)
print(f"\n  {'Filter':<40} {'Trades':>6} {'WR%':>5} {'AvgWin':>8} {'AvgLoss':>8} {'AvgPnL':>8} {'Total':>11}", flush=True)
print(f"  {'-'*90}", flush=True)

# 4 categories
cats = {
    'A: EOD✅ + 1st candle✅ (both green)' :
        [t for t in all_trades if t['eod_green'] and t['first_green']],
    'B: EOD✅ + 1st candle❌ (eod only)  ' :
        [t for t in all_trades if t['eod_green'] and not t['first_green']],
    'C: EOD❌ + 1st candle✅ (1st only)  ' :
        [t for t in all_trades if not t['eod_green'] and t['first_green']],
    'D: EOD❌ + 1st candle❌ (both red)  ' :
        [t for t in all_trades if not t['eod_green'] and not t['first_green']],
}
for label, trades in cats.items():
    analyse(trades, label)

# Combined filters
print(f"\n  {'Combined Filter':<40} {'Trades':>6} {'WR%':>5} {'AvgWin':>8} {'AvgLoss':>8} {'AvgPnL':>8} {'Total':>11}", flush=True)
print(f"  {'-'*90}", flush=True)

combos = {
    'BASELINE: EOD green only'             : [t for t in all_trades if t['eod_green']],
    'NEW: First candle green only'         : [t for t in all_trades if t['first_green']],
    'NEW: 1st OR 2nd candle green'         : [t for t in all_trades if t['first_green'] or t['second_green']],
    'STRICT: Both EOD + 1st green'         : [t for t in all_trades if t['eod_green'] and t['first_green']],
    'ALL trades (no green filter)'         : all_trades,
}
for label, trades in combos.items():
    analyse(trades, label)

# Entry time analysis
print(f"\n{'='*90}", flush=True)
print(f"  ANALYSIS 2 — ENTRY TIME", flush=True)
print(f"{'='*90}", flush=True)
print(f"\n  {'Time':<15} {'Trades':>6} {'WR%':>5} {'AvgPnL':>8} {'Total':>11}", flush=True)
print(f"  {'-'*50}", flush=True)
time_buckets = [
    ('9:15-9:30',  9*60+15, 9*60+30),
    ('9:30-10:00', 9*60+30,10*60+ 0),
    ('10:00-11:00',10*60+0, 11*60+ 0),
    ('11:00-13:00',11*60+0, 13*60+ 0),
    ('13:00-15:30',13*60+0, 15*60+30),
]
for label, t_min, t_max in time_buckets:
    b = [t for t in all_trades
         if t_min<=t['entry_mins']<t_max
         and t['result'] in ['WIN','SL']]
    if not b: continue
    w   = [t for t in b if t['result']=='WIN']
    wr  = round(len(w)/len(b)*100,1)
    avg = round(sum(t['pnl'] for t in b)/len(b),0)
    tot = round(sum(t['pnl'] for t in b),0)
    print(f"  {label:<15} {len(b):>6} {wr:>4}% {avg:>+8,.0f} {tot:>+11,.0f}", flush=True)

# Monthly breakdown
print(f"\n{'='*90}", flush=True)
print(f"  ANALYSIS 3 — MONTHLY (First Candle Green filter)", flush=True)
print(f"{'='*90}", flush=True)
monthly = defaultdict(lambda:{'trades':0,'wins':0,'pnl':0.0})
for t in [x for x in all_trades if x['first_green']]:
    if t['result'] not in ['WIN','SL']: continue
    monthly[t['month']]['trades'] += 1
    monthly[t['month']]['pnl']    += t['pnl']
    if t['result']=='WIN': monthly[t['month']]['wins'] += 1
print(f"\n  {'Month':<10} {'Trades':>7} {'Wins':>6} {'WR%':>6} {'P&L':>12}", flush=True)
print(f"  {'-'*45}", flush=True)
total_t=total_w=total_pnl=0
for month in sorted(monthly.keys()):
    m  = monthly[month]
    wr = round(m['wins']/m['trades']*100,1) if m['trades']>0 else 0
    total_t+=m['trades']; total_w+=m['wins']; total_pnl+=m['pnl']
    print(f"  {month:<10} {m['trades']:>7} {m['wins']:>6} {wr:>5}% {m['pnl']:>+12,.0f}", flush=True)
print(f"  {'-'*45}", flush=True)
wr_t=round(total_w/total_t*100,1) if total_t>0 else 0
print(f"  {'TOTAL':<10} {total_t:>7} {total_w:>6} {wr_t:>5}% {total_pnl:>+12,.0f}", flush=True)

# Final summary
print(f"\n{'='*90}", flush=True)
print(f"  FINAL ANSWER", flush=True)
print(f"{'='*90}", flush=True)

eod_trades = [t for t in all_trades if t['eod_green'] and t['result'] in ['WIN','SL']]
fc_trades  = [t for t in all_trades if t['first_green'] and t['result'] in ['WIN','SL']]

eod_wr  = round(sum(1 for t in eod_trades if t['result']=='WIN')/len(eod_trades)*100,1) if eod_trades else 0
fc_wr   = round(sum(1 for t in fc_trades  if t['result']=='WIN')/len(fc_trades)*100,1)  if fc_trades  else 0
eod_pnl = round(sum(t['pnl'] for t in eod_trades),0)
fc_pnl  = round(sum(t['pnl'] for t in fc_trades),0)

print(f"\n  EOD green filter  : {len(eod_trades):>4} trades | {eod_wr:>5}% WR | Rs{eod_pnl:>+9,.0f}", flush=True)
print(f"  1st candle green  : {len(fc_trades):>4} trades | {fc_wr:>5}% WR | Rs{fc_pnl:>+9,.0f}", flush=True)
print(f"\n  Can 1st candle green REPLACE EOD green?", flush=True)
if fc_wr >= eod_wr * 0.85 and fc_pnl >= eod_pnl * 0.85:
    print(f"  ✅ YES! Similar results — use 1st candle green in live system!", flush=True)
elif fc_wr >= eod_wr * 0.70:
    print(f"  ⚠️  PARTIALLY — lower WR but workable in live system", flush=True)
else:
    print(f"  ❌ NO — 1st candle green much worse than EOD green", flush=True)

elapsed=(datetime.now()-start_time).seconds
print(f"\n  Total time: {elapsed}s", flush=True)
conn_d.close()
conn_15.close()
print("\nDone!", flush=True)
