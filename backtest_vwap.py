"""
VWAP BACKTEST
Compare 4 approaches:
  A: EOD green only (baseline)
  B: EOD green + close > daily VWAP
  C: EOD green + close > anchored VWAP (from HV day)
  D: close > daily VWAP only (replace green candle)

VWAP calculated from 15-min data
"""
import sqlite3, json, time
from datetime import datetime, timedelta
from collections import defaultdict

print("="*70, flush=True)
print("  VWAP BACKTEST", flush=True)
print("  4 scenarios | Mar-May 2026 | 15-min data", flush=True)
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

def get_cam(h,l,cl):
    rng=h-l
    return cl-rng*1.1/4, cl-rng*1.1/2

def calc_daily_vwap(sym, date_str, sym_candles):
    """Calculate VWAP for a stock on a given day from 15-min candles"""
    candles = sym_candles.get(sym, [])
    if not candles: return None
    total_pv = 0
    total_v  = 0
    for bar in candles:
        if bar['datetime'][:10] != date_str: continue
        typical_price = (bar['high'] + bar['low'] + bar['close']) / 3
        total_pv += typical_price * bar['volume']
        total_v  += bar['volume']
    return round(total_pv/total_v, 2) if total_v > 0 else None

def calc_anchored_vwap(sym, anchor_date, today_str):
    """Calculate anchored VWAP from HV day to today"""
    c15.execute('''
        SELECT open, high, low, close, volume, datetime
        FROM prices_15min
        WHERE symbol=? AND DATE(datetime)>=? AND DATE(datetime)<=?
        ORDER BY datetime
    ''', (sym, anchor_date, today_str))
    candles = c15.fetchall()
    if not candles: return None
    total_pv = 0
    total_v  = 0
    for bar in candles:
        typical = (bar['high']+bar['low']+bar['close'])/3
        total_pv += typical * bar['volume']
        total_v  += bar['volume']
    return round(total_pv/total_v, 2) if total_v > 0 else None

# Store all trades with category info
all_trades = []

print(f"\nScanning signals with VWAP calculations...", flush=True)
start = datetime.now()

for day_idx, today in enumerate(available_days):
    if day_idx==0: continue
    today_dt = datetime.strptime(today,'%Y-%m-%d')

    if day_idx%10==0:
        elapsed=(datetime.now()-start).seconds
        print(f"  Day {day_idx}/59: {today} | "
              f"Trades: {len(all_trades)} | Time: {elapsed}s", flush=True)
        time.sleep(0.05)

    date_min=(today_dt-timedelta(days=60)).strftime('%Y-%m-%d')
    date_max=(today_dt-timedelta(days=7)).strftime('%Y-%m-%d')

    # Load 15-min candles for today
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
               d1.volume as hv_vol, d1.date as hv_date
        FROM daily_prices d1
        WHERE d1.date>=? AND d1.date<=?
        AND d1.close>=(d1.low+(d1.high-d1.low)*0.5)
        AND d1.volume=(SELECT MAX(volume) FROM daily_prices d2
            WHERE d2.symbol=d1.symbol AND d2.date>=? AND d2.date<=?)
        AND d1.low>0 AND d1.volume>0
    ''',(date_min,date_max,date_min,date_max))
    candidates=[r for r in cd.fetchall()
                if r['symbol'] in valid_symbols and r['symbol'] not in bl]

    for row in candidates:
        sym     = row['symbol']
        hv_high = row['hv_high']
        hv_low  = row['hv_low']
        hv_vol  = row['hv_vol']
        hv_date = row['hv_date']

        cd.execute('SELECT high,low,close FROM daily_prices WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1',(sym,today))
        prev=cd.fetchone()
        if not prev: continue

        l3,l4=get_cam(prev['high'],prev['low'],prev['close'])
        cd.execute('SELECT AVG(volume) as av FROM (SELECT volume FROM daily_prices WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 20)',(sym,today))
        avg_vol=cd.fetchone()['av'] or 0
        if avg_vol==0: continue

        hv_vol_r=round(hv_vol/avg_vol,2)
        if hv_vol_r>10: continue
        if abs(l3-hv_low)/hv_low*100>4: continue
        if l3<50: continue
        risk=l3-l4; reward=hv_high-l3
        if risk<=0 or reward<=0: continue
        rr=round(reward/risk,1)
        if not(5<=rr<=20): continue
        shares=min(int(1000/risk),int(20000/l3))
        if shares<1: continue

        # EOD daily candle
        cd.execute('SELECT open,close,volume FROM daily_prices WHERE symbol=? AND date=?',(sym,today))
        day_bar=cd.fetchone()
        if not day_bar: continue
        day_vol_r=round(day_bar['volume']/avg_vol,2)
        if day_vol_r<1.5: continue

        eod_green = day_bar['close'] > day_bar['open']

        # Calculate daily VWAP
        daily_vwap = calc_daily_vwap(sym, today, sym_candles)

        # Calculate anchored VWAP from HV day
        anchored_vwap = calc_anchored_vwap(sym, hv_date, today)

        # VWAP checks
        above_daily_vwap    = (day_bar['close'] > daily_vwap) if daily_vwap else None
        above_anchored_vwap = (day_bar['close'] > anchored_vwap) if anchored_vwap else None

        # Approach B entry
        candles=sym_candles.get(sym,[])
        if len(candles)<2: continue
        filled=False; fill_dt=None; ep=0
        for ci,bar in enumerate(candles[:-1]):
            if bar['low']>l3: continue
            nb=candles[ci+1]
            if nb['open']<=l3: continue
            if nb['open']>l3*1.02: continue
            if nb['open']>=hv_high: continue
            filled=True; fill_dt=nb['datetime']; ep=nb['open']
            break
        if not filled: continue

        # Get result
        c15.execute('''
            SELECT datetime,high,low,close FROM prices_15min
            WHERE symbol=? AND datetime>? ORDER BY datetime LIMIT 2000
        ''',(sym,fill_dt))
        future=c15.fetchall()
        result='OPEN'; pnl=0; days=0; prev_d=today
        for fb in future:
            bd=fb['datetime'][:10]
            if bd!=prev_d: days+=1; prev_d=bd
            if fb['low']<=l4: result='SL'; pnl=round((l4-ep)*shares,0); break
            if fb['high']>=hv_high: result='WIN'; pnl=round((hv_high-ep)*shares,0); break
        if result=='OPEN' and future:
            pnl=round((future[-1]['close']-ep)*shares,0)

        all_trades.append({
            'sym'             : sym,
            'date'            : today,
            'entry'           : ep,
            'l3'              : l3,
            'l4'              : l4,
            'target'          : hv_high,
            'shares'          : shares,
            'rr'              : rr,
            'result'          : result,
            'pnl'             : pnl,
            'days_held'       : days,
            'month'           : today[:7],
            'eod_green'       : eod_green,
            'daily_vwap'      : daily_vwap,
            'anchored_vwap'   : anchored_vwap,
            'above_daily_vwap': above_daily_vwap,
            'above_anchored_vwap': above_anchored_vwap,
            'hv_date'         : hv_date,
            'hv_age'          : (today_dt-datetime.strptime(hv_date,'%Y-%m-%d')).days,
        })

print(f"\nTotal trades: {len(all_trades)}", flush=True)

# ── ANALYSIS ──────────────────────────────────────────────────
def analyse(trades, label):
    closed=[t for t in trades if t['result'] in ['WIN','SL']]
    if not closed:
        print(f"  {label:<50}: No trades", flush=True)
        return 0, 0
    wins=[t for t in closed if t['result']=='WIN']
    sl  =[t for t in closed if t['result']=='SL']
    wr  =round(len(wins)/len(closed)*100,1)
    total=round(sum(t['pnl'] for t in trades),0)
    avg_w=round(sum(t['pnl'] for t in wins)/len(wins),0) if wins else 0
    avg_l=round(sum(t['pnl'] for t in sl)/len(sl),0) if sl else 0
    wl   =round(abs(avg_w/avg_l),1) if avg_l and avg_w else 0
    print(f"  {label:<50} T={len(closed):>4} WR={wr:>5}% "
          f"AvgW={avg_w:>+7,.0f} AvgL={avg_l:>+7,.0f} "
          f"W:L={wl:>4} P&L={total:>+10,.0f}", flush=True)
    return wr, total

print(f"\n{'='*100}", flush=True)
print(f"  VWAP BACKTEST RESULTS", flush=True)
print(f"{'='*100}", flush=True)
print(f"\n  {'Filter':<50} {'T':>5} {'WR%':>5} {'AvgWin':>8} {'AvgLoss':>8} {'W:L':>5} {'P&L':>11}", flush=True)
print(f"  {'-'*95}", flush=True)

# Baseline scenarios
scenarios = {
    'A: EOD green only (baseline)' :
        [t for t in all_trades if t['eod_green']],
    'B: No filter (reality)' :
        all_trades,
    'C: Close > Daily VWAP only' :
        [t for t in all_trades if t['above_daily_vwap']],
    'D: EOD green + close > Daily VWAP' :
        [t for t in all_trades if t['eod_green'] and t['above_daily_vwap']],
    'E: EOD green + close > Anchored VWAP' :
        [t for t in all_trades if t['eod_green'] and t['above_anchored_vwap']],
    'F: Close > Anchored VWAP only' :
        [t for t in all_trades if t['above_anchored_vwap']],
    'G: Close > Daily VWAP + close > Anchored VWAP':
        [t for t in all_trades if t['above_daily_vwap'] and t['above_anchored_vwap']],
    'H: EOD green + both VWAPs' :
        [t for t in all_trades if t['eod_green']
         and t['above_daily_vwap'] and t['above_anchored_vwap']],
}

best_pnl=0; best_label=''
for label, trades in scenarios.items():
    wr, total = analyse(trades, label)
    if total > best_pnl:
        best_pnl   = total
        best_label = label

# Monthly breakdown for top scenarios
print(f"\n{'='*100}", flush=True)
print(f"  MONTHLY BREAKDOWN — KEY SCENARIOS", flush=True)
print(f"{'='*100}", flush=True)

for label, filter_key in [
    ('A: EOD green (baseline)', lambda t: t['eod_green']),
    ('D: EOD green + Daily VWAP', lambda t: t['eod_green'] and t['above_daily_vwap']),
    ('E: EOD green + Anchored VWAP', lambda t: t['eod_green'] and t['above_anchored_vwap']),
    ('H: EOD green + both VWAPs', lambda t: t['eod_green'] and t['above_daily_vwap'] and t['above_anchored_vwap']),
]:
    trades = [t for t in all_trades if filter_key(t)]
    closed = [t for t in trades if t['result'] in ['WIN','SL']]
    if not closed: continue

    print(f"\n  {label}", flush=True)
    monthly=defaultdict(lambda:{'t':0,'w':0,'pnl':0.0})
    for t in trades:
        if t['result'] not in ['WIN','SL']: continue
        monthly[t['month']]['t']+=1
        monthly[t['month']]['pnl']+=t['pnl']
        if t['result']=='WIN': monthly[t['month']]['w']+=1
    total_t=total_w=0; total_pnl=0
    for m in sorted(monthly.keys()):
        mm=monthly[m]
        wr_m=round(mm['w']/mm['t']*100,1) if mm['t']>0 else 0
        total_t+=mm['t']; total_w+=mm['w']; total_pnl+=mm['pnl']
        print(f"    {m}: T={mm['t']:>3} W={mm['w']:>3} "
              f"WR={wr_m:>5}% P&L=Rs{mm['pnl']:>+9,.0f}", flush=True)
    wr_t=round(total_w/total_t*100,1) if total_t>0 else 0
    print(f"    TOTAL: T={total_t:>3} W={total_w:>3} "
          f"WR={wr_t:>5}% P&L=Rs{total_pnl:>+9,.0f}", flush=True)

# VWAP accuracy analysis
print(f"\n{'='*100}", flush=True)
print(f"  VWAP ACCURACY ANALYSIS", flush=True)
print(f"{'='*100}", flush=True)

# How often is close > VWAP when EOD green?
eod_green_trades = [t for t in all_trades if t['eod_green']]
if eod_green_trades:
    above_dvwap = sum(1 for t in eod_green_trades if t['above_daily_vwap'])
    above_avwap = sum(1 for t in eod_green_trades if t['above_anchored_vwap'])
    total_g     = len(eod_green_trades)
    print(f"\n  When EOD green ({total_g} trades):", flush=True)
    print(f"    Also above Daily VWAP   : {above_dvwap} ({round(above_dvwap/total_g*100,1)}%)", flush=True)
    print(f"    Also above Anchored VWAP: {above_avwap} ({round(above_avwap/total_g*100,1)}%)", flush=True)

# VWAP vs price at entry
print(f"\n  VWAP levels at entry (EOD green trades):", flush=True)
for t in [x for x in all_trades if x['eod_green'] and x['above_daily_vwap'] is not None][:10]:
    dvwap_diff = round((t['entry']-t['daily_vwap'])/t['daily_vwap']*100,2) if t['daily_vwap'] else 0
    avwap_diff = round((t['entry']-t['anchored_vwap'])/t['anchored_vwap']*100,2) if t['anchored_vwap'] else 0
    print(f"    {t['sym']:<12} {t['date']} | "
          f"Entry={t['entry']:>8} | "
          f"DVWAP={t['daily_vwap']:>8} ({dvwap_diff:>+.1f}%) | "
          f"AVWAP={t['anchored_vwap']:>8} ({avwap_diff:>+.1f}%) | "
          f"{t['result']:<5} Rs{t['pnl']:>+6,.0f}", flush=True)

# Final answer
print(f"\n{'='*100}", flush=True)
print(f"  FINAL ANSWER — DOES VWAP HELP?", flush=True)
print(f"{'='*100}", flush=True)

baseline = [t for t in all_trades if t['eod_green']]
d_vwap   = [t for t in all_trades if t['eod_green'] and t['above_daily_vwap']]
a_vwap   = [t for t in all_trades if t['eod_green'] and t['above_anchored_vwap']]
both     = [t for t in all_trades if t['eod_green'] and t['above_daily_vwap'] and t['above_anchored_vwap']]

def quick_stats(trades):
    closed=[t for t in trades if t['result'] in ['WIN','SL']]
    if not closed: return 0,0,0
    wins=[t for t in closed if t['result']=='WIN']
    wr=round(len(wins)/len(closed)*100,1)
    total=round(sum(t['pnl'] for t in trades),0)
    return len(closed), wr, total

b_t,b_wr,b_pnl   = quick_stats(baseline)
d_t,d_wr,d_pnl   = quick_stats(d_vwap)
a_t,a_wr,a_pnl   = quick_stats(a_vwap)
bt_t,bt_wr,bt_pnl = quick_stats(both)

print(f"\n  {'Approach':<45} {'Trades':>7} {'WR%':>6} {'P&L':>12} {'vs Baseline':>12}", flush=True)
print(f"  {'-'*85}", flush=True)
print(f"  {'Baseline (EOD green)':<45} {b_t:>7} {b_wr:>5}% {b_pnl:>+12,.0f} {'—':>12}", flush=True)
print(f"  {'+ Daily VWAP filter':<45} {d_t:>7} {d_wr:>5}% {d_pnl:>+12,.0f} {d_pnl-b_pnl:>+12,.0f}", flush=True)
print(f"  {'+ Anchored VWAP filter':<45} {a_t:>7} {a_wr:>5}% {a_pnl:>+12,.0f} {a_pnl-b_pnl:>+12,.0f}", flush=True)
print(f"  {'+ Both VWAPs filter':<45} {bt_t:>7} {bt_wr:>5}% {bt_pnl:>+12,.0f} {bt_pnl-b_pnl:>+12,.0f}", flush=True)

print(f"\n  VERDICT:", flush=True)
if d_pnl > b_pnl and d_wr > b_wr:
    print(f"  ✅ Daily VWAP HELPS! Better WR and P&L!", flush=True)
elif d_pnl > b_pnl:
    print(f"  ⚠️ Daily VWAP gives more P&L but lower WR (more trades)", flush=True)
else:
    print(f"  ❌ Daily VWAP does NOT help vs baseline", flush=True)

if a_pnl > b_pnl and a_wr > b_wr:
    print(f"  ✅ Anchored VWAP HELPS! Better WR and P&L!", flush=True)
elif a_pnl > b_pnl:
    print(f"  ⚠️ Anchored VWAP gives more P&L", flush=True)
else:
    print(f"  ❌ Anchored VWAP does NOT help vs baseline", flush=True)

elapsed=(datetime.now()-start).seconds
print(f"\n  Total time: {elapsed}s", flush=True)
conn_d.close(); conn_15.close()
print("\nDone!", flush=True)
