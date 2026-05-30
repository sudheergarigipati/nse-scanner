"""
FULLY REALISTIC Backtest — No EOD Hindsight Filters
Only uses data available BEFORE entry time:
  ✅ Yesterday's daily candle (L3/L4)
  ✅ HV day (historical)
  ✅ HV alignment + RR
  ✅ 15-min candle L3 touch
  ✅ Next candle opens above L3 (within 2%)
  ❌ NO green candle filter (unknown at entry)
  ❌ NO EOD volume filter (unknown at entry)
"""
import sqlite3, json, time
from datetime import datetime, timedelta
from collections import defaultdict

print("="*70, flush=True)
print("  FULLY REALISTIC BACKTEST — NO EOD HINDSIGHT", flush=True)
print("  Only uses data available at entry time", flush=True)
print("  All stocks | Mar-May 2026 | 15-min data", flush=True)
print("="*70, flush=True)

conn_d  = sqlite3.connect('/home/ubuntu/nse-scanner/nse_data.db')
conn_15 = sqlite3.connect('/home/ubuntu/nse-scanner/intraday_15min.db')
conn_d.row_factory  = sqlite3.Row
conn_15.row_factory = sqlite3.Row
cd  = conn_d.cursor()
c15 = conn_15.cursor()

bl = json.load(open('/home/ubuntu/nse-scanner/cautionary_stocks.json')).get('stocks',[])
cd.execute('SELECT symbol FROM daily_prices GROUP BY symbol HAVING COUNT(*) >= 252')
valid_symbols = {r['symbol'] for r in cd.fetchall()}

c15.execute("SELECT DISTINCT DATE(datetime) as dt FROM prices_15min ORDER BY dt")
available_days = [r['dt'] for r in c15.fetchall()]

print(f"Available days : {len(available_days)}", flush=True)
print(f"Valid symbols  : {len(valid_symbols)}", flush=True)
print(f"Date range     : {available_days[0]} to {available_days[-1]}", flush=True)
print(f"\nFilters used:", flush=True)
print(f"  ✅ HV day (7-60 calendar days, bullish candle)", flush=True)
print(f"  ✅ L3/L4 from yesterday's daily candle", flush=True)
print(f"  ✅ HV alignment (L3 within 4% of HV Low)", flush=True)
print(f"  ✅ HV vol ratio <= 10x", flush=True)
print(f"  ✅ R:R between 5-20x", flush=True)
print(f"  ✅ Price >= Rs 50", flush=True)
print(f"  ✅ L3 touch on 15-min candle", flush=True)
print(f"  ✅ Next candle opens above L3 (within 2%)", flush=True)
print(f"  ❌ NO green candle filter", flush=True)
print(f"  ❌ NO EOD volume filter", flush=True)

def get_cam(h, l, cl):
    rng = h - l
    return cl - rng*1.1/4, cl - rng*1.1/2

# Config
CAPITAL     = 60000
MAX_POS     = 3
RISK        = 1000
MAX_POS_VAL = 20000
MIN_RR      = 5.0
MAX_RR      = 20.0
COOLDOWN    = 10

open_positions = {}
capital        = CAPITAL
trade_log      = []
sl_cooldown    = {}
monthly_stats  = defaultdict(lambda:{
    'trades':0,'wins':0,'sl':0,'open':0,'pnl':0.0
})

print(f"\nRunning backtest...", flush=True)
start_time = datetime.now()

for day_idx, today in enumerate(available_days):
    if day_idx == 0: continue
    today_dt = datetime.strptime(today, '%Y-%m-%d')
    month    = today[:7]

    if day_idx % 10 == 0:
        elapsed = (datetime.now()-start_time).seconds
        print(f"  Day {day_idx}/{len(available_days)}: {today} | "
              f"Capital: Rs{capital:,.0f} | "
              f"Open: {len(open_positions)} | "
              f"Trades: {len(trade_log)} | "
              f"Time: {elapsed}s", flush=True)
        time.sleep(0.05)

    # Load all 15-min candles for today
    c15.execute('''
        SELECT symbol, datetime, open, high, low, close, volume
        FROM prices_15min WHERE DATE(datetime)=?
        ORDER BY symbol, datetime
    ''', (today,))
    all_today = c15.fetchall()
    sym_candles = defaultdict(list)
    for bar in all_today:
        sym_candles[bar['symbol']].append(bar)

    # ── Step 1: Monitor open positions ───────────────────────
    to_close = []
    for sym, pos in list(open_positions.items()):
        candles = sym_candles.get(sym, [])
        for bar in candles:
            if bar['low'] <= pos['sl']:
                pnl = (pos['sl'] - pos['entry']) * pos['shares']
                capital += pos['shares'] * pos['entry'] + pnl
                monthly_stats[month]['sl']     += 1
                monthly_stats[month]['pnl']    += pnl
                monthly_stats[month]['trades'] += 1
                sl_cooldown[sym] = today
                to_close.append(sym)
                trade_log.append({
                    'sym'      : sym,
                    'entry_dt' : pos['entry_dt'],
                    'exit_dt'  : bar['datetime'][:16],
                    'entry'    : pos['entry'],
                    'exit'     : pos['sl'],
                    'result'   : 'SL',
                    'pnl'      : round(pnl, 0),
                    'shares'   : pos['shares'],
                    'rr'       : pos['rr'],
                    'month'    : pos['entry_dt'][:7],
                    'days_held': pos.get('days_held', 0),
                    'day_green': pos.get('day_green', None),
                    'day_vol_r': pos.get('day_vol_r', 0),
                })
                break
            elif bar['high'] >= pos['target']:
                pnl = (pos['target'] - pos['entry']) * pos['shares']
                capital += pos['shares'] * pos['entry'] + pnl
                monthly_stats[month]['wins']   += 1
                monthly_stats[month]['pnl']    += pnl
                monthly_stats[month]['trades'] += 1
                to_close.append(sym)
                trade_log.append({
                    'sym'      : sym,
                    'entry_dt' : pos['entry_dt'],
                    'exit_dt'  : bar['datetime'][:16],
                    'entry'    : pos['entry'],
                    'exit'     : pos['target'],
                    'result'   : 'WIN',
                    'pnl'      : round(pnl, 0),
                    'shares'   : pos['shares'],
                    'rr'       : pos['rr'],
                    'month'    : pos['entry_dt'][:7],
                    'days_held': pos.get('days_held', 0),
                    'day_green': pos.get('day_green', None),
                    'day_vol_r': pos.get('day_vol_r', 0),
                })
                break

    for sym in to_close:
        if sym in open_positions:
            del open_positions[sym]

    for sym in open_positions:
        open_positions[sym]['days_held'] = \
            open_positions[sym].get('days_held', 0) + 1

    # ── Step 2: Find new signals ──────────────────────────────
    date_min = (today_dt - timedelta(days=60)).strftime('%Y-%m-%d')
    date_max = (today_dt - timedelta(days=7)).strftime('%Y-%m-%d')

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

        if sym in open_positions: continue
        if len(open_positions) >= MAX_POS: continue

        if sym in sl_cooldown:
            sl_dt = datetime.strptime(sl_cooldown[sym], '%Y-%m-%d')
            if (today_dt - sl_dt).days < COOLDOWN: continue

        # Yesterday's candle → L3/L4
        cd.execute('''
            SELECT high,low,close FROM daily_prices
            WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1
        ''', (sym, today))
        prev = cd.fetchone()
        if not prev: continue

        l3, l4 = get_cam(prev['high'], prev['low'], prev['close'])

        # Avg volume (for HV ratio only — NOT for signal filter)
        cd.execute('''
            SELECT AVG(volume) as av FROM (
                SELECT volume FROM daily_prices
                WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 20
            )
        ''', (sym, today))
        avg_vol = cd.fetchone()['av'] or 0
        if avg_vol == 0: continue

        # ── Filters available at entry time ──────────────────
        hv_vol_r = round(hv_vol/avg_vol, 2)
        if hv_vol_r > 10.0: continue          # HV not extreme event
        if abs(l3-hv_low)/hv_low*100 > 4.0: continue  # L3 near HV Low
        if l3 < 50: continue                   # min price

        risk   = l3 - l4
        reward = hv_high - l3
        if risk <= 0 or reward <= 0: continue
        rr = round(reward/risk, 1)
        if not (MIN_RR <= rr <= MAX_RR): continue

        shares = min(int(RISK/risk), int(MAX_POS_VAL/l3))
        if shares < 1: continue

        # ── NO green candle filter ────────────────────────────
        # ── NO volume filter ─────────────────────────────────
        # Just record what actually happened (for analysis)
        cd.execute('''
            SELECT open, close, volume FROM daily_prices
            WHERE symbol=? AND date=?
        ''', (sym, today))
        day_bar = cd.fetchone()
        day_green = None
        day_vol_r = 0
        if day_bar:
            day_green = day_bar['close'] > day_bar['open']
            day_vol_r = round(day_bar['volume']/avg_vol, 2)

        # ── APPROACH B: next candle entry (15-min) ────────────
        candles = sym_candles.get(sym, [])
        if len(candles) < 2: continue

        for ci, bar in enumerate(candles[:-1]):
            if bar['low'] > l3: continue

            # L3 touched!
            next_bar  = candles[ci + 1]
            next_open = next_bar['open']

            # Entry validation
            if next_open <= l3:        continue
            if next_open > l3 * 1.02:  continue
            if next_open >= hv_high:   continue
            if shares * next_open > capital: continue

            # ✅ Enter!
            entry_price = next_open
            capital    -= shares * entry_price
            open_positions[sym] = {
                'entry'    : entry_price,
                'sl'       : l4,
                'target'   : hv_high,
                'shares'   : shares,
                'entry_dt' : next_bar['datetime'][:16],
                'rr'       : rr,
                'days_held': 0,
                'day_green': day_green,
                'day_vol_r': day_vol_r,
            }

            # Check rest of today for exit
            for future_bar in candles[ci+2:]:
                if sym not in open_positions: break
                if future_bar['low'] <= l4:
                    pnl = (l4 - entry_price) * shares
                    capital += shares * entry_price + pnl
                    monthly_stats[month]['sl']     += 1
                    monthly_stats[month]['pnl']    += pnl
                    monthly_stats[month]['trades'] += 1
                    sl_cooldown[sym] = today
                    del open_positions[sym]
                    trade_log.append({
                        'sym'      : sym,
                        'entry_dt' : next_bar['datetime'][:16],
                        'exit_dt'  : future_bar['datetime'][:16],
                        'entry'    : entry_price,
                        'exit'     : l4,
                        'result'   : 'SL',
                        'pnl'      : round(pnl, 0),
                        'shares'   : shares,
                        'rr'       : rr,
                        'month'    : month,
                        'days_held': 0,
                        'day_green': day_green,
                        'day_vol_r': day_vol_r,
                    })
                    break
                elif future_bar['high'] >= hv_high:
                    pnl = (hv_high - entry_price) * shares
                    capital += shares * entry_price + pnl
                    monthly_stats[month]['wins']   += 1
                    monthly_stats[month]['pnl']    += pnl
                    monthly_stats[month]['trades'] += 1
                    del open_positions[sym]
                    trade_log.append({
                        'sym'      : sym,
                        'entry_dt' : next_bar['datetime'][:16],
                        'exit_dt'  : future_bar['datetime'][:16],
                        'entry'    : entry_price,
                        'exit'     : hv_high,
                        'result'   : 'WIN',
                        'pnl'      : round(pnl, 0),
                        'shares'   : shares,
                        'rr'       : rr,
                        'month'    : month,
                        'days_held': 0,
                        'day_green': day_green,
                        'day_vol_r': day_vol_r,
                    })
                    break
            break

# ── Close remaining ───────────────────────────────────────────
print(f"\nClosing remaining open positions...", flush=True)
for sym, pos in open_positions.items():
    cd.execute('SELECT close FROM daily_prices WHERE symbol=? ORDER BY date DESC LIMIT 1',(sym,))
    r = cd.fetchone()
    if r:
        pnl = (r['close'] - pos['entry']) * pos['shares']
        monthly_stats[available_days[-1][:7]]['open'] += 1
        monthly_stats[available_days[-1][:7]]['pnl']  += pnl
        trade_log.append({
            'sym':sym,'entry_dt':pos['entry_dt'],'exit_dt':'OPEN',
            'entry':pos['entry'],'exit':r['close'],'result':'OPEN',
            'pnl':round(pnl,0),'shares':pos['shares'],'rr':pos['rr'],
            'month':pos['entry_dt'][:7],'days_held':pos.get('days_held',0),
            'day_green':pos.get('day_green'),'day_vol_r':pos.get('day_vol_r',0),
        })

# ── Results ───────────────────────────────────────────────────
print(f"\n{'='*70}", flush=True)
print(f"  FULLY REALISTIC BACKTEST RESULTS", flush=True)
print(f"{'='*70}", flush=True)

total_t=total_w=total_sl=total_o=0
total_pnl=0.0
print(f"\n  {'Month':<10} {'Trades':>7} {'Wins':>6} {'SL':>5} {'Open':>6} {'WR%':>6} {'P&L':>12}", flush=True)
print(f"  {'-'*60}", flush=True)
for month in sorted(monthly_stats.keys()):
    m  = monthly_stats[month]
    wr = round(m['wins']/m['trades']*100,1) if m['trades']>0 else 0
    total_t+=m['trades']; total_w+=m['wins']
    total_sl+=m['sl']; total_o+=m['open']
    total_pnl+=m['pnl']
    print(f"  {month:<10} {m['trades']:>7} {m['wins']:>6} {m['sl']:>5} {m['open']:>6} {wr:>5}% {m['pnl']:>+12,.0f}", flush=True)

print(f"  {'-'*60}", flush=True)
wr_t = round(total_w/total_t*100,1) if total_t>0 else 0
print(f"  {'TOTAL':<10} {total_t:>7} {total_w:>6} {total_sl:>5} {total_o:>6} {wr_t:>5}% {total_pnl:>+12,.0f}", flush=True)

wins_l=[t for t in trade_log if t['result']=='WIN']
sl_l  =[t for t in trade_log if t['result']=='SL']
avg_w =round(sum(t['pnl'] for t in wins_l)/len(wins_l),0) if wins_l else 0
avg_l =round(sum(t['pnl'] for t in sl_l)/len(sl_l),0) if sl_l else 0
wl_r  =round(abs(avg_w/avg_l),1) if avg_l and avg_w else 0

print(f"\n  KEY STATS:", flush=True)
print(f"  Total trades   : {total_t}", flush=True)
print(f"  Win rate       : {wr_t}%", flush=True)
print(f"  Avg win        : Rs {avg_w:+,.0f}", flush=True)
print(f"  Avg loss       : Rs {avg_l:+,.0f}", flush=True)
print(f"  Win:Loss ratio : {wl_r}:1", flush=True)
print(f"  Total P&L      : Rs {total_pnl:+,.0f}", flush=True)
print(f"  Final capital  : Rs {capital:,.0f}", flush=True)

# ── Green candle analysis (recorded but not filtered) ─────────
print(f"\n  GREEN vs RED candle analysis (post-trade):", flush=True)
closed = [t for t in trade_log if t['result'] in ['WIN','SL']]
for label, val in [('GREEN day', True), ('RED day', False), ('Unknown', None)]:
    b = [t for t in closed if t['day_green']==val]
    if not b: continue
    w = [t for t in b if t['result']=='WIN']
    wr= round(len(w)/len(b)*100,1)
    avg_p=round(sum(t['pnl'] for t in b)/len(b),0)
    tot_p=round(sum(t['pnl'] for t in b),0)
    print(f"  {label:<12}: Trades={len(b):>4} WR={wr:>5}% AvgPnL=Rs{avg_p:>+7,.0f} Total=Rs{tot_p:>+9,.0f}", flush=True)

# ── Volume analysis ───────────────────────────────────────────
print(f"\n  VOLUME ratio analysis (post-trade):", flush=True)
for v_min,v_max,label in [(0,1.0,'<1x'),(1.0,1.5,'1-1.5x'),(1.5,3.0,'1.5-3x'),(3.0,5.0,'3-5x'),(5.0,99,'5x+')]:
    b=[t for t in closed if v_min<=t['day_vol_r']<v_max]
    if not b: continue
    w=[t for t in b if t['result']=='WIN']
    wr=round(len(w)/len(b)*100,1)
    avg_p=round(sum(t['pnl'] for t in b)/len(b),0)
    tot_p=round(sum(t['pnl'] for t in b),0)
    print(f"  Vol {label:<8}: Trades={len(b):>4} WR={wr:>5}% AvgPnL=Rs{avg_p:>+7,.0f} Total=Rs{tot_p:>+9,.0f}", flush=True)

# ── R:R analysis ──────────────────────────────────────────────
print(f"\n  R:R analysis:", flush=True)
for rr_min,rr_max,label in [(5,8,'5-8x'),(8,12,'8-12x'),(12,16,'12-16x'),(16,21,'16-20x')]:
    b=[t for t in closed if rr_min<=t['rr']<rr_max]
    if not b: continue
    w=[t for t in b if t['result']=='WIN']
    wr=round(len(w)/len(b)*100,1)
    avg_w2=round(sum(t['pnl'] for t in [x for x in b if x['result']=='WIN'])/len(w),0) if w else 0
    sl2=[t for t in b if t['result']=='SL']
    avg_l2=round(sum(t['pnl'] for t in sl2)/len(sl2),0) if sl2 else 0
    tot_p=round(sum(t['pnl'] for t in b),0)
    print(f"  RR {label:<8}: Trades={len(b):>4} WR={wr:>5}% AvgWin=Rs{avg_w2:>+7,.0f} AvgLoss=Rs{avg_l2:>+7,.0f} Total=Rs{tot_p:>+9,.0f}", flush=True)

# ── Days held ─────────────────────────────────────────────────
print(f"\n  DAYS HELD:", flush=True)
for d_min,d_max,label in [(0,1,'Same day'),(1,2,'Day 1'),(2,3,'Day 2'),(3,4,'Day 3'),(4,99,'Day 4+')]:
    b=[t for t in closed if d_min<=t['days_held']<d_max]
    if not b: continue
    w=[t for t in b if t['result']=='WIN']
    wr=round(len(w)/len(b)*100,1)
    avg_p=round(sum(t['pnl'] for t in b)/len(b),0)
    print(f"  {label:<12}: Trades={len(b):>4} WR={wr:>5}% AvgPnL=Rs{avg_p:>+7,.0f}", flush=True)

# ── Full trade log ────────────────────────────────────────────
print(f"\n  FULL TRADE LOG:", flush=True)
print(f"  {'Stock':<12} {'Entry DT':<18} {'Exit DT':<18} {'Entry':>8} {'Exit':>8} {'RR':>5} {'Days':>5} {'Green':>6} {'VolR':>5} {'Result':<10} {'P&L':>8}", flush=True)
print(f"  {'-'*110}", flush=True)
for t in sorted(trade_log, key=lambda x: x['entry_dt']):
    icon ='✅' if t['result']=='WIN' else '❌' if t['result']=='SL' else '⏳'
    green='🟢' if t['day_green'] else '🔴' if t['day_green']==False else '❓'
    print(f"  {t['sym']:<12} {t['entry_dt']:<18} {t['exit_dt']:<18} "
          f"{t['entry']:>8.2f} {t['exit']:>8.2f} {t['rr']:>5} "
          f"{t['days_held']:>5} {green:>6} {t['day_vol_r']:>4.1f}x "
          f"{icon}{t['result']:<9} Rs{t['pnl']:>+7,.0f}", flush=True)

# ── Final comparison ──────────────────────────────────────────
print(f"\n  FINAL COMPARISON:", flush=True)
print(f"  {'Approach':<35} {'Trades':>7} {'WR%':>6} {'P&L':>12} {'Note'}", flush=True)
print(f"  {'-'*75}", flush=True)
print(f"  {'Daily BT (hindsight)':<35} {'28':>7} {'60.7%':>6} {'Rs +23,291':>12} EOD filters", flush=True)
print(f"  {'Approach A (current)':<35} {'86':>7} {'23.3%':>6} {'Rs +20,233':>12} EOD filters+15min entry", flush=True)
print(f"  {'Approach B (next candle+EOD)':<35} {'49':>7} {'57.1%':>6} {'Rs +28,249':>12} EOD filters+next candle", flush=True)
print(f"  {'Realistic (no EOD filters)':<35} {total_t:>7} {wr_t:>5}% {total_pnl:>+12,.0f} NO EOD filters", flush=True)

elapsed=(datetime.now()-start_time).seconds
print(f"\n  Total time: {elapsed}s", flush=True)
conn_d.close()
conn_15.close()
print("\nDone!", flush=True)
