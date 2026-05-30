"""
EOD Signal Backtest
Signal: 4:15 PM using COMPLETE daily candle
Entry : Next day LIMIT at L3 (fills if price touches L3)
Exit  : GTT SL at L4 | GTT Target at HV High
"""
import sqlite3, json, time
from datetime import datetime, timedelta
from collections import defaultdict

print("="*70, flush=True)
print("  EOD SIGNAL BACKTEST", flush=True)
print("  Signal at 4:15 PM | LIMIT next day | GTT SL+Target", flush=True)
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
print(f"\nFilters (EOD — all confirmed):", flush=True)
print(f"  ✅ Green candle (close > open) — confirmed at EOD", flush=True)
print(f"  ✅ Volume >= 1.5x — confirmed at EOD", flush=True)
print(f"  ✅ HV alignment within 4%", flush=True)
print(f"  ✅ RR >= 8x (raised from 5x)", flush=True)
print(f"  ✅ HV vol ratio <= 10x", flush=True)
print(f"  ✅ Price >= Rs 50", flush=True)
print(f"\nEntry logic:", flush=True)
print(f"  Signal fires today at EOD", flush=True)
print(f"  LIMIT order placed at L3 for TOMORROW", flush=True)
print(f"  If tomorrow's price touches L3 → fills", flush=True)
print(f"  If tomorrow gaps above L3 → NOT filled (missed)", flush=True)

def get_cam(h, l, cl):
    rng = h - l
    return cl - rng*1.1/4, cl - rng*1.1/2

CAPITAL     = 60000
MAX_POS     = 3
RISK        = 1000
MAX_POS_VAL = 20000
MIN_VOL_R   = 1.5
MIN_RR      = 8.0  # raised from 5x
MAX_RR      = 20.0
COOLDOWN    = 10

open_positions = {}
pending_orders = {}  # sym → {l3, l4, target, shares, rr, signal_date}
capital        = CAPITAL
trade_log      = []
sl_cooldown    = {}
monthly_stats  = defaultdict(lambda:{
    'trades':0,'wins':0,'sl':0,'open':0,'pnl':0.0,
    'signals':0,'filled':0,'not_filled':0
})

fill_stats = {'filled':0, 'not_filled':0, 'gap_up':0, 'no_touch':0}

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
              f"Pending: {len(pending_orders)} | "
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

    # ── Step 1: Monitor open positions ───────────────────────
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
                    'signal_dt' : pos['signal_dt'],
                    'entry_dt'  : pos['entry_dt'],
                    'exit_dt'   : bar['datetime'][:16],
                    'entry'     : pos['entry'],
                    'exit'      : pos['sl'],
                    'result'    : 'SL',
                    'pnl'       : round(pnl, 0),
                    'shares'    : pos['shares'],
                    'rr'        : pos['rr'],
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
                    'signal_dt' : pos['signal_dt'],
                    'entry_dt'  : pos['entry_dt'],
                    'exit_dt'   : bar['datetime'][:16],
                    'entry'     : pos['entry'],
                    'exit'      : pos['target'],
                    'result'    : 'WIN',
                    'pnl'       : round(pnl, 0),
                    'shares'    : pos['shares'],
                    'rr'        : pos['rr'],
                    'month'     : pos['entry_dt'][:7],
                    'days_held' : pos.get('days_held', 0),
                })
                break

    for sym in to_close:
        if sym in open_positions:
            del open_positions[sym]

    for sym in open_positions:
        open_positions[sym]['days_held'] = \
            open_positions[sym].get('days_held', 0) + 1

    # ── Step 2: Check pending LIMIT orders from yesterday ────
    # These were set by EOD signal yesterday
    filled_today  = []
    expired_today = []

    for sym, order in list(pending_orders.items()):
        if sym in open_positions:
            expired_today.append(sym)
            continue
        if len(open_positions) >= MAX_POS:
            expired_today.append(sym)
            continue

        candles = sym_candles.get(sym, [])
        if not candles:
            expired_today.append(sym)
            fill_stats['no_touch'] += 1
            monthly_stats[month]['not_filled'] += 1
            continue

        # Check if first candle already above L3×1.02 (gap up)
        first_open = candles[0]['open']
        if first_open > order['l3'] * 1.02:
            fill_stats['gap_up'] += 1
            fill_stats['not_filled'] += 1
            monthly_stats[month]['not_filled'] += 1
            expired_today.append(sym)
            continue

        # Check if price touches L3 today
        filled    = False
        fill_bar  = None
        fill_idx  = 0

        for ci, bar in enumerate(candles):
            if bar['low'] <= order['l3']:
                # Check next candle (Approach B)
                if ci + 1 >= len(candles):
                    break
                next_bar  = candles[ci + 1]
                next_open = next_bar['open']

                if next_open <= order['l3']:    continue
                if next_open > order['l3']*1.02: break
                if next_open >= order['target']: break
                if order['shares'] * next_open > capital: break

                # ✅ Filled!
                filled     = True
                fill_bar   = next_bar
                fill_idx   = ci + 1
                fill_price = next_open
                break

        if filled and fill_bar:
            capital -= order['shares'] * fill_price
            open_positions[sym] = {
                'entry'     : fill_price,
                'sl'        : order['l4'],
                'target'    : order['target'],
                'shares'    : order['shares'],
                'entry_dt'  : fill_bar['datetime'][:16],
                'signal_dt' : order['signal_dt'],
                'rr'        : order['rr'],
                'days_held' : 0,
            }
            fill_stats['filled'] += 1
            monthly_stats[month]['filled'] += 1

            # Check rest of today for SL/Target
            for future_bar in candles[fill_idx+1:]:
                if sym not in open_positions: break
                if future_bar['low'] <= order['l4']:
                    pnl = (order['l4'] - fill_price) * order['shares']
                    capital += order['shares'] * fill_price + pnl
                    monthly_stats[month]['sl']     += 1
                    monthly_stats[month]['pnl']    += pnl
                    monthly_stats[month]['trades'] += 1
                    sl_cooldown[sym] = today
                    del open_positions[sym]
                    trade_log.append({
                        'sym'      : sym,
                        'signal_dt': order['signal_dt'],
                        'entry_dt' : fill_bar['datetime'][:16],
                        'exit_dt'  : future_bar['datetime'][:16],
                        'entry'    : fill_price,
                        'exit'     : order['l4'],
                        'result'   : 'SL',
                        'pnl'      : round(pnl, 0),
                        'shares'   : order['shares'],
                        'rr'       : order['rr'],
                        'month'    : month,
                        'days_held': 0,
                    })
                    break
                elif future_bar['high'] >= order['target']:
                    pnl = (order['target'] - fill_price) * order['shares']
                    capital += order['shares'] * fill_price + pnl
                    monthly_stats[month]['wins']   += 1
                    monthly_stats[month]['pnl']    += pnl
                    monthly_stats[month]['trades'] += 1
                    del open_positions[sym]
                    trade_log.append({
                        'sym'      : sym,
                        'signal_dt': order['signal_dt'],
                        'entry_dt' : fill_bar['datetime'][:16],
                        'exit_dt'  : future_bar['datetime'][:16],
                        'entry'    : fill_price,
                        'exit'     : order['target'],
                        'result'   : 'WIN',
                        'pnl'      : round(pnl, 0),
                        'shares'   : order['shares'],
                        'rr'       : order['rr'],
                        'month'    : month,
                        'days_held': 0,
                    })
                    break
            filled_today.append(sym)
        else:
            fill_stats['no_touch'] += 1
            fill_stats['not_filled'] += 1
            monthly_stats[month]['not_filled'] += 1
            expired_today.append(sym)

    for sym in filled_today + expired_today:
        if sym in pending_orders:
            del pending_orders[sym]

    # ── Step 3: EOD scan — generate signals for TOMORROW ─────
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
        if sym in pending_orders: continue

        if sym in sl_cooldown:
            sl_dt = datetime.strptime(sl_cooldown[sym], '%Y-%m-%d')
            if (today_dt - sl_dt).days < COOLDOWN: continue

        # Today's COMPLETE daily candle
        cd.execute('''
            SELECT open, high, low, close, volume
            FROM daily_prices WHERE symbol=? AND date=?
        ''', (sym, today))
        day_bar = cd.fetchone()
        if not day_bar: continue

        # ── EOD FILTERS (all confirmed!) ──────────────────────
        # Green candle
        if day_bar['close'] <= day_bar['open']: continue

        # L3/L4 from TODAY's complete candle
        l3, l4 = get_cam(day_bar['high'], day_bar['low'], day_bar['close'])

        # Avg volume
        cd.execute('''
            SELECT AVG(volume) as av FROM (
                SELECT volume FROM daily_prices
                WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 20
            )
        ''', (sym, today))
        avg_vol = cd.fetchone()['av'] or 0
        if avg_vol == 0: continue

        day_vol_r = round(day_bar['volume']/avg_vol, 2)
        if day_vol_r < MIN_VOL_R: continue  # volume confirmed

        hv_vol_r = round(hv_vol/avg_vol, 2)
        if hv_vol_r > 10.0: continue
        if abs(l3-hv_low)/hv_low*100 > 4.0: continue
        if l3 < 50: continue

        risk   = l3 - l4
        reward = hv_high - l3
        if risk <= 0 or reward <= 0: continue
        rr = round(reward/risk, 1)
        if not (MIN_RR <= rr <= MAX_RR): continue

        shares = min(int(RISK/risk), int(MAX_POS_VAL/l3))
        if shares < 1: continue

        # ✅ Valid EOD signal → place LIMIT for tomorrow
        pending_orders[sym] = {
            'l3'         : l3,
            'l4'         : l4,
            'target'     : hv_high,
            'shares'     : shares,
            'rr'         : rr,
            'signal_dt'  : today,
            'vol_r'      : day_vol_r,
        }
        monthly_stats[month]['signals'] += 1

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
            'sym':sym,'signal_dt':pos['signal_dt'],
            'entry_dt':pos['entry_dt'],'exit_dt':'OPEN',
            'entry':pos['entry'],'exit':r['close'],'result':'OPEN',
            'pnl':round(pnl,0),'shares':pos['shares'],'rr':pos['rr'],
            'month':pos['entry_dt'][:7],'days_held':pos.get('days_held',0),
        })

# ── Results ───────────────────────────────────────────────────
print(f"\n{'='*70}", flush=True)
print(f"  EOD SIGNAL BACKTEST RESULTS", flush=True)
print(f"{'='*70}", flush=True)

total_t=total_w=total_sl=total_o=0
total_pnl=0.0
total_sig=total_fill=total_nfill=0

print(f"\n  {'Month':<10} {'Sig':>5} {'Fill':>5} {'NFill':>6} {'Trades':>7} {'Wins':>6} {'SL':>5} {'WR%':>6} {'P&L':>12}", flush=True)
print(f"  {'-'*70}", flush=True)
for month in sorted(monthly_stats.keys()):
    m   = monthly_stats[month]
    wr  = round(m['wins']/m['trades']*100,1) if m['trades']>0 else 0
    frt = round(m['filled']/(m['signals'])*100,1) if m['signals']>0 else 0
    total_t  +=m['trades']; total_w+=m['wins']
    total_sl +=m['sl'];     total_o+=m['open']
    total_pnl+=m['pnl']
    total_sig+=m['signals']; total_fill+=m['filled']
    total_nfill+=m['not_filled']
    print(f"  {month:<10} {m['signals']:>5} {m['filled']:>5} {m['not_filled']:>6} {m['trades']:>7} {m['wins']:>6} {m['sl']:>5} {wr:>5}% {m['pnl']:>+12,.0f}", flush=True)

print(f"  {'-'*70}", flush=True)
wr_t  = round(total_w/total_t*100,1) if total_t>0 else 0
fill_r= round(total_fill/total_sig*100,1) if total_sig>0 else 0
print(f"  {'TOTAL':<10} {total_sig:>5} {total_fill:>5} {total_nfill:>6} {total_t:>7} {total_w:>6} {total_sl:>5} {wr_t:>5}% {total_pnl:>+12,.0f}", flush=True)

wins_l=[t for t in trade_log if t['result']=='WIN']
sl_l  =[t for t in trade_log if t['result']=='SL']
avg_w =round(sum(t['pnl'] for t in wins_l)/len(wins_l),0) if wins_l else 0
avg_l =round(sum(t['pnl'] for t in sl_l)/len(sl_l),0) if sl_l else 0
wl_r  =round(abs(avg_w/avg_l),1) if avg_l and avg_w else 0

print(f"\n  KEY STATS:", flush=True)
print(f"  Total signals  : {total_sig}", flush=True)
print(f"  Fill rate      : {fill_r}% ({total_fill} filled / {total_nfill} not filled)", flush=True)
print(f"  Gap ups missed : {fill_stats['gap_up']}", flush=True)
print(f"  No touch missed: {fill_stats['no_touch']}", flush=True)
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
    b=[t for t in trade_log if d_min<=t['days_held']<d_max and t['result']!='OPEN']
    if not b: continue
    w=[t for t in b if t['result']=='WIN']
    wr=round(len(w)/len(b)*100,1)
    avg_p=round(sum(t['pnl'] for t in b)/len(b),0)
    print(f"  {label:<12}: Trades={len(b):>4} WR={wr:>5}% AvgPnL=Rs{avg_p:>+7,.0f}", flush=True)

# RR breakdown
print(f"\n  R:R BREAKDOWN:", flush=True)
for rr_min,rr_max,label in [(8,12,'8-12x'),(12,16,'12-16x'),(16,21,'16-20x')]:
    b=[t for t in trade_log if rr_min<=t['rr']<rr_max and t['result']!='OPEN']
    if not b: continue
    w=[t for t in b if t['result']=='WIN']
    sl2=[t for t in b if t['result']=='SL']
    wr=round(len(w)/len(b)*100,1)
    avg_w2=round(sum(t['pnl'] for t in w)/len(w),0) if w else 0
    avg_l2=round(sum(t['pnl'] for t in sl2)/len(sl2),0) if sl2 else 0
    tot=round(sum(t['pnl'] for t in b),0)
    print(f"  RR {label:<8}: Trades={len(b):>4} WR={wr:>5}% AvgWin=Rs{avg_w2:>+7,.0f} AvgLoss=Rs{avg_l2:>+7,.0f} Total=Rs{tot:>+9,.0f}", flush=True)

# Trade log
print(f"\n  TRADE LOG:", flush=True)
print(f"  {'Stock':<12} {'Signal':<12} {'Entry DT':<18} {'Exit DT':<18} {'Entry':>8} {'Exit':>8} {'RR':>5} {'Days':>5} {'Result':<10} {'P&L':>8}", flush=True)
print(f"  {'-'*110}", flush=True)
for t in sorted(trade_log, key=lambda x: x['entry_dt']):
    icon='✅' if t['result']=='WIN' else '❌' if t['result']=='SL' else '⏳'
    print(f"  {t['sym']:<12} {t['signal_dt']:<12} {t['entry_dt']:<18} {t['exit_dt']:<18} "
          f"{t['entry']:>8.2f} {t['exit']:>8.2f} {t['rr']:>5} "
          f"{t['days_held']:>5} {icon}{t['result']:<9} Rs{t['pnl']:>+7,.0f}", flush=True)

print(f"\n  FINAL COMPARISON:", flush=True)
print(f"  {'Approach':<40} {'Trades':>7} {'WR%':>6} {'P&L':>12}", flush=True)
print(f"  {'-'*70}", flush=True)
print(f"  {'Daily BT (hindsight)':<40} {'28':>7} {'60.7%':>6} {'Rs +23,291':>12}", flush=True)
print(f"  {'Approach B (EOD filters+15min)':<40} {'49':>7} {'57.1%':>6} {'Rs +28,249':>12}", flush=True)
print(f"  {'Realistic (no EOD filters)':<40} {'122':>7} {'16.4%':>6} {'Rs    +846':>12}", flush=True)
print(f"  {'EOD Signal (this test)':<40} {total_t:>7} {wr_t:>5}% {total_pnl:>+12,.0f}", flush=True)

elapsed=(datetime.now()-start_time).seconds
print(f"\n  Total time: {elapsed}s", flush=True)
conn_d.close()
conn_15.close()
print("\nDone!", flush=True)
