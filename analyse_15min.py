import sqlite3
from collections import defaultdict

print("Loading 15-min backtest results for R:R analysis...", flush=True)

# Read trade log from backtest results
# We'll analyse from the backtest_15min.py trade_log

conn_d  = sqlite3.connect('/home/ubuntu/nse-scanner/nse_data.db')
conn_d.row_factory  = sqlite3.Row
conn_15 = sqlite3.connect('/home/ubuntu/nse-scanner/intraday_15min.db')
conn_15.row_factory = sqlite3.Row
cd  = conn_d.cursor()
c15 = conn_15.cursor()

from datetime import datetime, timedelta
import json

bl = json.load(open('cautionary_stocks.json')).get('stocks',[])
cd.execute('SELECT symbol FROM daily_prices GROUP BY symbol HAVING COUNT(*) >= 252')
valid_symbols = {r['symbol'] for r in cd.fetchall()}

c15.execute("SELECT DISTINCT DATE(datetime) as dt FROM prices_15min ORDER BY dt")
available_days = [r['dt'] for r in c15.fetchall()]

def get_cam(h, l, cl):
    rng = h - l
    return cl - rng*1.1/4, cl - rng*1.1/2

# ── Full analysis ─────────────────────────────────────────────
all_trades = []

for day_idx, today in enumerate(available_days):
    if day_idx == 0: continue
    today_dt = datetime.strptime(today, '%Y-%m-%d')
    date_min = (today_dt - timedelta(days=60)).strftime('%Y-%m-%d')
    date_max = (today_dt - timedelta(days=7)).strftime('%Y-%m-%d')

    cd.execute('''
        SELECT d1.symbol, d1.high as hv_high, d1.low as hv_low, d1.volume as hv_vol
        FROM daily_prices d1
        WHERE d1.date>=? AND d1.date<=?
        AND d1.close>=(d1.low+(d1.high-d1.low)*0.5)
        AND d1.volume=(SELECT MAX(volume) FROM daily_prices d2
            WHERE d2.symbol=d1.symbol AND d2.date>=? AND d2.date<=?)
        AND d1.low>0 AND d1.volume>0
    ''', (date_min, date_max, date_min, date_max))
    candidates = [r for r in cd.fetchall()
                  if r['symbol'] in valid_symbols and r['symbol'] not in bl]

    for row in candidates:
        sym=row['symbol']; hv_high=row['hv_high']
        hv_low=row['hv_low']; hv_vol=row['hv_vol']

        cd.execute('SELECT high,low,close FROM daily_prices WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1',(sym,today))
        prev = cd.fetchone()
        if not prev: continue

        l3,l4 = get_cam(prev['high'],prev['low'],prev['close'])

        cd.execute('SELECT AVG(volume) as av FROM (SELECT volume FROM daily_prices WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 20)',(sym,today))
        avg_vol = cd.fetchone()['av'] or 0
        if avg_vol == 0: continue

        hv_vol_r = round(hv_vol/avg_vol,2)
        if hv_vol_r > 10.0: continue
        if abs(l3-hv_low)/hv_low*100 > 4.0: continue
        if l3 < 50: continue

        risk=l3-l4; reward=hv_high-l3
        if risk<=0 or reward<=0: continue
        rr=round(reward/risk,1)
        if not(5.0<=rr<=20.0): continue

        shares=min(int(1000/risk),int(20000/l3))
        if shares<1: continue

        # Get 15-min candles for entry day
        c15.execute('SELECT COUNT(*) as cnt FROM prices_15min WHERE symbol=? AND DATE(datetime)=?',(sym,today))
        if c15.fetchone()['cnt'] == 0: continue

        c15.execute('''
            SELECT datetime, open, high, low, close, volume
            FROM prices_15min WHERE symbol=? AND DATE(datetime)>=?
            ORDER BY datetime LIMIT 500
        ''', (sym, today))
        all_candles = c15.fetchall()

        # Find fill
        filled=False; fill_dt=None; fill_candle_idx=0
        today_candles = [c for c in all_candles if c['datetime'][:10]==today]

        for ci, bar in enumerate(today_candles):
            if bar['low'] <= l3:
                filled=True
                fill_dt=bar['datetime'][:16]
                fill_candle_idx=ci
                fill_hour=int(bar['datetime'][11:13])
                fill_min =int(bar['datetime'][14:16])
                fill_time_mins = fill_hour*60 + fill_min
                # Volume at fill time
                fill_vol = bar['volume']
                break

        if not filled: continue

        # Check daily candle green/red/volume
        cd.execute('SELECT open,close,volume FROM daily_prices WHERE symbol=? AND date=?',(sym,today))
        day_bar = cd.fetchone()
        if not day_bar: continue
        day_green = day_bar['close'] > day_bar['open']
        day_vol_r = round(day_bar['volume']/avg_vol,2)

        # Find exit across multiple days
        result='OPEN'; exit_dt=None; days_held=0; exit_price=0

        # Get all candles from fill point onwards (across days)
        c15.execute('''
            SELECT datetime, high, low, close
            FROM prices_15min WHERE symbol=? AND datetime>?
            ORDER BY datetime LIMIT 2000
        ''', (sym, fill_dt))
        future_candles = c15.fetchall()

        prev_date = today
        for bar in future_candles:
            bar_date = bar['datetime'][:10]
            if bar_date != prev_date:
                days_held += 1
                prev_date = bar_date

            if bar['low'] <= l4:
                result='SL'; exit_dt=bar['datetime'][:16]
                exit_price=l4; break
            if bar['high'] >= hv_high:
                result='WIN'; exit_dt=bar['datetime'][:16]
                exit_price=hv_high; break

        if result=='OPEN':
            # Use last available price
            if future_candles:
                exit_price=future_candles[-1]['close']
                exit_dt=future_candles[-1]['datetime'][:16]
            else:
                exit_price=l3

        pnl = round((exit_price-l3)*shares,0)

        all_trades.append({
            'sym'        : sym,
            'date'       : today,
            'l3'         : l3,
            'l4'         : l4,
            'target'     : hv_high,
            'shares'     : shares,
            'rr'         : rr,
            'fill_dt'    : fill_dt,
            'fill_time'  : fill_time_mins,
            'fill_vol'   : fill_vol,
            'day_green'  : day_green,
            'day_vol_r'  : day_vol_r,
            'result'     : result,
            'exit_dt'    : exit_dt,
            'days_held'  : days_held,
            'pnl'        : pnl,
            'month'      : today[:7],
            'hv_vol_r'   : hv_vol_r,
        })

print(f"Total filled trades: {len(all_trades)}", flush=True)

# ── Analysis 1: R:R Breakdown ─────────────────────────────────
print(f"\n{'='*75}", flush=True)
print(f"  ANALYSIS 1 — R:R BREAKDOWN", flush=True)
print(f"{'='*75}", flush=True)

rr_buckets = [
    ('5-7x',   5.0,  7.0),
    ('7-10x',  7.0, 10.0),
    ('10-15x',10.0, 15.0),
    ('15-20x',15.0, 20.0),
]

print(f"  {'R:R Range':<12} {'Trades':>7} {'Wins':>6} {'SL':>5} {'WR%':>6} {'AvgWin':>9} {'AvgLoss':>9} {'AvgPnL':>9} {'TotalPnL':>11}", flush=True)
print(f"  {'-'*80}", flush=True)

for label, rr_min, rr_max in rr_buckets:
    bucket = [t for t in all_trades if rr_min<=t['rr']<rr_max]
    if not bucket: continue
    wins   = [t for t in bucket if t['result']=='WIN']
    sl     = [t for t in bucket if t['result']=='SL']
    wr     = round(len(wins)/len(bucket)*100,1)
    avg_w  = round(sum(t['pnl'] for t in wins)/len(wins),0) if wins else 0
    avg_l  = round(sum(t['pnl'] for t in sl)/len(sl),0) if sl else 0
    avg_p  = round(sum(t['pnl'] for t in bucket)/len(bucket),0)
    tot_p  = round(sum(t['pnl'] for t in bucket),0)
    print(f"  {label:<12} {len(bucket):>7} {len(wins):>6} {len(sl):>5} {wr:>5}% {avg_w:>+9,.0f} {avg_l:>+9,.0f} {avg_p:>+9,.0f} {tot_p:>+11,.0f}", flush=True)

# ── Analysis 2: Entry Time Breakdown ─────────────────────────
print(f"\n{'='*75}", flush=True)
print(f"  ANALYSIS 2 — ENTRY TIME BREAKDOWN", flush=True)
print(f"{'='*75}", flush=True)

time_buckets = [
    ('9:15-9:30',  9*60+15, 9*60+30),
    ('9:30-10:00', 9*60+30,10*60+ 0),
    ('10:00-11:00',10*60+0, 11*60+ 0),
    ('11:00-12:00',11*60+0, 12*60+ 0),
    ('12:00-14:00',12*60+0, 14*60+ 0),
    ('14:00-15:30',14*60+0, 15*60+30),
]

print(f"  {'Time':<14} {'Trades':>7} {'Wins':>6} {'SL':>5} {'WR%':>6} {'AvgPnL':>9} {'TotalPnL':>11}", flush=True)
print(f"  {'-'*65}", flush=True)

for label, t_min, t_max in time_buckets:
    bucket = [t for t in all_trades if t_min<=t['fill_time']<t_max]
    if not bucket: continue
    wins = [t for t in bucket if t['result']=='WIN']
    sl   = [t for t in bucket if t['result']=='SL']
    wr   = round(len(wins)/len(bucket)*100,1)
    avg_p= round(sum(t['pnl'] for t in bucket)/len(bucket),0)
    tot_p= round(sum(t['pnl'] for t in bucket),0)
    print(f"  {label:<14} {len(bucket):>7} {len(wins):>6} {len(sl):>5} {wr:>5}% {avg_p:>+9,.0f} {tot_p:>+11,.0f}", flush=True)

# ── Analysis 3: Green vs Red Candle ──────────────────────────
print(f"\n{'='*75}", flush=True)
print(f"  ANALYSIS 3 — GREEN vs RED DAILY CANDLE", flush=True)
print(f"{'='*75}", flush=True)

for label, green_val in [('GREEN candle',True),('RED candle',False)]:
    bucket = [t for t in all_trades if t['day_green']==green_val]
    if not bucket: continue
    wins = [t for t in bucket if t['result']=='WIN']
    sl   = [t for t in bucket if t['result']=='SL']
    wr   = round(len(wins)/len(bucket)*100,1)
    avg_w= round(sum(t['pnl'] for t in wins)/len(wins),0) if wins else 0
    avg_l= round(sum(t['pnl'] for t in sl)/len(sl),0) if sl else 0
    avg_p= round(sum(t['pnl'] for t in bucket)/len(bucket),0)
    tot_p= round(sum(t['pnl'] for t in bucket),0)
    print(f"  {label:<15} Trades:{len(bucket):>5} WR:{wr:>5}% AvgWin:{avg_w:>+8,.0f} AvgLoss:{avg_l:>+8,.0f} AvgPnL:{avg_p:>+8,.0f} Total:{tot_p:>+10,.0f}", flush=True)

# ── Analysis 4: Volume at Entry ───────────────────────────────
print(f"\n{'='*75}", flush=True)
print(f"  ANALYSIS 4 — DAILY VOLUME RATIO AT ENTRY", flush=True)
print(f"{'='*75}", flush=True)

vol_buckets = [
    ('1.5-2x',  1.5, 2.0),
    ('2-3x',    2.0, 3.0),
    ('3-5x',    3.0, 5.0),
    ('5x+',     5.0, 999),
]

print(f"  {'Vol Range':<12} {'Trades':>7} {'Wins':>6} {'SL':>5} {'WR%':>6} {'AvgPnL':>9} {'TotalPnL':>11}", flush=True)
print(f"  {'-'*65}", flush=True)

for label, v_min, v_max in vol_buckets:
    bucket = [t for t in all_trades if v_min<=t['day_vol_r']<v_max]
    if not bucket: continue
    wins = [t for t in bucket if t['result']=='WIN']
    sl   = [t for t in bucket if t['result']=='SL']
    wr   = round(len(wins)/len(bucket)*100,1)
    avg_p= round(sum(t['pnl'] for t in bucket)/len(bucket),0)
    tot_p= round(sum(t['pnl'] for t in bucket),0)
    print(f"  {label:<12} {len(bucket):>7} {len(wins):>6} {len(sl):>5} {wr:>5}% {avg_p:>+9,.0f} {tot_p:>+11,.0f}", flush=True)

# ── Analysis 5: Days Held ─────────────────────────────────────
print(f"\n{'='*75}", flush=True)
print(f"  ANALYSIS 5 — DAYS HELD TO EXIT", flush=True)
print(f"{'='*75}", flush=True)

days_buckets = [
    ('Same day',  0, 1),
    ('Day 1',     1, 2),
    ('Day 2',     2, 3),
    ('Day 3',     3, 4),
    ('Day 4+',    4, 999),
]

print(f"  {'Hold Period':<14} {'Trades':>7} {'Wins':>6} {'SL':>5} {'WR%':>6} {'AvgPnL':>9} {'TotalPnL':>11}", flush=True)
print(f"  {'-'*65}", flush=True)

for label, d_min, d_max in days_buckets:
    bucket = [t for t in all_trades if d_min<=t['days_held']<d_max and t['result']!='OPEN']
    if not bucket: continue
    wins = [t for t in bucket if t['result']=='WIN']
    sl   = [t for t in bucket if t['result']=='SL']
    wr   = round(len(wins)/len(bucket)*100,1)
    avg_p= round(sum(t['pnl'] for t in bucket)/len(bucket),0)
    tot_p= round(sum(t['pnl'] for t in bucket),0)
    print(f"  {label:<14} {len(bucket):>7} {len(wins):>6} {len(sl):>5} {wr:>5}% {avg_p:>+9,.0f} {tot_p:>+11,.0f}", flush=True)

# ── Analysis 6: HV Age ────────────────────────────────────────
print(f"\n{'='*75}", flush=True)
print(f"  ANALYSIS 6 — HV AGE IMPACT", flush=True)
print(f"{'='*75}", flush=True)

# Need to add hv_age to trades — skip for now, use vol ratio as proxy

# ── Analysis 7: Best R:R + Time combo ────────────────────────
print(f"\n{'='*75}", flush=True)
print(f"  ANALYSIS 7 — BEST COMBINATION (R:R + Entry Time)", flush=True)
print(f"{'='*75}", flush=True)

combos = [
    ('RR>10 + Entry<10AM',   lambda t: t['rr']>=10 and t['fill_time']<10*60),
    ('RR>10 + Green candle', lambda t: t['rr']>=10 and t['day_green']),
    ('RR>10 + Vol>2x',       lambda t: t['rr']>=10 and t['day_vol_r']>=2.0),
    ('RR>8  + Green + Vol2x',lambda t: t['rr']>=8  and t['day_green'] and t['day_vol_r']>=2.0),
    ('RR>10 + Green + Vol2x',lambda t: t['rr']>=10 and t['day_green'] and t['day_vol_r']>=2.0),
    ('ALL filters (current)',lambda t: t['rr']>=5  and t['day_green'] and t['day_vol_r']>=1.5),
]

print(f"  {'Combo':<30} {'Trades':>7} {'Wins':>6} {'WR%':>6} {'AvgPnL':>9} {'TotalPnL':>11}", flush=True)
print(f"  {'-'*75}", flush=True)

for label, filter_fn in combos:
    bucket = [t for t in all_trades if filter_fn(t)]
    if not bucket: continue
    wins = [t for t in bucket if t['result']=='WIN']
    wr   = round(len(wins)/len(bucket)*100,1)
    avg_p= round(sum(t['pnl'] for t in bucket)/len(bucket),0)
    tot_p= round(sum(t['pnl'] for t in bucket),0)
    print(f"  {label:<30} {len(bucket):>7} {len(wins):>6} {wr:>5}% {avg_p:>+9,.0f} {tot_p:>+11,.0f}", flush=True)

# ── Monthly Summary ───────────────────────────────────────────
print(f"\n{'='*75}", flush=True)
print(f"  MONTHLY SUMMARY — 15-MIN BACKTEST", flush=True)
print(f"{'='*75}", flush=True)

monthly = defaultdict(lambda:{'trades':0,'wins':0,'sl':0,'pnl':0.0})
for t in all_trades:
    if t['result']=='OPEN': continue
    monthly[t['month']]['trades'] += 1
    monthly[t['month']]['pnl']    += t['pnl']
    if t['result']=='WIN': monthly[t['month']]['wins'] += 1
    elif t['result']=='SL': monthly[t['month']]['sl']  += 1

total_t=total_w=total_sl=total_pnl=0
print(f"  {'Month':<10} {'Trades':>7} {'Wins':>6} {'SL':>5} {'WR%':>6} {'P&L':>10}", flush=True)
print(f"  {'-'*55}", flush=True)
for month in sorted(monthly.keys()):
    m  = monthly[month]
    wr = round(m['wins']/m['trades']*100,1) if m['trades']>0 else 0
    total_t   += m['trades']
    total_w   += m['wins']
    total_sl  += m['sl']
    total_pnl += m['pnl']
    print(f"  {month:<10} {m['trades']:>7} {m['wins']:>6} {m['sl']:>5} {wr:>5}% {m['pnl']:>+10,.0f}", flush=True)

print(f"  {'-'*55}", flush=True)
wr_t = round(total_w/total_t*100,1) if total_t>0 else 0
print(f"  {'TOTAL':<10} {total_t:>7} {total_w:>6} {total_sl:>5} {wr_t:>5}% {total_pnl:>+10,.0f}", flush=True)

wins_l=[t for t in all_trades if t['result']=='WIN']
sl_l  =[t for t in all_trades if t['result']=='SL']
print(f"\n  Avg win : Rs {round(sum(t['pnl'] for t in wins_l)/len(wins_l),0):+,.0f}" if wins_l else "", flush=True)
print(f"  Avg loss: Rs {round(sum(t['pnl'] for t in sl_l)/len(sl_l),0):+,.0f}" if sl_l else "", flush=True)
print(f"  Win:Loss ratio: {round(abs(sum(t['pnl'] for t in wins_l)/len(wins_l) / (sum(t['pnl'] for t in sl_l)/len(sl_l))),1) if wins_l and sl_l else 'N/A'}:1", flush=True)

conn_d.close()
conn_15.close()
print("\nDone!", flush=True)
