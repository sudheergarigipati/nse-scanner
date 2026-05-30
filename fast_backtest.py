import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

print("Loading data...", flush=True)
conn = sqlite3.connect('nse_data.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

# ── Load ALL price data into memory once ──────────────────────
c.execute('''
    SELECT symbol, date, open, high, low, close, volume
    FROM daily_prices
    WHERE date >= '2023-01-01'
    ORDER BY symbol, date
''')
rows = c.fetchall()

# Organise into dict: symbol -> list of bars
from collections import defaultdict
prices = defaultdict(list)
for r in rows:
    prices[r['symbol']].append(dict(r))

# Symbol -> index map for fast lookup
price_idx = {}  # symbol -> {date: index}
for sym, bars in prices.items():
    price_idx[sym] = {b['date']: i for i, b in enumerate(bars)}

print(f"Loaded {len(prices)} symbols", flush=True)

# Valid symbols (252+ days)
valid_symbols = {s for s, bars in prices.items() if len(bars) >= 252}
print(f"Valid symbols: {len(valid_symbols)}", flush=True)

# All trading days
c.execute('SELECT DISTINCT date FROM daily_prices WHERE date >= ? AND date <= ? ORDER BY date', ('2024-05-01','2026-05-27'))
trading_days = [r['date'] for r in c.fetchall()]
print(f"Trading days: {len(trading_days)}", flush=True)

def get_camarilla(h, l, cl):
    rng = h - l
    return cl - rng*1.1/4, cl - rng*1.1/2  # l3, l4

# ── Pre-compute 20-day avg volume for all symbols ─────────────
print("Pre-computing avg volumes...", flush=True)
avg_vol_cache = {}  # (symbol, date) -> avg_vol
for sym, bars in prices.items():
    for i in range(20, len(bars)):
        avg = sum(b['volume'] for b in bars[i-20:i]) / 20
        avg_vol_cache[(sym, bars[i]['date'])] = avg

print("Running backtest...", flush=True)

RISK_PER_TRADE = 1000
MAX_POS_VAL    = 20000
MIN_RR         = 5.0
MAX_L3_HV_PCT  = 4.0
VOL_MULTIPLIER = 0.5
MIN_HISTORY    = 252

for MAX_CAPITAL in [50000, 100000]:
    MAX_OPEN = MAX_CAPITAL // MAX_POS_VAL

    open_pos  = {}   # symbol -> position
    sl_dates  = {}   # symbol -> date of last SL hit (cooldown)
    capital   = MAX_CAPITAL
    monthly   = defaultdict(lambda: {
        'trades':0,'wins':0,'losses':0,
        'skipped_cap':0,'skipped_cool':0,'pnl':0.0,'signals':0
    })

    for i, today in enumerate(trading_days):
        if i == 0: continue
        month = today[:7]

        # ── Close positions ───────────────────────────────────
        to_close = []
        for sym, pos in open_pos.items():
            idx = price_idx[sym].get(today)
            if idx is None: continue
            bar = prices[sym][idx]
            if bar['low'] <= pos['sl']:
                pnl = (pos['sl'] - pos['entry']) * pos['shares']
                capital += pos['shares'] * pos['entry'] + pnl
                monthly[pos['month']]['losses'] += 1
                monthly[pos['month']]['pnl']    += pnl
                sl_dates[sym] = today  # record SL date for cooldown
                to_close.append(sym)
            elif bar['high'] >= pos['target']:
                pnl = (pos['target'] - pos['entry']) * pos['shares']
                capital += pos['shares'] * pos['entry'] + pnl
                monthly[pos['month']]['wins']  += 1
                monthly[pos['month']]['pnl']   += pnl
                to_close.append(sym)
        for sym in to_close:
            del open_pos[sym]

        # ── Find HV candidates point-in-time ─────────────────
        today_dt  = datetime.strptime(today, '%Y-%m-%d')
        date_min  = (today_dt - timedelta(days=150)).strftime('%Y-%m-%d')
        date_max  = (today_dt - timedelta(days=7)).strftime('%Y-%m-%d')

        sigs = []
        for sym in valid_symbols:
            if sym not in price_idx: continue
            if sym in open_pos: continue

            # Cooldown check — skip 10 days after SL
            if sym in sl_dates:
                sl_dt   = datetime.strptime(sl_dates[sym], '%Y-%m-%d')
                if (today_dt - sl_dt).days < 10:
                    monthly[month]['skipped_cool'] += 1
                    continue

            bars = prices[sym]
            idx  = price_idx[sym].get(today)
            if idx is None or idx < 1: continue
            tb = bars[idx]      # today
            pb = bars[idx-1]    # yesterday
            if tb['date'] != today: continue

            # Find highest volume day in window (point-in-time)
            hv_bar = None
            hv_vol = 0
            for b in bars:
                if b['date'] < date_min: continue
                if b['date'] > date_max: break
                # HV condition: close in top 50% of range
                if b['high'] == b['low']: continue
                if b['close'] < b['low'] + (b['high']-b['low'])*0.5: continue
                if b['volume'] > hv_vol:
                    hv_vol = b['volume']
                    hv_bar = b

            if not hv_bar: continue

            hv_high = hv_bar['high']
            hv_low  = hv_bar['low']

            # Daily camarilla from yesterday
            l3, l4 = get_camarilla(pb['high'], pb['low'], pb['close'])

            avg_vol = avg_vol_cache.get((sym, today), 0)

            # Signal conditions
            if not (tb['low'] <= l3):                              continue
            if not (tb['close'] > tb['open']):                     continue
            if not (tb['close'] >= l3):                            continue
            if not (abs(l3-hv_low)/hv_low*100 <= MAX_L3_HV_PCT):  continue
            if not (tb['volume'] >= avg_vol * VOL_MULTIPLIER):     continue
            if not (tb['close'] >= 50):                            continue

            risk   = l3 - l4
            reward = hv_high - l3
            if risk <= 0 or reward <= 0: continue
            rr = reward / risk
            if rr < MIN_RR: continue

            shares = min(int(RISK_PER_TRADE/risk), int(MAX_POS_VAL/l3))
            if shares < 1: continue

            monthly[month]['signals'] += 1
            sigs.append({
                'symbol': sym, 'entry': round(l3,2), 'sl': round(l4,2),
                'target': round(hv_high,2), 'shares': shares,
                'rr': round(rr,1), 'pos_val': round(shares*l3,0),
                'month': month
            })

        # ── Place trades (best R:R first) ─────────────────────
        for sig in sorted(sigs, key=lambda x: x['rr'], reverse=True):
            sym = sig['symbol']
            if sym in open_pos: continue
            if len(open_pos) >= MAX_OPEN:
                monthly[month]['skipped_cap'] += 1
                continue
            if sig['pos_val'] > capital:
                monthly[month]['skipped_cap'] += 1
                continue
            capital -= sig['shares'] * sig['entry']
            open_pos[sym] = {**sig}
            monthly[month]['trades'] += 1

    # ── Print results ─────────────────────────────────────────
    print(f"\n{'='*85}", flush=True)
    print(f"  REAL BACKTEST (Point-in-Time + 10d Cooldown) | Capital Rs {MAX_CAPITAL:,} | Max pos: {MAX_OPEN}", flush=True)
    print(f"{'='*85}", flush=True)
    print(f"  {'Month':<10} {'Sigs':>6} {'Trades':>7} {'SkpCap':>8} {'SkpCool':>8} {'Wins':>6} {'Loss':>6} {'P&L':>10} {'Capital':>12}", flush=True)
    print(f"  {'-'*80}", flush=True)

    running = MAX_CAPITAL
    tot_sig=tot_tr=tot_win=tot_loss=tot_sc=tot_cool=0
    tot_pnl=0.0

    for month in sorted(monthly.keys()):
        m = monthly[month]
        running   += m['pnl']
        tot_sig   += m['signals']
        tot_tr    += m['trades']
        tot_win   += m['wins']
        tot_loss  += m['losses']
        tot_sc    += m['skipped_cap']
        tot_cool  += m['skipped_cool']
        tot_pnl   += m['pnl']
        print(f"  {month:<10} {m['signals']:>6} {m['trades']:>7} {m['skipped_cap']:>8} {m['skipped_cool']:>8} "
              f"{m['wins']:>6} {m['losses']:>6} {m['pnl']:>+10,.0f} {running:>12,.0f}", flush=True)

    print(f"  {'-'*80}", flush=True)
    wr  = round(tot_win/(tot_win+tot_loss)*100,1) if (tot_win+tot_loss)>0 else 0
    ret = round(tot_pnl/MAX_CAPITAL*100,1)
    print(f"  {'TOTAL':<10} {tot_sig:>6} {tot_tr:>7} {tot_sc:>8} {tot_cool:>8} "
          f"{tot_win:>6} {tot_loss:>6} {tot_pnl:>+10,.0f} {running:>12,.0f}", flush=True)
    print(f"\n  Win rate    : {wr}%", flush=True)
    print(f"  Total return: {ret}%", flush=True)
    print(f"  Avg/month   : Rs {round(tot_pnl/24):,}", flush=True)
    print(f"  Trades/month: {round(tot_tr/24,1)}", flush=True)
    print(f"  Cooldown skips: {tot_cool}", flush=True)

conn.close()
print("\nDone!", flush=True)
