"""
HYBRID SL BACKTEST
Rules:
  1. Calculate Daily L3/L4 AND Weekly L4
  2. If Weekly L4 >= L3 (entry) → SKIP (stock below weekly support)
  3. If Daily gap < 10 Rs → use Weekly L4 as SL
  4. If Daily gap >= 10 Rs → use Daily L4 as SL
  5. Shares based on actual SL used
  6. Entry: Approach B (next 15-min candle after L3 touch)
  7. Exit: 15-min candles (SL or Target)

Reality check:
  Signal  : EOD green + volume (half real)
  Entry   : 15-min next candle (REAL)
  SL      : Weekly/Daily L4 (REAL - known before market opens)
  Exit    : 15-min candles (REAL)
"""
import sqlite3, json, time
from datetime import datetime, timedelta
from collections import defaultdict

print("="*70, flush=True)
print("  HYBRID SL BACKTEST", flush=True)
print("  Daily entry + Smart SL (Weekly or Daily L4)", flush=True)
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
print(f"Date range     : {available_days[0]} to {available_days[-1]}", flush=True)
print(f"\nRules:", flush=True)
print(f"  ✅ Skip if Weekly L4 >= L3 (stock below weekly support)", flush=True)
print(f"  ✅ Use Weekly L4 as SL if Daily gap < 10 Rs", flush=True)
print(f"  ✅ Use Daily L4 as SL if Daily gap >= 10 Rs", flush=True)
print(f"  ✅ Approach B entry (next 15-min candle)", flush=True)
print(f"  ✅ GTT SL + Target via 15-min candles", flush=True)

def get_cam(h, l, cl):
    rng = h - l
    return cl - rng*1.1/4, cl - rng*1.1/2

def get_weekly_l4(sym, trade_date):
    """Get previous week's L4 using actual calendar"""
    trade_dt     = datetime.strptime(trade_date, '%Y-%m-%d')
    trade_weekday= trade_dt.weekday()  # 0=Mon 4=Fri
    this_monday  = trade_dt - timedelta(days=trade_weekday)
    prev_monday  = this_monday - timedelta(days=7)
    prev_friday  = this_monday - timedelta(days=3)

    cd.execute('''
        SELECT MAX(high) as wh, MIN(low) as wl
        FROM daily_prices WHERE symbol=?
        AND date>=? AND date<=?
    ''', (sym,
          prev_monday.strftime('%Y-%m-%d'),
          prev_friday.strftime('%Y-%m-%d')))
    wk = cd.fetchone()
    if not wk or not wk['wh']: return None

    cd.execute('''
        SELECT close FROM daily_prices WHERE symbol=?
        AND date>=? AND date<=?
        ORDER BY date DESC LIMIT 1
    ''', (sym,
          prev_monday.strftime('%Y-%m-%d'),
          prev_friday.strftime('%Y-%m-%d')))
    wc = cd.fetchone()
    if not wc: return None

    _, wk_l4 = get_cam(wk['wh'], wk['wl'], wc['close'])
    return round(wk_l4, 2)

# Config
CAPITAL         = 60000
MAX_POS         = 3
RISK            = 1000
MAX_POS_VAL     = 20000
MIN_VOL_R       = 1.5
MIN_RR          = 5.0
MAX_RR          = 20.0
COOLDOWN        = 10
DAILY_GAP_THRESH= 10.0  # Rs threshold for daily vs weekly SL

open_positions = {}
capital        = CAPITAL
trade_log      = []
sl_cooldown    = {}
monthly_stats  = defaultdict(lambda:{
    'trades':0,'wins':0,'sl':0,'open':0,'pnl':0.0,
    'used_weekly_sl':0,'used_daily_sl':0,'skipped_weak':0
})

skip_stats = {
    'weak_setup'  : 0,  # Weekly L4 >= L3
    'no_weekly'   : 0,  # No weekly data available
    'daily_used'  : 0,  # Used daily SL (gap >= 10)
    'weekly_used' : 0,  # Used weekly SL (gap < 10)
}

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

    # ── Monitor open positions ────────────────────────────────
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
                    'sym'       : sym,
                    'entry_dt'  : pos['entry_dt'],
                    'exit_dt'   : bar['datetime'][:16],
                    'entry'     : pos['entry'],
                    'exit'      : pos['sl'],
                    'result'    : 'SL',
                    'pnl'       : round(pnl, 0),
                    'shares'    : pos['shares'],
                    'rr'        : pos['rr'],
                    'sl_type'   : pos['sl_type'],
                    'sl_dist'   : pos['sl_dist'],
                    'month'     : pos['entry_dt'][:7],
                    'days_held' : pos.get('days_held', 0),
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
                    'sym'       : sym,
                    'entry_dt'  : pos['entry_dt'],
                    'exit_dt'   : bar['datetime'][:16],
                    'entry'     : pos['entry'],
                    'exit'      : pos['target'],
                    'result'    : 'WIN',
                    'pnl'       : round(pnl, 0),
                    'shares'    : pos['shares'],
                    'rr'        : pos['rr'],
                    'sl_type'   : pos['sl_type'],
                    'sl_dist'   : pos['sl_dist'],
                    'month'     : pos['entry_dt'][:7],
                    'days_held' : pos.get('days_held', 0),
                })
                break

    for sym in to_close:
        if sym in open_positions: del open_positions[sym]
    for sym in open_positions:
        open_positions[sym]['days_held'] = \
            open_positions[sym].get('days_held', 0) + 1

    # ── Find new signals ──────────────────────────────────────
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

        # Yesterday's daily candle → L3/L4
        cd.execute('''
            SELECT high,low,close FROM daily_prices
            WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1
        ''', (sym, today))
        prev = cd.fetchone()
        if not prev: continue

        daily_l3, daily_l4 = get_cam(
            prev['high'], prev['low'], prev['close'])

        # Avg volume
        cd.execute('''
            SELECT AVG(volume) as av FROM (
                SELECT volume FROM daily_prices
                WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 20
            )
        ''', (sym, today))
        avg_vol = cd.fetchone()['av'] or 0
        if avg_vol == 0: continue

        # Base filters
        hv_vol_r = round(hv_vol/avg_vol, 2)
        if hv_vol_r > 10.0: continue
        if abs(daily_l3-hv_low)/hv_low*100 > 4.0: continue
        if daily_l3 < 50: continue

        # EOD daily filters
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

        # ── HYBRID SL LOGIC ───────────────────────────────────
        wk_l4 = get_weekly_l4(sym, today)

        # Rule 1: Skip if weekly L4 >= L3 (below weekly support)
        if wk_l4 and wk_l4 >= daily_l3:
            skip_stats['weak_setup'] += 1
            monthly_stats[month]['skipped_weak'] += 1
            continue

        # Rule 2: Choose SL based on daily gap
        daily_gap = daily_l3 - daily_l4

        if daily_gap < DAILY_GAP_THRESH and wk_l4:
            # Use weekly SL (daily too tight)
            chosen_sl   = wk_l4
            sl_type     = 'WEEKLY'
            skip_stats['weekly_used'] += 1
            monthly_stats[month]['used_weekly_sl'] += 1
        else:
            # Use daily SL (wide enough or no weekly data)
            chosen_sl   = daily_l4
            sl_type     = 'DAILY'
            skip_stats['daily_used'] += 1
            monthly_stats[month]['used_daily_sl'] += 1

        # Position sizing based on chosen SL
        risk   = daily_l3 - chosen_sl
        reward = hv_high - daily_l3
        if risk <= 0 or reward <= 0: continue
        rr = round(reward/risk, 1)
        if not (MIN_RR <= rr <= MAX_RR): continue

        shares = min(int(RISK/risk), int(MAX_POS_VAL/daily_l3))
        if shares < 1: continue

        # ── Approach B: next 15-min candle entry ──────────────
        candles = sym_candles.get(sym, [])
        if len(candles) < 2: continue

        for ci, bar in enumerate(candles[:-1]):
            if bar['low'] > daily_l3: continue

            next_bar  = candles[ci+1]
            next_open = next_bar['open']

            if next_open <= daily_l3:        continue
            if next_open > daily_l3 * 1.02:  continue
            if next_open >= hv_high:         continue
            if shares * next_open > capital: continue

            # ✅ Valid entry!
            entry_price = next_open
            sl_dist     = round(entry_price - chosen_sl, 2)
            capital    -= shares * entry_price
            open_positions[sym] = {
                'entry'    : entry_price,
                'sl'       : chosen_sl,
                'target'   : hv_high,
                'shares'   : shares,
                'entry_dt' : next_bar['datetime'][:16],
                'rr'       : rr,
                'sl_type'  : sl_type,
                'sl_dist'  : sl_dist,
                'days_held': 0,
            }

            # Check rest of today
            for future_bar in candles[ci+2:]:
                if sym not in open_positions: break
                if future_bar['low'] <= chosen_sl:
                    pnl = (chosen_sl - entry_price) * shares
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
                        'exit'     : chosen_sl,
                        'result'   : 'SL',
                        'pnl'      : round(pnl, 0),
                        'shares'   : shares,
                        'rr'       : rr,
                        'sl_type'  : sl_type,
                        'sl_dist'  : sl_dist,
                        'month'    : month,
                        'days_held': 0,
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
                        'sl_type'  : sl_type,
                        'sl_dist'  : sl_dist,
                        'month'    : month,
                        'days_held': 0,
                    })
                    break
            break

# ── Close remaining ───────────────────────────────────────────
print(f"\nClosing remaining open positions...", flush=True)
for sym, pos in open_positions.items():
    cd.execute('''
        SELECT close FROM daily_prices
        WHERE symbol=? ORDER BY date DESC LIMIT 1
    ''', (sym,))
    r = cd.fetchone()
    if r:
        pnl = (r['close'] - pos['entry']) * pos['shares']
        monthly_stats[available_days[-1][:7]]['open'] += 1
        monthly_stats[available_days[-1][:7]]['pnl']  += pnl
        trade_log.append({
            'sym':sym, 'entry_dt':pos['entry_dt'], 'exit_dt':'OPEN',
            'entry':pos['entry'], 'exit':r['close'], 'result':'OPEN',
            'pnl':round(pnl,0), 'shares':pos['shares'], 'rr':pos['rr'],
            'sl_type':pos['sl_type'], 'sl_dist':pos['sl_dist'],
            'month':pos['entry_dt'][:7], 'days_held':pos.get('days_held',0),
        })

# ── Results ───────────────────────────────────────────────────
print(f"\n{'='*70}", flush=True)
print(f"  HYBRID SL BACKTEST RESULTS", flush=True)
print(f"{'='*70}", flush=True)

# SL usage stats
print(f"\n  SL SELECTION STATS:", flush=True)
print(f"  Weekly SL used  : {skip_stats['weekly_used']} trades", flush=True)
print(f"  Daily SL used   : {skip_stats['daily_used']} trades", flush=True)
print(f"  Skipped (weak)  : {skip_stats['weak_setup']} stocks", flush=True)

# Monthly summary
total_t=total_w=total_sl=total_o=0
total_pnl=0.0
print(f"\n  {'Month':<10} {'Trades':>7} {'Wins':>6} {'SL':>5} {'Open':>6} {'WR%':>6} {'P&L':>12}", flush=True)
print(f"  {'-'*60}", flush=True)
for month in sorted(monthly_stats.keys()):
    m  = monthly_stats[month]
    wr = round(m['wins']/m['trades']*100,1) if m['trades']>0 else 0
    total_t  +=m['trades']; total_w+=m['wins']
    total_sl +=m['sl'];     total_o+=m['open']
    total_pnl+=m['pnl']
    print(f"  {month:<10} {m['trades']:>7} {m['wins']:>6} {m['sl']:>5} {m['open']:>6} {wr:>5}% {m['pnl']:>+12,.0f}", flush=True)

print(f"  {'-'*60}", flush=True)
wr_t = round(total_w/total_t*100,1) if total_t>0 else 0
print(f"  {'TOTAL':<10} {total_t:>7} {total_w:>6} {total_sl:>5} {total_o:>6} {wr_t:>5}% {total_pnl:>+12,.0f}", flush=True)

wins_l = [t for t in trade_log if t['result']=='WIN']
sl_l   = [t for t in trade_log if t['result']=='SL']
avg_w  = round(sum(t['pnl'] for t in wins_l)/len(wins_l),0) if wins_l else 0
avg_l  = round(sum(t['pnl'] for t in sl_l)/len(sl_l),0) if sl_l else 0
wl_r   = round(abs(avg_w/avg_l),1) if avg_l and avg_w else 0

print(f"\n  KEY STATS:", flush=True)
print(f"  Total trades   : {total_t}", flush=True)
print(f"  Win rate       : {wr_t}%", flush=True)
print(f"  Avg win        : Rs {avg_w:+,.0f}", flush=True)
print(f"  Avg loss       : Rs {avg_l:+,.0f}", flush=True)
print(f"  Win:Loss ratio : {wl_r}:1", flush=True)
print(f"  Total P&L      : Rs {total_pnl:+,.0f}", flush=True)
print(f"  Final capital  : Rs {capital:,.0f}", flush=True)

# SL type breakdown
print(f"\n  SL TYPE BREAKDOWN:", flush=True)
for sl_type in ['WEEKLY','DAILY']:
    b = [t for t in trade_log if t.get('sl_type')==sl_type
         and t['result'] in ['WIN','SL']]
    if not b: continue
    w = [t for t in b if t['result']=='WIN']
    s = [t for t in b if t['result']=='SL']
    wr= round(len(w)/len(b)*100,1)
    avg_sl_dist = round(sum(t['sl_dist'] for t in b)/len(b),2)
    avg_p = round(sum(t['pnl'] for t in b)/len(b),0)
    tot_p = round(sum(t['pnl'] for t in b),0)
    print(f"  {sl_type} SL: Trades={len(b):>4} WR={wr:>5}% "
          f"AvgSLdist=Rs{avg_sl_dist:>7.2f} AvgPnL=Rs{avg_p:>+7,.0f} "
          f"Total=Rs{tot_p:>+9,.0f}", flush=True)

# Days held
print(f"\n  DAYS HELD:", flush=True)
for d_min,d_max,label in [(0,1,'Same day'),(1,2,'Day 1'),(2,3,'Day 2'),(3,4,'Day 3'),(4,99,'Day 4+')]:
    b=[t for t in trade_log if d_min<=t['days_held']<d_max
       and t['result'] in ['WIN','SL']]
    if not b: continue
    w=[t for t in b if t['result']=='WIN']
    wr=round(len(w)/len(b)*100,1)
    avg_p=round(sum(t['pnl'] for t in b)/len(b),0)
    print(f"  {label:<12}: Trades={len(b):>4} WR={wr:>5}% AvgPnL=Rs{avg_p:>+7,.0f}", flush=True)

# Full trade log
print(f"\n  FULL TRADE LOG:", flush=True)
print(f"  {'Stock':<12} {'Entry DT':<18} {'Exit DT':<18} {'Entry':>8} "
      f"{'Exit':>8} {'RR':>5} {'SL':>7} {'SLdist':>7} {'Days':>5} "
      f"{'Result':<10} {'P&L':>8}", flush=True)
print(f"  {'-'*110}", flush=True)
for t in sorted(trade_log, key=lambda x: x['entry_dt']):
    icon='✅' if t['result']=='WIN' else '❌' if t['result']=='SL' else '⏳'
    print(f"  {t['sym']:<12} {t['entry_dt']:<18} {t['exit_dt']:<18} "
          f"{t['entry']:>8.2f} {t['exit']:>8.2f} {t['rr']:>5} "
          f"{t.get('sl_type','?'):>7} {t.get('sl_dist',0):>7.2f} "
          f"{t['days_held']:>5} {icon}{t['result']:<9} "
          f"Rs{t['pnl']:>+7,.0f}", flush=True)

# Final comparison
print(f"\n  COMPARISON:", flush=True)
print(f"  {'Approach':<40} {'Trades':>7} {'WR%':>6} {'P&L':>12}", flush=True)
print(f"  {'-'*70}", flush=True)
print(f"  {'Baseline Approach B (daily SL)':<40} {'49':>7} {'57.1%':>6} {'Rs +28,249':>12}", flush=True)
print(f"  {'Hybrid SL (this test)':<40} {total_t:>7} {wr_t:>5}% {total_pnl:>+12,.0f}", flush=True)

elapsed=(datetime.now()-start_time).seconds
print(f"\n  Total time: {elapsed}s", flush=True)
conn_d.close()
conn_15.close()
print("\nDone!", flush=True)
