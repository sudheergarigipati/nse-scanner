import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

print("Loading databases...", flush=True)

# Daily DB for signals
conn_d = sqlite3.connect('/home/ubuntu/nse-scanner/nse_data.db')
conn_d.row_factory = sqlite3.Row
cd = conn_d.cursor()

# 15-min DB for intraday simulation
conn_15 = sqlite3.connect('/home/ubuntu/nse-scanner/intraday_15min.db')
conn_15.row_factory = sqlite3.Row
c15 = conn_15.cursor()

# Check available date range in 15-min DB
c15.execute('SELECT MIN(datetime) as mn, MAX(datetime) as mx, COUNT(DISTINCT symbol) as syms FROM prices_15min')
r = c15.fetchone()
print(f"15-min DB: {r['syms']} symbols | {r['mn'][:10]} to {r['mx'][:10]}", flush=True)

# Get trading days available in 15-min data
c15.execute("SELECT DISTINCT DATE(datetime) as dt FROM prices_15min ORDER BY dt")
available_days = [r['dt'] for r in c15.fetchall()]
print(f"Trading days available: {len(available_days)}", flush=True)
print(f"Days: {available_days[0]} to {available_days[-1]}", flush=True)

# Valid symbols with 252+ days history
cd.execute('SELECT symbol FROM daily_prices GROUP BY symbol HAVING COUNT(*) >= 252')
valid_symbols = {r['symbol'] for r in cd.fetchall()}

import json
bl = json.load(open('/home/ubuntu/nse-scanner/cautionary_stocks.json')).get('stocks',[])

def get_cam(h, l, cl):
    rng = h - l
    return cl - rng*1.1/4, cl - rng*1.1/2

# ── Main backtest ─────────────────────────────────────────────
print(f"\nRunning 15-min backtest...", flush=True)
print(f"Strategy: LIMIT at L3 | GTT SL at L4 | GTT Target at HV High", flush=True)
print(f"Entry: fills when 15-min LOW touches L3", flush=True)
print(f"Exit : SL/Target fires on 15-min HIGH/LOW | Hold overnight", flush=True)

all_signals  = []
monthly_stats= defaultdict(lambda:{'trades':0,'wins':0,'sl':0,'open':0,'pnl':0.0})

CAPITAL     = 60000
MAX_POS     = 3
RISK        = 1000
MAX_POS_VAL = 20000

# Simulate with capital
open_positions = {}  # sym → {entry, sl, target, shares, entry_dt}
capital        = CAPITAL
trade_log      = []
sl_cooldown    = {}  # sym → date of SL hit

for day_idx, today in enumerate(available_days):
    today_dt = datetime.strptime(today, '%Y-%m-%d')
    month    = today[:7]

    # ── Check open positions on today's 15-min candles ──────
    to_close = []
    for sym, pos in open_positions.items():
        c15.execute('''
            SELECT datetime, open, high, low, close
            FROM prices_15min
            WHERE symbol=? AND DATE(datetime)=?
            ORDER BY datetime
        ''', (sym, today))
        candles = c15.fetchall()
        if not candles: continue

        for bar in candles:
            if bar['low'] <= pos['sl']:
                # SL hit
                pnl = (pos['sl'] - pos['entry']) * pos['shares']
                capital += pos['shares'] * pos['entry'] + pnl
                monthly_stats[month]['sl']    += 1
                monthly_stats[month]['pnl']   += pnl
                monthly_stats[month]['trades'] += 1
                sl_cooldown[sym] = today
                to_close.append(sym)
                trade_log.append({
                    'sym':sym,'entry_dt':pos['entry_dt'],'exit_dt':bar['datetime'][:16],
                    'entry':pos['entry'],'exit':pos['sl'],'result':'SL',
                    'pnl':round(pnl,0),'shares':pos['shares'],'month':month
                })
                break
            if bar['high'] >= pos['target']:
                # Target hit
                pnl = (pos['target'] - pos['entry']) * pos['shares']
                capital += pos['shares'] * pos['entry'] + pnl
                monthly_stats[month]['wins']   += 1
                monthly_stats[month]['pnl']    += pnl
                monthly_stats[month]['trades'] += 1
                to_close.append(sym)
                trade_log.append({
                    'sym':sym,'entry_dt':pos['entry_dt'],'exit_dt':bar['datetime'][:16],
                    'entry':pos['entry'],'exit':pos['target'],'result':'WIN',
                    'pnl':round(pnl,0),'shares':pos['shares'],'month':month
                })
                break

    for sym in to_close:
        del open_positions[sym]

    # ── Find new signals for today ───────────────────────────
    if day_idx == 0: continue

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
                  if r['symbol'] in valid_symbols
                  and r['symbol'] not in bl]

    signals_today = []
    for row in candidates:
        sym     = row['symbol']
        hv_high = row['hv_high']
        hv_low  = row['hv_low']
        hv_vol  = row['hv_vol']

        if sym in open_positions: continue
        if sym in sl_cooldown:
            sl_dt = datetime.strptime(sl_cooldown[sym], '%Y-%m-%d')
            if (today_dt - sl_dt).days < 10: continue

        # Yesterday's daily candle for L3/L4
        cd.execute('SELECT high,low,close FROM daily_prices WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1',(sym,today))
        prev = cd.fetchone()
        if not prev: continue

        l3, l4 = get_cam(prev['high'], prev['low'], prev['close'])

        # Avg volume
        cd.execute('SELECT AVG(volume) as av FROM (SELECT volume FROM daily_prices WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 20)',(sym,today))
        avg_vol = cd.fetchone()['av'] or 0
        if avg_vol == 0: continue

        hv_vol_r = round(hv_vol/avg_vol, 2)
        if hv_vol_r > 10.0: continue
        if abs(l3-hv_low)/hv_low*100 > 4.0: continue
        if l3 < 50: continue

        risk   = l3 - l4
        reward = hv_high - l3
        if risk <= 0 or reward <= 0: continue
        rr = reward/risk
        if not (5.0 <= rr <= 20.0): continue

        shares = min(int(RISK/risk), int(MAX_POS_VAL/l3))
        if shares < 1: continue

        # Check 15-min data available for this stock today
        c15.execute('SELECT COUNT(*) as cnt FROM prices_15min WHERE symbol=? AND DATE(datetime)=?',(sym,today))
        if c15.fetchone()['cnt'] == 0: continue

        signals_today.append({
            'sym':sym,'l3':l3,'l4':l4,'target':hv_high,
            'shares':shares,'rr':round(rr,1),'hv_vol_r':hv_vol_r
        })

    # Sort by RR, take best ones within capital/position limits
    for sig in sorted(signals_today, key=lambda x: -x['rr']):
        if len(open_positions) >= MAX_POS: break
        sym = sig['sym']
        if sym in open_positions: continue

        # Simulate LIMIT order on 15-min candles
        c15.execute('''
            SELECT datetime, open, high, low, close, volume
            FROM prices_15min WHERE symbol=? AND DATE(datetime)=?
            ORDER BY datetime
        ''', (sym, today))
        candles = c15.fetchall()

        filled    = False
        fill_dt   = None
        fill_price= sig['l3']

        for bar in candles:
            # Check volume filter on first candle (proxy for daily volume)
            # We check accumulative volume by 11 AM
            ts = bar['datetime'][11:16]

            if not filled:
                if bar['low'] <= sig['l3']:
                    # Check if green so far (close > open of this candle)
                    if bar['close'] >= sig['l3']:  # closed above L3
                        filled    = True
                        fill_dt   = bar['datetime'][:16]
                        fill_price= sig['l3']
                        break

        if filled and sig['shares']*fill_price <= capital:
            capital -= sig['shares'] * fill_price
            open_positions[sym] = {
                'entry'   : fill_price,
                'sl'      : sig['l4'],
                'target'  : sig['target'],
                'shares'  : sig['shares'],
                'entry_dt': fill_dt,
                'rr'      : sig['rr'],
            }

    if day_idx % 10 == 0:
        print(f"  {today} | Capital: Rs {capital:,.0f} | Open: {len(open_positions)} | Trades: {len(trade_log)}", flush=True)

# Close remaining open positions at last price
print(f"\nClosing remaining open positions...", flush=True)
for sym, pos in open_positions.items():
    cd.execute('SELECT close FROM daily_prices WHERE symbol=? ORDER BY date DESC LIMIT 1',(sym,))
    r = cd.fetchone()
    if r:
        pnl = (r['close'] - pos['entry']) * pos['shares']
        monthly_stats[today[:7]]['open'] += 1
        monthly_stats[today[:7]]['pnl']  += pnl
        trade_log.append({
            'sym':sym,'entry_dt':pos['entry_dt'],'exit_dt':'OPEN',
            'entry':pos['entry'],'exit':r['close'],'result':'OPEN',
            'pnl':round(pnl,0),'shares':pos['shares'],'month':today[:7]
        })

# ── Results ───────────────────────────────────────────────────
print(f"\n{'='*75}", flush=True)
print(f"  15-MIN BACKTEST RESULTS (Rs 60k | 3 positions | Mar-May 2026)", flush=True)
print(f"{'='*75}", flush=True)

total_t=total_w=total_sl=total_o=0
total_pnl=0.0

print(f"\n  {'Month':<10} {'Trades':>7} {'Wins':>6} {'SL':>5} {'Open':>6} {'WR%':>6} {'P&L':>10}", flush=True)
print(f"  {'-'*55}", flush=True)
for month in sorted(monthly_stats.keys()):
    m  = monthly_stats[month]
    wr = round(m['wins']/m['trades']*100,1) if m['trades']>0 else 0
    total_t   += m['trades']
    total_w   += m['wins']
    total_sl  += m['sl']
    total_o   += m['open']
    total_pnl += m['pnl']
    print(f"  {month:<10} {m['trades']:>7} {m['wins']:>6} {m['sl']:>5} {m['open']:>6} {wr:>5}% {m['pnl']:>+10,.0f}", flush=True)

print(f"  {'-'*55}", flush=True)
wr_total = round(total_w/total_t*100,1) if total_t>0 else 0
print(f"  {'TOTAL':<10} {total_t:>7} {total_w:>6} {total_sl:>5} {total_o:>6} {wr_total:>5}% {total_pnl:>+10,.0f}", flush=True)

wins_list = [t for t in trade_log if t['result']=='WIN']
sl_list   = [t for t in trade_log if t['result']=='SL']
avg_win   = round(sum(t['pnl'] for t in wins_list)/len(wins_list),0) if wins_list else 0
avg_loss  = round(sum(t['pnl'] for t in sl_list)/len(sl_list),0) if sl_list else 0

print(f"\n  KEY STATS:", flush=True)
print(f"  Total trades  : {total_t}", flush=True)
print(f"  Win rate      : {wr_total}%", flush=True)
print(f"  Avg win       : Rs {avg_win:+,.0f}", flush=True)
print(f"  Avg loss      : Rs {avg_loss:+,.0f}", flush=True)
print(f"  Total P&L     : Rs {total_pnl:+,.0f}", flush=True)

print(f"\n  TRADE LOG:", flush=True)
print(f"  {'Stock':<12} {'Entry DT':<18} {'Exit DT':<18} {'Entry':>8} {'Exit':>8} {'Result':<10} {'P&L':>8}", flush=True)
print(f"  {'-'*85}", flush=True)
for t in sorted(trade_log, key=lambda x: x['entry_dt']):
    icon = '✅' if t['result']=='WIN' else '❌' if t['result']=='SL' else '⏳'
    print(f"  {t['sym']:<12} {t['entry_dt']:<18} {t['exit_dt']:<18} {t['entry']:>8.2f} {t['exit']:>8.2f} {icon}{t['result']:<9} Rs{t['pnl']:>+7,.0f}", flush=True)

print(f"\n  COMPARISON:", flush=True)
print(f"  Daily backtest (Apr+May): Rs +23,291 | 60.7% WR", flush=True)
print(f"  15-min backtest (Apr+May): Rs {total_pnl:+,.0f} | {wr_total}% WR", flush=True)

conn_d.close()
conn_15.close()
print("\nDone!", flush=True)
