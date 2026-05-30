"""
Comprehensive Backtest — Options A to E
A: 2 PM Green Check
B: Approach B + 2 PM combination
C: Volume at entry candle
D: RR threshold optimisation
E: Cooldown period test
"""
import sqlite3, json, time
from datetime import datetime, timedelta
from collections import defaultdict

print("="*70, flush=True)
print("  COMPREHENSIVE BACKTEST — OPTIONS A TO E", flush=True)
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

print(f"Available days: {len(available_days)}", flush=True)
print(f"Date range    : {available_days[0]} to {available_days[-1]}", flush=True)

def get_cam(h, l, cl):
    rng = h - l
    return cl - rng*1.1/4, cl - rng*1.1/2

def run_backtest(name, desc, min_rr=5.0, max_rr=20.0,
                 min_vol_r=1.5, cooldown=10,
                 check_2pm_green=False,
                 require_2pm_before_entry=False,
                 min_entry_vol_ratio=0.0):
    """
    Universal backtest runner with configurable parameters
    Always uses Approach B (next candle entry)
    """
    CAPITAL     = 60000
    MAX_POS     = 3
    RISK        = 1000
    MAX_POS_VAL = 20000

    open_positions = {}
    capital        = CAPITAL
    trade_log      = []
    sl_cooldown    = {}
    monthly_stats  = defaultdict(lambda:{
        'trades':0,'wins':0,'sl':0,'open':0,'pnl':0.0
    })

    for day_idx, today in enumerate(available_days):
        if day_idx == 0: continue
        today_dt = datetime.strptime(today, '%Y-%m-%d')
        month    = today[:7]

        time.sleep(0.02)

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

        # Monitor open positions
        to_close = []
        for sym, pos in list(open_positions.items()):
            candles = sym_candles.get(sym, [])
            for bar in candles:
                if bar['low'] <= pos['sl']:
                    pnl = (pos['sl']-pos['entry'])*pos['shares']
                    capital += pos['shares']*pos['entry']+pnl
                    monthly_stats[pos['entry_dt'][:7]]['sl']     += 1
                    monthly_stats[pos['entry_dt'][:7]]['pnl']    += pnl
                    monthly_stats[pos['entry_dt'][:7]]['trades'] += 1
                    sl_cooldown[sym] = today
                    to_close.append(sym)
                    trade_log.append({
                        'sym':sym,'entry_dt':pos['entry_dt'],
                        'exit_dt':bar['datetime'][:16],
                        'entry':pos['entry'],'exit':pos['sl'],
                        'result':'SL','pnl':round(pnl,0),
                        'shares':pos['shares'],'rr':pos['rr'],
                        'month':pos['entry_dt'][:7],
                        'days_held':pos.get('days_held',0),
                        'entry_vol_r':pos.get('entry_vol_r',0),
                    })
                    break
                elif bar['high'] >= pos['target']:
                    pnl = (pos['target']-pos['entry'])*pos['shares']
                    capital += pos['shares']*pos['entry']+pnl
                    monthly_stats[pos['entry_dt'][:7]]['wins']   += 1
                    monthly_stats[pos['entry_dt'][:7]]['pnl']    += pnl
                    monthly_stats[pos['entry_dt'][:7]]['trades'] += 1
                    to_close.append(sym)
                    trade_log.append({
                        'sym':sym,'entry_dt':pos['entry_dt'],
                        'exit_dt':bar['datetime'][:16],
                        'entry':pos['entry'],'exit':pos['target'],
                        'result':'WIN','pnl':round(pnl,0),
                        'shares':pos['shares'],'rr':pos['rr'],
                        'month':pos['entry_dt'][:7],
                        'days_held':pos.get('days_held',0),
                        'entry_vol_r':pos.get('entry_vol_r',0),
                    })
                    break

        for sym in to_close:
            if sym in open_positions: del open_positions[sym]
        for sym in open_positions:
            open_positions[sym]['days_held'] = \
                open_positions[sym].get('days_held',0)+1

        # Find new signals
        date_min = (today_dt-timedelta(days=60)).strftime('%Y-%m-%d')
        date_max = (today_dt-timedelta(days=7)).strftime('%Y-%m-%d')

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
                sl_dt = datetime.strptime(sl_cooldown[sym],'%Y-%m-%d')
                if (today_dt-sl_dt).days < cooldown: continue

            cd.execute('''
                SELECT high,low,close FROM daily_prices
                WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1
            ''', (sym, today))
            prev = cd.fetchone()
            if not prev: continue

            l3, l4 = get_cam(prev['high'],prev['low'],prev['close'])

            cd.execute('''
                SELECT AVG(volume) as av FROM (
                    SELECT volume FROM daily_prices
                    WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 20
                )
            ''', (sym, today))
            avg_vol = cd.fetchone()['av'] or 0
            if avg_vol == 0: continue

            hv_vol_r = round(hv_vol/avg_vol,2)
            if hv_vol_r > 10.0: continue
            if abs(l3-hv_low)/hv_low*100 > 4.0: continue
            if l3 < 50: continue

            risk   = l3-l4
            reward = hv_high-l3
            if risk<=0 or reward<=0: continue
            rr = round(reward/risk,1)
            if not(min_rr<=rr<=max_rr): continue

            shares = min(int(RISK/risk),int(MAX_POS_VAL/l3))
            if shares < 1: continue

            # EOD daily filters
            cd.execute('''
                SELECT open,close,volume FROM daily_prices
                WHERE symbol=? AND date=?
            ''', (sym, today))
            day_bar = cd.fetchone()
            if not day_bar: continue

            day_vol_r  = round(day_bar['volume']/avg_vol,2)
            day_green  = day_bar['close'] > day_bar['open']

            # Base volume filter
            if day_vol_r < min_vol_r: continue

            # Option A/B: 2 PM green check
            if check_2pm_green:
                # Get candle at 2 PM (14:00)
                candles_2pm = sym_candles.get(sym, [])
                green_at_2pm = False
                for bar in candles_2pm:
                    ts = bar['datetime'][11:16]
                    if ts >= '14:00':
                        # Check if trending green at 2 PM
                        first_bar = candles_2pm[0] if candles_2pm else None
                        if first_bar:
                            green_at_2pm = bar['close'] > first_bar['open']
                        break

                if not green_at_2pm: continue

            elif not check_2pm_green:
                # Standard EOD green filter
                if not day_green: continue

            # 15-min candles
            candles = sym_candles.get(sym, [])
            if len(candles) < 2: continue

            for ci, bar in enumerate(candles[:-1]):
                if bar['low'] > l3: continue

                next_bar  = candles[ci+1]
                next_open = next_bar['open']

                if next_open <= l3:        continue
                if next_open > l3*1.02:    continue
                if next_open >= hv_high:   continue
                if shares*next_open > capital: continue

                # Option B: must enter before 2 PM
                if require_2pm_before_entry:
                    entry_ts = next_bar['datetime'][11:16]
                    if entry_ts >= '14:00': continue

                # Option C: volume at entry candle
                entry_vol_r = 0
                if avg_vol > 0:
                    entry_vol_r = round(bar['volume']/
                        (avg_vol/26), 2)  # vs expected per candle
                if entry_vol_r < min_entry_vol_ratio: continue

                entry_price = next_open
                capital    -= shares*entry_price
                open_positions[sym] = {
                    'entry'      : entry_price,
                    'sl'         : l4,
                    'target'     : hv_high,
                    'shares'     : shares,
                    'entry_dt'   : next_bar['datetime'][:16],
                    'rr'         : rr,
                    'days_held'  : 0,
                    'entry_vol_r': entry_vol_r,
                }

                for future_bar in candles[ci+2:]:
                    if sym not in open_positions: break
                    if future_bar['low'] <= l4:
                        pnl = (l4-entry_price)*shares
                        capital += shares*entry_price+pnl
                        monthly_stats[month]['sl']     += 1
                        monthly_stats[month]['pnl']    += pnl
                        monthly_stats[month]['trades'] += 1
                        sl_cooldown[sym] = today
                        del open_positions[sym]
                        trade_log.append({
                            'sym':sym,'entry_dt':next_bar['datetime'][:16],
                            'exit_dt':future_bar['datetime'][:16],
                            'entry':entry_price,'exit':l4,
                            'result':'SL','pnl':round(pnl,0),
                            'shares':shares,'rr':rr,'month':month,
                            'days_held':0,'entry_vol_r':entry_vol_r,
                        })
                        break
                    elif future_bar['high'] >= hv_high:
                        pnl = (hv_high-entry_price)*shares
                        capital += shares*entry_price+pnl
                        monthly_stats[month]['wins']   += 1
                        monthly_stats[month]['pnl']    += pnl
                        monthly_stats[month]['trades'] += 1
                        del open_positions[sym]
                        trade_log.append({
                            'sym':sym,'entry_dt':next_bar['datetime'][:16],
                            'exit_dt':future_bar['datetime'][:16],
                            'entry':entry_price,'exit':hv_high,
                            'result':'WIN','pnl':round(pnl,0),
                            'shares':shares,'rr':rr,'month':month,
                            'days_held':0,'entry_vol_r':entry_vol_r,
                        })
                        break
                break

    # Close remaining
    for sym, pos in open_positions.items():
        cd.execute('SELECT close FROM daily_prices WHERE symbol=? ORDER BY date DESC LIMIT 1',(sym,))
        r = cd.fetchone()
        if r:
            pnl = (r['close']-pos['entry'])*pos['shares']
            monthly_stats[available_days[-1][:7]]['open'] += 1
            monthly_stats[available_days[-1][:7]]['pnl']  += pnl
            trade_log.append({
                'sym':sym,'entry_dt':pos['entry_dt'],'exit_dt':'OPEN',
                'entry':pos['entry'],'exit':r['close'],'result':'OPEN',
                'pnl':round(pnl,0),'shares':pos['shares'],'rr':pos['rr'],
                'month':pos['entry_dt'][:7],'days_held':pos.get('days_held',0),
                'entry_vol_r':pos.get('entry_vol_r',0),
            })

    closed = [t for t in trade_log if t['result'] in ['WIN','SL']]
    wins   = [t for t in closed if t['result']=='WIN']
    sl_hit = [t for t in closed if t['result']=='SL']
    total_pnl = sum(t['pnl'] for t in trade_log)
    wr  = round(len(wins)/len(closed)*100,1) if closed else 0
    avg_w = round(sum(t['pnl'] for t in wins)/len(wins),0) if wins else 0
    avg_l = round(sum(t['pnl'] for t in sl_hit)/len(sl_hit),0) if sl_hit else 0
    wl_r  = round(abs(avg_w/avg_l),1) if avg_l and avg_w else 0

    return {
        'name'     : name,
        'desc'     : desc,
        'trades'   : len(closed),
        'wins'     : len(wins),
        'sl'       : len(sl_hit),
        'wr'       : wr,
        'avg_win'  : avg_w,
        'avg_loss' : avg_l,
        'wl_ratio' : wl_r,
        'total_pnl': round(total_pnl,0),
        'monthly'  : dict(monthly_stats),
        'trades_log': trade_log,
    }

# ── Define all test configs ───────────────────────────────────
test_configs = [
    # Baseline
    dict(name='BASELINE', desc='Approach B baseline (EOD green+vol)',
         min_rr=5.0, cooldown=10, check_2pm_green=False),

    # Option A: 2 PM green check variants
    dict(name='A1_2PM_GREEN', desc='A: 2PM green check only',
         min_rr=5.0, cooldown=10, check_2pm_green=True,
         require_2pm_before_entry=False),

    dict(name='A2_2PM_BEFORE_ENTRY', desc='A+B: 2PM green + entry before 2PM',
         min_rr=5.0, cooldown=10, check_2pm_green=True,
         require_2pm_before_entry=True),

    # Option C: Volume at entry candle
    dict(name='C1_ENTRY_VOL_1x', desc='C: Entry candle vol >= 1x avg/candle',
         min_rr=5.0, cooldown=10, check_2pm_green=False,
         min_entry_vol_ratio=1.0),

    dict(name='C2_ENTRY_VOL_2x', desc='C: Entry candle vol >= 2x avg/candle',
         min_rr=5.0, cooldown=10, check_2pm_green=False,
         min_entry_vol_ratio=2.0),

    dict(name='C3_ENTRY_VOL_3x', desc='C: Entry candle vol >= 3x avg/candle',
         min_rr=5.0, cooldown=10, check_2pm_green=False,
         min_entry_vol_ratio=3.0),

    # Option D: RR threshold
    dict(name='D1_RR_5', desc='D: RR >= 5x (baseline)',
         min_rr=5.0, max_rr=20.0, cooldown=10, check_2pm_green=False),

    dict(name='D2_RR_7', desc='D: RR >= 7x',
         min_rr=7.0, max_rr=20.0, cooldown=10, check_2pm_green=False),

    dict(name='D3_RR_8', desc='D: RR >= 8x',
         min_rr=8.0, max_rr=20.0, cooldown=10, check_2pm_green=False),

    dict(name='D4_RR_10', desc='D: RR >= 10x',
         min_rr=10.0, max_rr=20.0, cooldown=10, check_2pm_green=False),

    dict(name='D5_RR_12', desc='D: RR >= 12x',
         min_rr=12.0, max_rr=20.0, cooldown=10, check_2pm_green=False),

    # Option E: Cooldown period
    dict(name='E1_COOL_5', desc='E: Cooldown 5 days after SL',
         min_rr=5.0, cooldown=5, check_2pm_green=False),

    dict(name='E2_COOL_10', desc='E: Cooldown 10 days (baseline)',
         min_rr=5.0, cooldown=10, check_2pm_green=False),

    dict(name='E3_COOL_15', desc='E: Cooldown 15 days after SL',
         min_rr=5.0, cooldown=15, check_2pm_green=False),

    dict(name='E4_COOL_0', desc='E: No cooldown',
         min_rr=5.0, cooldown=0, check_2pm_green=False),

    # Best combinations
    dict(name='COMBO1', desc='COMBO: RR>=8 + 2PM green',
         min_rr=8.0, cooldown=10, check_2pm_green=True),

    dict(name='COMBO2', desc='COMBO: RR>=10 + 2PM green',
         min_rr=10.0, cooldown=10, check_2pm_green=True),

    dict(name='COMBO3', desc='COMBO: RR>=8 + vol>=2x + 2PM green',
         min_rr=8.0, cooldown=10, check_2pm_green=True,
         min_entry_vol_ratio=2.0),
]

# ── Run all tests ─────────────────────────────────────────────
all_results = []
start_time  = datetime.now()

for i, cfg in enumerate(test_configs):
    elapsed = (datetime.now()-start_time).seconds
    print(f"\n[{i+1}/{len(test_configs)}] {cfg['name']}: {cfg['desc']}", flush=True)
    r = run_backtest(**cfg)
    all_results.append(r)
    print(f"  → Trades={r['trades']} WR={r['wr']}% AvgWin=Rs{r['avg_win']:+,.0f} "
          f"AvgLoss=Rs{r['avg_loss']:+,.0f} W:L={r['wl_ratio']} "
          f"P&L=Rs{r['total_pnl']:+,.0f} [{elapsed}s]", flush=True)

# ── Final comparison table ────────────────────────────────────
print(f"\n{'='*90}", flush=True)
print(f"  COMPREHENSIVE RESULTS — ALL OPTIONS A TO E", flush=True)
print(f"{'='*90}", flush=True)
print(f"\n  {'Test':<25} {'Desc':<42} {'Trades':>7} {'WR%':>6} {'AvgWin':>8} {'AvgLoss':>8} {'W:L':>5} {'TotalPnL':>11}", flush=True)
print(f"  {'-'*115}", flush=True)

# Sort by total P&L
for r in sorted(all_results, key=lambda x: -x['total_pnl']):
    print(f"  {r['name']:<25} {r['desc']:<42} {r['trades']:>7} {r['wr']:>5}% "
          f"{r['avg_win']:>+8,.0f} {r['avg_loss']:>+8,.0f} {r['wl_ratio']:>5} "
          f"{r['total_pnl']:>+11,.0f}", flush=True)

# Monthly breakdown for top 5
print(f"\n{'='*90}", flush=True)
print(f"  MONTHLY BREAKDOWN — TOP 5 BY P&L", flush=True)
print(f"{'='*90}", flush=True)

top5 = sorted(all_results, key=lambda x: -x['total_pnl'])[:5]
for r in top5:
    print(f"\n  {r['name']}: {r['desc']}", flush=True)
    total_t=total_w=total_pnl=0
    for month in sorted(r['monthly'].keys()):
        m  = r['monthly'][month]
        wr = round(m['wins']/m['trades']*100,1) if m['trades']>0 else 0
        total_t+=m['trades']; total_w+=m['wins']; total_pnl+=m['pnl']
        print(f"    {month}: Trades={m['trades']:>3} Wins={m['wins']:>3} "
              f"WR={wr:>5}% P&L=Rs{m['pnl']:>+9,.0f}", flush=True)
    wr_t=round(total_w/total_t*100,1) if total_t>0 else 0
    print(f"    TOTAL : Trades={total_t:>3} Wins={total_w:>3} "
          f"WR={wr_t:>5}% P&L=Rs{total_pnl:>+9,.0f}", flush=True)

# Group analysis
print(f"\n{'='*90}", flush=True)
print(f"  GROUP ANALYSIS", flush=True)
print(f"{'='*90}", flush=True)

groups = {
    'Option A (2PM Green)'      : [r for r in all_results if r['name'].startswith('A')],
    'Option C (Entry Volume)'   : [r for r in all_results if r['name'].startswith('C')],
    'Option D (RR Threshold)'   : [r for r in all_results if r['name'].startswith('D')],
    'Option E (Cooldown)'       : [r for r in all_results if r['name'].startswith('E')],
    'Combinations'              : [r for r in all_results if r['name'].startswith('COMBO')],
}

for grp_name, grp_results in groups.items():
    if not grp_results: continue
    print(f"\n  {grp_name}:", flush=True)
    best = max(grp_results, key=lambda x: x['total_pnl'])
    baseline = next((r for r in all_results if r['name']=='BASELINE'), None)
    for r in grp_results:
        diff = r['total_pnl'] - baseline['total_pnl'] if baseline else 0
        marker = ' ← BEST IN GROUP' if r['name']==best['name'] else ''
        marker += f' (vs baseline: Rs{diff:+,.0f})' if baseline else ''
        print(f"    {r['name']:<25} Trades={r['trades']:>3} WR={r['wr']:>5}% "
              f"P&L=Rs{r['total_pnl']:>+9,.0f}{marker}", flush=True)

# Key takeaways
print(f"\n{'='*90}", flush=True)
print(f"  KEY TAKEAWAYS", flush=True)
print(f"{'='*90}", flush=True)

baseline = next((r for r in all_results if r['name']=='BASELINE'), None)
best_overall = max(all_results, key=lambda x: x['total_pnl'])
best_wr = max(all_results, key=lambda x: x['wr'])

if baseline:
    print(f"\n  Baseline (Approach B)  : Rs{baseline['total_pnl']:+,.0f} | {baseline['wr']}% WR | {baseline['trades']} trades", flush=True)
print(f"  Best P&L               : {best_overall['name']} → Rs{best_overall['total_pnl']:+,.0f} | {best_overall['wr']}% WR", flush=True)
print(f"  Best Win Rate          : {best_wr['name']} → {best_wr['wr']}% WR | Rs{best_wr['total_pnl']:+,.0f}", flush=True)

elapsed = (datetime.now()-start_time).seconds
print(f"\n  Total time: {elapsed}s", flush=True)
conn_d.close()
conn_15.close()
print("\nDone!", flush=True)
