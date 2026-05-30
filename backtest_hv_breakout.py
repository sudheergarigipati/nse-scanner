"""
HV HIGH BREAKOUT BACKTEST — Proper Version
Rules:
  1. HV day in 7-180 day window (bullish candle)
  2. Stock below HV High for 5+ days before breakout
  3. Breakout = 15-min candle closes ABOVE HV High with volume >= 2x
  4. Entry = next 15-min candle open after breakout candle
  5. SL = HV High - 0.5× HV range
  6. Target = HV High + 1.5× HV range
  7. Capital = Rs 60,000 | Max 3 positions | Rs 1,000 risk
  8. Cooldown = 7 days per stock after any exit
  9. No duplicate entries same stock same day
"""
import sqlite3, json, time
from datetime import datetime, timedelta
from collections import defaultdict

print("="*70, flush=True)
print("  HV HIGH BREAKOUT BACKTEST", flush=True)
print("  15-min entry | Proper capital management", flush=True)
print("  All stocks | Mar-May 2026", flush=True)
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

print(f"Available days: {len(available_days)}", flush=True)
print(f"Date range    : {available_days[0]} to {available_days[-1]}", flush=True)
print(f"\nStrategy rules:", flush=True)
print(f"  HV window  : 7-180 calendar days", flush=True)
print(f"  Breakout   : 15-min close > HV High + vol >= 2x avg", flush=True)
print(f"  Entry      : Next 15-min candle open", flush=True)
print(f"  SL         : HV High - 0.5× range", flush=True)
print(f"  Target     : HV High + 1.5× range", flush=True)
print(f"  Capital    : Rs 60,000 | 3 positions | Rs 1,000 risk", flush=True)
print(f"  Cooldown   : 7 days after any exit", flush=True)

# Config
CAPITAL     = 60000
MAX_POS     = 3
RISK        = 1000
MAX_POS_VAL = 20000
COOLDOWN    = 7
HV_WIN_MIN  = 7
HV_WIN_MAX  = 180
MIN_VOL_R   = 2.0   # breakout volume minimum
MIN_BELOW   = 5     # days stock must be below HV High before breakout
SL_MULT     = 0.5   # SL = HV High - SL_MULT × range
TGT_MULT    = 1.5   # Target = HV High + TGT_MULT × range

open_positions = {}
capital        = CAPITAL
trade_log      = []
cooldown_map   = {}  # sym → last exit date
monthly_stats  = defaultdict(lambda:{
    'signals':0,'filled':0,'trades':0,
    'wins':0,'sl':0,'open':0,'pnl':0.0
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
                cooldown_map[sym] = today
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
                    'hv_high'  : pos['hv_high'],
                    'hv_range' : pos['hv_range'],
                    'vol_r'    : pos['vol_r'],
                    'tests'    : pos['tests'],
                    'month'    : pos['entry_dt'][:7],
                    'days_held': pos.get('days_held', 0),
                })
                break
            elif bar['high'] >= pos['target']:
                pnl = (pos['target'] - pos['entry']) * pos['shares']
                capital += pos['shares'] * pos['entry'] + pnl
                monthly_stats[pos['entry_dt'][:7]]['wins']   += 1
                monthly_stats[pos['entry_dt'][:7]]['pnl']    += pnl
                monthly_stats[pos['entry_dt'][:7]]['trades'] += 1
                cooldown_map[sym] = today
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
                    'hv_high'  : pos['hv_high'],
                    'hv_range' : pos['hv_range'],
                    'vol_r'    : pos['vol_r'],
                    'tests'    : pos['tests'],
                    'month'    : pos['entry_dt'][:7],
                    'days_held': pos.get('days_held', 0),
                })
                break

    for sym in to_close:
        if sym in open_positions: del open_positions[sym]
    for sym in open_positions:
        open_positions[sym]['days_held'] = \
            open_positions[sym].get('days_held', 0) + 1

    # ── Find new breakout signals ─────────────────────────────
    date_min = (today_dt - timedelta(days=HV_WIN_MAX)).strftime('%Y-%m-%d')
    date_max = (today_dt - timedelta(days=HV_WIN_MIN)).strftime('%Y-%m-%d')

    # Get HV candidates for today
    cd.execute('''
        SELECT d1.symbol,
               d1.high  as hv_high,
               d1.low   as hv_low,
               d1.volume as hv_vol,
               d1.date  as hv_date
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

    signals_today = set()  # avoid duplicate signals same day

    for row in candidates:
        sym      = row['symbol']
        hv_high  = row['hv_high']
        hv_low   = row['hv_low']
        hv_vol   = row['hv_vol']
        hv_range = hv_high - hv_low
        if hv_range <= 0: continue

        if sym in open_positions: continue
        if sym in signals_today: continue
        if len(open_positions) >= MAX_POS: continue

        # Cooldown check
        if sym in cooldown_map:
            last_exit = datetime.strptime(cooldown_map[sym], '%Y-%m-%d')
            if (today_dt - last_exit).days < COOLDOWN: continue

        # Avg volume
        cd.execute('''
            SELECT AVG(volume) as av FROM (
                SELECT volume FROM daily_prices
                WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 20
            )
        ''', (sym, today))
        avg_vol = cd.fetchone()['av'] or 0
        if avg_vol == 0: continue

        # Calculate levels
        sl     = round(hv_high - SL_MULT * hv_range, 2)
        target = round(hv_high + TGT_MULT * hv_range, 2)
        risk   = hv_high - sl
        reward = target - hv_high
        if risk <= 0 or reward <= 0: continue
        rr     = round(reward/risk, 1)

        shares = min(int(RISK/risk), int(MAX_POS_VAL/hv_high))
        if shares < 1: continue

        # Check stock was BELOW HV High for MIN_BELOW days before today
        cd.execute('''
            SELECT COUNT(*) as cnt FROM daily_prices
            WHERE symbol=? AND date<? AND date>=?
            AND close < ?
        ''', (sym, today,
              (today_dt - timedelta(days=30)).strftime('%Y-%m-%d'),
              hv_high))
        below_count = c15.fetchone() or cd.fetchone()
        # re-fetch properly
        cd.execute('''
            SELECT COUNT(*) as cnt FROM daily_prices
            WHERE symbol=? AND date<? AND date>=?
            AND close < ?
        ''', (sym, today,
              (today_dt - timedelta(days=30)).strftime('%Y-%m-%d'),
              hv_high))
        below_count = cd.fetchone()['cnt']
        if below_count < MIN_BELOW: continue

        # Count resistance tests (how many times touched HV High)
        cd.execute('''
            SELECT COUNT(*) as tests FROM daily_prices
            WHERE symbol=? AND date<? AND date>=?
            AND high >= ? AND close < ?
        ''', (sym, today,
              (today_dt - timedelta(days=90)).strftime('%Y-%m-%d'),
              hv_high * 0.98,
              hv_high))
        tests = cd.fetchone()['tests']

        # ── Check 15-min candles for breakout ─────────────────
        candles = sym_candles.get(sym, [])
        if len(candles) < 2: continue

        monthly_stats[month]['signals'] += 1

        for ci, bar in enumerate(candles[:-1]):
            # Breakout candle conditions:
            # 1. Close above HV High
            # 2. Volume >= 2x avg per candle
            candle_avg_vol = avg_vol / 26  # expected per 15-min candle
            candle_vol_r   = round(bar['volume'] / candle_avg_vol, 1) \
                             if candle_avg_vol > 0 else 0

            if bar['close'] <= hv_high: continue
            if candle_vol_r < MIN_VOL_R: continue

            # ✅ Breakout confirmed!
            # Enter at NEXT candle open
            next_bar  = candles[ci+1]
            next_open = next_bar['open']

            # Entry validation
            if next_open <= hv_high: continue  # gapped below HV High
            if next_open > hv_high * 1.03: continue  # too far above (>3%)
            if next_open >= target: continue   # above target already
            if shares * next_open > capital: continue

            # ✅ Valid entry!
            entry_price  = next_open
            actual_risk  = entry_price - sl
            if actual_risk <= 0: continue
            actual_shares = min(int(RISK/actual_risk), int(MAX_POS_VAL/entry_price))
            if actual_shares < 1: continue

            capital -= actual_shares * entry_price
            open_positions[sym] = {
                'entry'    : entry_price,
                'sl'       : sl,
                'target'   : target,
                'shares'   : actual_shares,
                'entry_dt' : next_bar['datetime'][:16],
                'hv_high'  : hv_high,
                'hv_range' : round(hv_range, 2),
                'vol_r'    : candle_vol_r,
                'tests'    : tests,
                'days_held': 0,
            }
            signals_today.add(sym)
            monthly_stats[month]['filled'] += 1

            # Check rest of today for exit
            for future_bar in candles[ci+2:]:
                if sym not in open_positions: break
                if future_bar['low'] <= sl:
                    pnl = (sl - entry_price) * actual_shares
                    capital += actual_shares * entry_price + pnl
                    monthly_stats[month]['sl']     += 1
                    monthly_stats[month]['pnl']    += pnl
                    monthly_stats[month]['trades'] += 1
                    cooldown_map[sym] = today
                    del open_positions[sym]
                    trade_log.append({
                        'sym'      : sym,
                        'entry_dt' : next_bar['datetime'][:16],
                        'exit_dt'  : future_bar['datetime'][:16],
                        'entry'    : entry_price,
                        'exit'     : sl,
                        'result'   : 'SL',
                        'pnl'      : round(pnl, 0),
                        'shares'   : actual_shares,
                        'hv_high'  : hv_high,
                        'hv_range' : round(hv_range, 2),
                        'vol_r'    : candle_vol_r,
                        'tests'    : tests,
                        'month'    : month,
                        'days_held': 0,
                    })
                    break
                elif future_bar['high'] >= target:
                    pnl = (target - entry_price) * actual_shares
                    capital += actual_shares * entry_price + pnl
                    monthly_stats[month]['wins']   += 1
                    monthly_stats[month]['pnl']    += pnl
                    monthly_stats[month]['trades'] += 1
                    cooldown_map[sym] = today
                    del open_positions[sym]
                    trade_log.append({
                        'sym'      : sym,
                        'entry_dt' : next_bar['datetime'][:16],
                        'exit_dt'  : future_bar['datetime'][:16],
                        'entry'    : entry_price,
                        'exit'     : target,
                        'result'   : 'WIN',
                        'pnl'      : round(pnl, 0),
                        'shares'   : actual_shares,
                        'hv_high'  : hv_high,
                        'hv_range' : round(hv_range, 2),
                        'vol_r'    : candle_vol_r,
                        'tests'    : tests,
                        'month'    : month,
                        'days_held': 0,
                    })
                    break
            break  # only first breakout candle per stock per day

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
            'pnl':round(pnl,0),'shares':pos['shares'],
            'hv_high':pos['hv_high'],'hv_range':pos['hv_range'],
            'vol_r':pos['vol_r'],'tests':pos['tests'],
            'month':pos['entry_dt'][:7],'days_held':pos.get('days_held',0),
        })

# ── Results ───────────────────────────────────────────────────
print(f"\n{'='*70}", flush=True)
print(f"  HV HIGH BREAKOUT BACKTEST RESULTS", flush=True)
print(f"{'='*70}", flush=True)

total_t=total_w=total_sl=total_o=0
total_pnl=0.0
total_sig=total_fill=0

print(f"\n  {'Month':<10} {'Sig':>5} {'Fill':>5} {'Trades':>7} {'Wins':>6} {'SL':>5} {'Open':>5} {'WR%':>6} {'P&L':>12}", flush=True)
print(f"  {'-'*65}", flush=True)
for month in sorted(monthly_stats.keys()):
    m  = monthly_stats[month]
    wr = round(m['wins']/m['trades']*100,1) if m['trades']>0 else 0
    total_t  +=m['trades']; total_w+=m['wins']
    total_sl +=m['sl'];     total_o+=m['open']
    total_pnl+=m['pnl']
    total_sig+=m['signals']; total_fill+=m['filled']
    print(f"  {month:<10} {m['signals']:>5} {m['filled']:>5} {m['trades']:>7} "
          f"{m['wins']:>6} {m['sl']:>5} {m['open']:>5} {wr:>5}% {m['pnl']:>+12,.0f}", flush=True)

print(f"  {'-'*65}", flush=True)
wr_t  = round(total_w/total_t*100,1) if total_t>0 else 0
fill_r= round(total_fill/total_sig*100,1) if total_sig>0 else 0
print(f"  {'TOTAL':<10} {total_sig:>5} {total_fill:>5} {total_t:>7} "
      f"{total_w:>6} {total_sl:>5} {total_o:>5} {wr_t:>5}% {total_pnl:>+12,.0f}", flush=True)

wins_l = [t for t in trade_log if t['result']=='WIN']
sl_l   = [t for t in trade_log if t['result']=='SL']
avg_w  = round(sum(t['pnl'] for t in wins_l)/len(wins_l),0) if wins_l else 0
avg_l  = round(sum(t['pnl'] for t in sl_l)/len(sl_l),0) if sl_l else 0
wl_r   = round(abs(avg_w/avg_l),1) if avg_l and avg_w else 0

print(f"\n  KEY STATS:", flush=True)
print(f"  Total signals  : {total_sig}", flush=True)
print(f"  Fill rate      : {fill_r}% ({total_fill} filled)", flush=True)
print(f"  Total trades   : {total_t}", flush=True)
print(f"  Win rate       : {wr_t}%", flush=True)
print(f"  Avg win        : Rs {avg_w:+,.0f}", flush=True)
print(f"  Avg loss       : Rs {avg_l:+,.0f}", flush=True)
print(f"  Win:Loss ratio : {wl_r}:1", flush=True)
print(f"  Total P&L      : Rs {total_pnl:+,.0f}", flush=True)
print(f"  Final capital  : Rs {capital:,.0f}", flush=True)

# Days held
print(f"\n  DAYS HELD:", flush=True)
for d_min,d_max,label in [(0,1,'Same day'),(1,2,'Day 1'),(2,3,'Day 2'),(3,4,'Day 3'),(4,99,'Day 4+')]:
    b=[t for t in trade_log if d_min<=t['days_held']<d_max and t['result'] in ['WIN','SL']]
    if not b: continue
    w=[t for t in b if t['result']=='WIN']
    wr=round(len(w)/len(b)*100,1)
    avg_p=round(sum(t['pnl'] for t in b)/len(b),0)
    print(f"  {label:<12}: Trades={len(b):>4} WR={wr:>5}% AvgPnL=Rs{avg_p:>+7,.0f}", flush=True)

# Vol ratio analysis
print(f"\n  BREAKOUT VOLUME ANALYSIS:", flush=True)
for v_min,v_max,label in [(2,5,'2-5x'),(5,10,'5-10x'),(10,20,'10-20x'),(20,999,'20x+')]:
    b=[t for t in trade_log if v_min<=t['vol_r']<v_max and t['result'] in ['WIN','SL']]
    if not b: continue
    w=[t for t in b if t['result']=='WIN']
    wr=round(len(w)/len(b)*100,1)
    avg_p=round(sum(t['pnl'] for t in b)/len(b),0)
    print(f"  Vol {label:<8}: Trades={len(b):>4} WR={wr:>5}% AvgPnL=Rs{avg_p:>+7,.0f}", flush=True)

# Resistance tests analysis
print(f"\n  RESISTANCE TESTS ANALYSIS:", flush=True)
for t_min,t_max,label in [(0,5,'0-5 tests'),(5,15,'5-15 tests'),(15,999,'15+ tests')]:
    b=[t for t in trade_log if t_min<=t['tests']<t_max and t['result'] in ['WIN','SL']]
    if not b: continue
    w=[t for t in b if t['result']=='WIN']
    wr=round(len(w)/len(b)*100,1)
    avg_p=round(sum(t['pnl'] for t in b)/len(b),0)
    print(f"  {label:<12}: Trades={len(b):>4} WR={wr:>5}% AvgPnL=Rs{avg_p:>+7,.0f}", flush=True)

# Full trade log
print(f"\n  TRADE LOG:", flush=True)
print(f"  {'Stock':<12} {'Entry DT':<18} {'Exit DT':<18} {'Entry':>8} {'Exit':>8} "
      f"{'HVHigh':>8} {'Range':>7} {'Vol':>6} {'Tests':>6} {'Days':>5} {'Result':<10} {'P&L':>8}", flush=True)
print(f"  {'-'*115}", flush=True)
for t in sorted(trade_log, key=lambda x: x['entry_dt']):
    icon='✅' if t['result']=='WIN' else '❌' if t['result']=='SL' else '⏳'
    print(f"  {t['sym']:<12} {t['entry_dt']:<18} {t['exit_dt']:<18} "
          f"{t['entry']:>8.2f} {t['exit']:>8.2f} "
          f"{t['hv_high']:>8.2f} {t['hv_range']:>7.2f} "
          f"{t['vol_r']:>5.1f}x {t['tests']:>6} {t['days_held']:>5} "
          f"{icon}{t['result']:<9} Rs{t['pnl']:>+7,.0f}", flush=True)

# Comparison
print(f"\n  COMPARISON:", flush=True)
print(f"  {'Strategy':<40} {'Trades':>7} {'WR%':>6} {'P&L':>12}", flush=True)
print(f"  {'-'*70}", flush=True)
print(f"  {'HV Low Bounce (Approach B)':<40} {'49':>7} {'57.1%':>6} {'Rs +28,249':>12}", flush=True)
print(f"  {'HV High Breakout (this)':<40} {total_t:>7} {wr_t:>5}% {total_pnl:>+12,.0f}", flush=True)

elapsed=(datetime.now()-start_time).seconds
print(f"\n  Total time: {elapsed}s", flush=True)
conn_d.close()
conn_15.close()
print("\nDone!", flush=True)
