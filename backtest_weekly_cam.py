"""
Weekly Camarilla Backtest — 3 Scenarios
S1: Pure Weekly (entry+SL at weekly levels)
S2: Hybrid (daily entry + weekly SL)
S3: Confluence (daily L3 near weekly L3)
"""
import sqlite3, json, time
from datetime import datetime, timedelta
from collections import defaultdict

print("="*70, flush=True)
print("  WEEKLY CAMARILLA BACKTEST — 3 SCENARIOS", flush=True)
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

def get_weekly_cam(symbol, before_date):
    """Get previous week's Camarilla levels"""
    cd.execute('''
        SELECT
            MAX(high)  as wk_high,
            MIN(low)   as wk_low
        FROM daily_prices
        WHERE symbol=?
        AND strftime('%Y-%W', date) = strftime('%Y-%W', date(?, '-7 days'))
    ''', (symbol, before_date))
    wk = cd.fetchone()
    if not wk or not wk['wk_high']: return None, None, None, None

    # Previous week's close
    cd.execute('''
        SELECT close FROM daily_prices
        WHERE symbol=?
        AND strftime('%Y-%W', date) = strftime('%Y-%W', date(?, '-7 days'))
        ORDER BY date DESC LIMIT 1
    ''', (symbol, before_date))
    wk_close = cd.fetchone()
    if not wk_close: return None, None, None, None

    wk_l3, wk_l4 = get_cam(wk['wk_high'], wk['wk_low'], wk_close['close'])
    return wk_l3, wk_l4, wk['wk_high'], wk['wk_low']

CAPITAL     = 60000
MAX_POS     = 3
RISK        = 1000
MAX_POS_VAL = 20000
MIN_VOL_R   = 1.5
MIN_RR      = 5.0
MAX_RR      = 20.0
COOLDOWN    = 10

scenarios = {
    'S1_PureWeekly' : {
        'desc'   : 'Pure Weekly (entry+SL at weekly L3/L4)',
        'results': []
    },
    'S2_Hybrid'     : {
        'desc'   : 'Hybrid (Daily entry + Weekly SL)',
        'results': []
    },
    'S3_Confluence' : {
        'desc'   : 'Confluence (Daily L3 near Weekly L3 ±2%)',
        'results': []
    },
    'S0_Baseline'   : {
        'desc'   : 'Baseline Approach B (daily only, no weekly)',
        'results': []
    },
}

print(f"\nRunning all 4 scenarios...", flush=True)
start_time = datetime.now()

for scenario_key, scenario in scenarios.items():
    print(f"\n  Running {scenario_key}: {scenario['desc']}...", flush=True)

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

        time.sleep(0.01)  # resource friendly

        # Load 15-min candles
        c15.execute('''
            SELECT symbol, datetime, open, high, low, close
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
                    pnl = (pos['sl'] - pos['entry']) * pos['shares']
                    capital += pos['shares'] * pos['entry'] + pnl
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
                        'sl_dist':pos.get('sl_dist',0),
                    })
                    break
                elif bar['high'] >= pos['target']:
                    pnl = (pos['target'] - pos['entry']) * pos['shares']
                    capital += pos['shares'] * pos['entry'] + pnl
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
                        'sl_dist':pos.get('sl_dist',0),
                    })
                    break

        for sym in to_close:
            if sym in open_positions: del open_positions[sym]
        for sym in open_positions:
            open_positions[sym]['days_held'] = \
                open_positions[sym].get('days_held',0)+1

        # Find new signals
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
                sl_dt = datetime.strptime(sl_cooldown[sym],'%Y-%m-%d')
                if (today_dt-sl_dt).days < COOLDOWN: continue

            # Yesterday's daily candle
            cd.execute('''
                SELECT high,low,close FROM daily_prices
                WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1
            ''', (sym, today))
            prev = cd.fetchone()
            if not prev: continue

            daily_l3, daily_l4 = get_cam(prev['high'],prev['low'],prev['close'])

            # Weekly Camarilla
            wk_l3, wk_l4, wk_high, wk_low = get_weekly_cam(sym, today)

            # Avg volume
            cd.execute('''
                SELECT AVG(volume) as av FROM (
                    SELECT volume FROM daily_prices
                    WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 20
                )
            ''', (sym, today))
            avg_vol = cd.fetchone()['av'] or 0
            if avg_vol == 0: continue

            # Daily candle filters (EOD — green + volume)
            cd.execute('''
                SELECT open, close, volume FROM daily_prices
                WHERE symbol=? AND date=?
            ''', (sym, today))
            day_bar = cd.fetchone()
            if not day_bar: continue
            day_vol_r = round(day_bar['volume']/avg_vol, 2)
            day_green = day_bar['close'] > day_bar['open']
            if day_vol_r < MIN_VOL_R: continue
            if not day_green: continue

            hv_vol_r = round(hv_vol/avg_vol, 2)
            if hv_vol_r > 10.0: continue
            if daily_l3 < 50: continue

            # ── Scenario-specific logic ───────────────────────
            entry_l3 = None
            entry_sl = None

            if scenario_key == 'S0_Baseline':
                # Original approach B — daily only
                if abs(daily_l3-hv_low)/hv_low*100 > 4.0: continue
                risk   = daily_l3 - daily_l4
                reward = hv_high - daily_l3
                if risk<=0 or reward<=0: continue
                rr = round(reward/risk,1)
                if not(MIN_RR<=rr<=MAX_RR): continue
                entry_l3 = daily_l3
                entry_sl = daily_l4

            elif scenario_key == 'S1_PureWeekly':
                # Entry and SL at weekly levels
                if not wk_l3 or not wk_l4: continue
                if abs(wk_l3-hv_low)/hv_low*100 > 4.0: continue
                risk   = wk_l3 - wk_l4
                reward = hv_high - wk_l3
                if risk<=0 or reward<=0: continue
                rr = round(reward/risk,1)
                if not(MIN_RR<=rr<=MAX_RR): continue
                entry_l3 = wk_l3
                entry_sl = wk_l4

            elif scenario_key == 'S2_Hybrid':
                # Daily entry + Weekly SL
                if not wk_l4: continue
                if abs(daily_l3-hv_low)/hv_low*100 > 4.0: continue
                risk   = daily_l3 - wk_l4  # wider SL!
                reward = hv_high - daily_l3
                if risk<=0 or reward<=0: continue
                rr = round(reward/risk,1)
                if not(MIN_RR<=rr<=MAX_RR): continue
                entry_l3 = daily_l3
                entry_sl = wk_l4

            elif scenario_key == 'S3_Confluence':
                # Only trade when daily L3 near weekly L3
                if not wk_l3: continue
                confluence = abs(daily_l3-wk_l3)/wk_l3*100
                if confluence > 2.0: continue  # must be within 2%
                if abs(daily_l3-hv_low)/hv_low*100 > 4.0: continue
                risk   = daily_l3 - daily_l4
                reward = hv_high - daily_l3
                if risk<=0 or reward<=0: continue
                rr = round(reward/risk,1)
                if not(MIN_RR<=rr<=MAX_RR): continue
                entry_l3 = daily_l3
                entry_sl = daily_l4

            if entry_l3 is None: continue

            shares = min(int(RISK/(entry_l3-entry_sl)),
                        int(MAX_POS_VAL/entry_l3))
            if shares < 1: continue

            # 15-min entry (Approach B)
            candles = sym_candles.get(sym, [])
            if len(candles) < 2: continue

            for ci, bar in enumerate(candles[:-1]):
                if bar['low'] > entry_l3: continue

                next_bar  = candles[ci+1]
                next_open = next_bar['open']

                if next_open <= entry_l3:        continue
                if next_open > entry_l3 * 1.02:  continue
                if next_open >= hv_high:         continue
                if shares * next_open > capital: continue

                entry_price = next_open
                sl_dist     = round(entry_price - entry_sl, 2)
                capital    -= shares * entry_price
                open_positions[sym] = {
                    'entry'    : entry_price,
                    'sl'       : entry_sl,
                    'target'   : hv_high,
                    'shares'   : shares,
                    'entry_dt' : next_bar['datetime'][:16],
                    'rr'       : rr,
                    'days_held': 0,
                    'sl_dist'  : sl_dist,
                }

                # Check rest of today
                for future_bar in candles[ci+2:]:
                    if sym not in open_positions: break
                    if future_bar['low'] <= entry_sl:
                        pnl = (entry_sl-entry_price)*shares
                        capital += shares*entry_price+pnl
                        monthly_stats[month]['sl']     += 1
                        monthly_stats[month]['pnl']    += pnl
                        monthly_stats[month]['trades'] += 1
                        sl_cooldown[sym] = today
                        del open_positions[sym]
                        trade_log.append({
                            'sym':sym,'entry_dt':next_bar['datetime'][:16],
                            'exit_dt':future_bar['datetime'][:16],
                            'entry':entry_price,'exit':entry_sl,
                            'result':'SL','pnl':round(pnl,0),
                            'shares':shares,'rr':rr,'month':month,
                            'days_held':0,'sl_dist':sl_dist,
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
                            'days_held':0,'sl_dist':sl_dist,
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
                'sl_dist':pos.get('sl_dist',0),
            })

    # Store results
    total_pnl = sum(t['pnl'] for t in trade_log if t['result']!='OPEN')
    total_pnl+= sum(t['pnl'] for t in trade_log if t['result']=='OPEN')
    wins   = [t for t in trade_log if t['result']=='WIN']
    sl_hit = [t for t in trade_log if t['result']=='SL']
    closed = [t for t in trade_log if t['result'] in ['WIN','SL']]
    wr     = round(len(wins)/len(closed)*100,1) if closed else 0
    avg_w  = round(sum(t['pnl'] for t in wins)/len(wins),0) if wins else 0
    avg_l  = round(sum(t['pnl'] for t in sl_hit)/len(sl_hit),0) if sl_hit else 0
    wl_r   = round(abs(avg_w/avg_l),1) if avg_l and avg_w else 0

    # Avg SL distance
    avg_sl_dist = round(sum(t['sl_dist'] for t in closed)/len(closed),2) if closed else 0

    # Same candle SL hits
    same_candle_sl = sum(1 for t in trade_log
                        if t['result']=='SL' and t['days_held']==0
                        and t['exit_dt'][:13]==t['entry_dt'][:13])

    scenarios[scenario_key]['stats'] = {
        'trades'        : len(closed),
        'wins'          : len(wins),
        'sl'            : len(sl_hit),
        'wr'            : wr,
        'avg_win'       : avg_w,
        'avg_loss'      : avg_l,
        'wl_ratio'      : wl_r,
        'total_pnl'     : round(total_pnl,0),
        'monthly'       : dict(monthly_stats),
        'trade_log'     : trade_log,
        'avg_sl_dist'   : avg_sl_dist,
        'same_candle_sl': same_candle_sl,
    }

    elapsed = (datetime.now()-start_time).seconds
    print(f"  Done! Trades={len(closed)} WR={wr}% P&L=Rs{total_pnl:+,.0f} Time={elapsed}s", flush=True)

# ── Print comparison ──────────────────────────────────────────
print(f"\n{'='*80}", flush=True)
print(f"  SCENARIO COMPARISON", flush=True)
print(f"{'='*80}", flush=True)
print(f"\n  {'Scenario':<40} {'Trades':>7} {'WR%':>6} {'AvgWin':>9} {'AvgLoss':>9} {'W:L':>5} {'SameCandleSL':>13} {'AvgSLdist':>10} {'TotalPnL':>11}", flush=True)
print(f"  {'-'*115}", flush=True)

for key, sc in scenarios.items():
    s = sc['stats']
    print(f"  {sc['desc']:<40} {s['trades']:>7} {s['wr']:>5}% {s['avg_win']:>+9,.0f} {s['avg_loss']:>+9,.0f} {s['wl_ratio']:>5} {s['same_candle_sl']:>13} {s['avg_sl_dist']:>10.2f} {s['total_pnl']:>+11,.0f}", flush=True)

# Monthly breakdown
print(f"\n{'='*80}", flush=True)
print(f"  MONTHLY BREAKDOWN", flush=True)
print(f"{'='*80}", flush=True)

all_months = sorted(set(
    m for sc in scenarios.values()
    for m in sc['stats']['monthly'].keys()
))

for key, sc in scenarios.items():
    s = sc['stats']
    print(f"\n  {sc['desc']}:", flush=True)
    total_t=total_w=total_pnl=0
    for month in all_months:
        m   = s['monthly'].get(month, {'trades':0,'wins':0,'sl':0,'pnl':0.0})
        wr  = round(m['wins']/m['trades']*100,1) if m['trades']>0 else 0
        total_t+=m['trades']; total_w+=m['wins']; total_pnl+=m['pnl']
        print(f"    {month}: Trades={m['trades']:>3} Wins={m['wins']:>3} WR={wr:>5}% P&L=Rs{m['pnl']:>+9,.0f}", flush=True)
    wr_t=round(total_w/total_t*100,1) if total_t>0 else 0
    print(f"    TOTAL : Trades={total_t:>3} Wins={total_w:>3} WR={wr_t:>5}% P&L=Rs{total_pnl:>+9,.0f}", flush=True)

# Key insight
print(f"\n{'='*80}", flush=True)
print(f"  KEY INSIGHTS", flush=True)
print(f"{'='*80}", flush=True)
for key, sc in scenarios.items():
    s  = sc['stats']
    tl = s['trade_log']
    closed = [t for t in tl if t['result'] in ['WIN','SL']]
    same_d = [t for t in closed if t['days_held']==0]
    same_d_wr = round(sum(1 for t in same_d if t['result']=='WIN')/len(same_d)*100,1) if same_d else 0
    print(f"\n  {sc['desc']}:", flush=True)
    print(f"    Same day exits   : {len(same_d)} trades | {same_d_wr}% WR", flush=True)
    print(f"    Same candle SL   : {s['same_candle_sl']} hits", flush=True)
    print(f"    Avg SL distance  : Rs {s['avg_sl_dist']:.2f}", flush=True)
    print(f"    Win:Loss ratio   : {s['wl_ratio']}:1", flush=True)

elapsed=(datetime.now()-start_time).seconds
print(f"\n  Total time: {elapsed}s", flush=True)
conn_d.close()
conn_15.close()
print("\nDone!", flush=True)
