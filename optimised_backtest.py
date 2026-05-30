import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict
import os

print("Starting optimised backtest in batches...", flush=True)

conn = sqlite3.connect('nse_data.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

# ── CONFIG ────────────────────────────────────────────────────
RISK_PER_TRADE = 1000
MAX_POS_VAL    = 20000
MIN_RR         = 5.0
MAX_RR         = 20.0
MAX_L3_HV_PCT  = 4.0
MIN_VOL_RATIO  = 1.5
MAX_HV_VOL     = 10.0
MIN_HV_AGE     = 7
MAX_HV_AGE     = 60
MIN_PRICE      = 50
COOLDOWN_DAYS  = 10
BATCH_SIZE     = 50

# ── Get trading days ──────────────────────────────────────────
c.execute('SELECT DISTINCT date FROM daily_prices WHERE date >= ? AND date <= ? ORDER BY date', ('2024-05-01','2026-05-27'))
trading_days = [r['date'] for r in c.fetchall()]
print(f"Trading days: {len(trading_days)}", flush=True)

# ── Get valid symbols ─────────────────────────────────────────
c.execute('SELECT symbol FROM daily_prices GROUP BY symbol HAVING COUNT(*) >= 252 ORDER BY symbol')
all_symbols = [r['symbol'] for r in c.fetchall()]
print(f"Valid symbols: {len(all_symbols)}", flush=True)

def get_camarilla(h, l, cl):
    rng = h - l
    return cl - rng*1.1/4, cl - rng*1.1/2

# ── Results DB ───────────────────────────────────────────────
rdb = sqlite3.connect('optimised_backtest.db')
rdb.row_factory = sqlite3.Row
rc  = rdb.cursor()

rc.execute('''
    CREATE TABLE IF NOT EXISTS signals (
        symbol      TEXT,
        date        TEXT,
        hv_date     TEXT,
        hv_age      INTEGER,
        entry       REAL,
        sl          REAL,
        target      REAL,
        result      TEXT DEFAULT 'OPEN',
        exit_price  REAL DEFAULT 0,
        exit_date   TEXT DEFAULT '',
        pnl_rs      REAL DEFAULT 0,
        days_held   INTEGER DEFAULT 0,
        rr          REAL,
        vol_ratio   REAL,
        hv_vol_r    REAL,
        shares      INTEGER,
        filter_fail TEXT DEFAULT '',
        UNIQUE(symbol, date)
    )
''')
rc.execute('''
    CREATE TABLE IF NOT EXISTS filter_log (
        date        TEXT,
        symbol      TEXT,
        reason      TEXT
    )
''')
rc.execute('''
    CREATE TABLE IF NOT EXISTS progress (
        id          INTEGER PRIMARY KEY,
        batch_start INTEGER DEFAULT 0
    )
''')
rdb.commit()

# Resume support
rc.execute('SELECT batch_start FROM progress WHERE id=1')
r = rc.fetchone()
resume_batch = r['batch_start'] if r else 0
print(f"Resuming from batch {resume_batch//BATCH_SIZE + 1}", flush=True)

total_batches = (len(all_symbols) + BATCH_SIZE - 1) // BATCH_SIZE

for batch_start in range(resume_batch, len(all_symbols), BATCH_SIZE):
    batch     = all_symbols[batch_start:batch_start+BATCH_SIZE]
    batch_num = batch_start // BATCH_SIZE + 1
    print(f"Batch {batch_num}/{total_batches}: {batch[0]} to {batch[-1]}...", flush=True)

    # Load only this batch
    placeholders = ','.join('?' * len(batch))
    c.execute(f'''
        SELECT symbol, date, open, high, low, close, volume
        FROM daily_prices
        WHERE symbol IN ({placeholders})
        AND date >= '2023-01-01'
        ORDER BY symbol, date
    ''', batch)
    rows = c.fetchall()

    prices    = defaultdict(list)
    for r in rows:
        prices[r['symbol']].append(dict(r))

    price_idx = {}
    for sym, bars in prices.items():
        price_idx[sym] = {b['date']: i for i, b in enumerate(bars)}

    # Avg volume
    avg_vol_cache = {}
    for sym, bars in prices.items():
        for i in range(20, len(bars)):
            avg = sum(b['volume'] for b in bars[i-20:i]) / 20
            avg_vol_cache[(sym, bars[i]['date'])] = avg

    batch_signals  = []
    batch_filters  = []

    for i, today in enumerate(trading_days):
        if i == 0: continue
        today_dt = datetime.strptime(today, '%Y-%m-%d')
        date_min = (today_dt - timedelta(days=MAX_HV_AGE)).strftime('%Y-%m-%d')
        date_max = (today_dt - timedelta(days=MIN_HV_AGE)).strftime('%Y-%m-%d')

        for sym in batch:
            if sym not in price_idx: continue
            idx = price_idx[sym].get(today)
            if idx is None or idx < 1: continue
            bars = prices[sym]
            tb   = bars[idx]
            pb   = bars[idx-1]
            if tb['date'] != today: continue

            # Find HV point-in-time
            hv_bar = None; hv_vol = 0
            for b in bars:
                if b['date'] < date_min: continue
                if b['date'] > date_max: break
                if b['high'] == b['low']: continue
                if b['close'] < b['low'] + (b['high']-b['low'])*0.5: continue
                if b['volume'] > hv_vol:
                    hv_vol = b['volume']
                    hv_bar = b
            if not hv_bar: continue

            hv_high  = hv_bar['high']
            hv_low   = hv_bar['low']
            hv_date  = hv_bar['date']
            hv_age   = (today_dt - datetime.strptime(hv_date, '%Y-%m-%d')).days
            avg_vol  = avg_vol_cache.get((sym, today), 0)
            hv_vol_r = round(hv_vol/avg_vol, 1) if avg_vol > 0 else 0
            l3, l4   = get_camarilla(pb['high'], pb['low'], pb['close'])
            vol_r    = round(tb['volume']/avg_vol, 2) if avg_vol > 0 else 0

            # Filters
            if not (tb['low'] <= l3): continue

            fail = ''
            if not (tb['close'] > tb['open']):                    fail = 'Red candle'
            elif not (tb['close'] >= l3):                         fail = f'Close<L3'
            elif not (abs(l3-hv_low)/hv_low*100 <= MAX_L3_HV_PCT): fail = f'L3 far from HVLow {abs(l3-hv_low)/hv_low*100:.1f}%'
            elif not (vol_r >= MIN_VOL_RATIO):                    fail = f'Low vol {vol_r:.1f}x<{MIN_VOL_RATIO}x'
            elif not (hv_vol_r <= MAX_HV_VOL):                    fail = f'HV too strong {hv_vol_r:.1f}x'
            elif not (tb['close'] >= MIN_PRICE):                  fail = f'Price low'
            else:
                risk   = l3 - l4
                reward = hv_high - l3
                if risk <= 0 or reward <= 0: continue
                rr = reward / risk
                if not (MIN_RR <= rr <= MAX_RR):                  fail = f'RR {rr:.1f}x out of range'

            if fail:
                batch_filters.append((today, sym, fail))
                continue

            shares = min(int(RISK_PER_TRADE/risk), int(MAX_POS_VAL/l3))
            if shares < 1: continue

            # Simulate outcome — next 30 days
            result     = 'OPEN'
            exit_price = 0
            exit_date  = ''
            days_held  = 0

            for j in range(idx+1, min(idx+31, len(bars))):
                fut = bars[j]
                days_held += 1
                if fut['low'] <= l4:
                    result     = 'SL'
                    exit_price = l4
                    exit_date  = fut['date']
                    break
                if fut['high'] >= hv_high:
                    result     = 'WIN'
                    exit_price = hv_high
                    exit_date  = fut['date']
                    break

            if result == 'OPEN':
                last       = min(idx+30, len(bars)-1)
                exit_price = bars[last]['close']
                exit_date  = bars[last]['date']

            pnl_rs = round((exit_price - l3) * shares, 0)

            batch_signals.append((
                sym, today, hv_date, hv_age,
                round(l3,2), round(l4,2), round(hv_high,2),
                result, round(exit_price,2), exit_date,
                pnl_rs, days_held,
                round(rr,1), vol_r, hv_vol_r, shares, ''
            ))

    # Save to DB
    rc.executemany('''
        INSERT OR IGNORE INTO signals
        (symbol,date,hv_date,hv_age,entry,sl,target,
         result,exit_price,exit_date,pnl_rs,days_held,
         rr,vol_ratio,hv_vol_r,shares,filter_fail)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ''', batch_signals)

    rc.executemany('INSERT INTO filter_log (date,symbol,reason) VALUES (?,?,?)', batch_filters)

    # Save progress
    rc.execute('INSERT OR REPLACE INTO progress (id,batch_start) VALUES (1,?)', (batch_start+BATCH_SIZE,))
    rdb.commit()

    print(f"  Signals: {len(batch_signals)} | Filtered: {len(batch_filters)}", flush=True)

# ── Now simulate capital allocation day by day ────────────────
print("\nSimulating capital allocation...", flush=True)

for MAX_CAPITAL in [50000, 100000]:
    MAX_OPEN = MAX_CAPITAL // MAX_POS_VAL

    log = open(f'backtest_log_{MAX_CAPITAL}.txt', 'w')
    def L(msg): print(msg, file=log, flush=True)

    L(f"OPTIMISED BACKTEST | Capital Rs {MAX_CAPITAL:,} | Max positions: {MAX_OPEN}")
    L(f"Filters: HV {MIN_HV_AGE}-{MAX_HV_AGE}d | Vol>={MIN_VOL_RATIO}x | HVVol<={MAX_HV_VOL}x | R:R {MIN_RR}-{MAX_RR}x | Cooldown {COOLDOWN_DAYS}d")
    L("="*110)

    open_pos  = {}
    sl_dates  = {}
    capital   = MAX_CAPITAL
    monthly   = defaultdict(lambda: {
        'trades':0,'wins':0,'losses':0,
        'skipped_cap':0,'skipped_cool':0,'pnl':0.0,'signals':0
    })

    for i, today in enumerate(trading_days):
        if i == 0: continue
        month    = today[:7]
        today_dt = datetime.strptime(today, '%Y-%m-%d')

        # Close positions
        to_close = []
        for sym, pos in open_pos.items():
            rc.execute('SELECT result,exit_price,exit_date,pnl_rs FROM signals WHERE symbol=? AND date=?', (sym, pos['entry_date']))
            sig = rc.fetchone()
            if not sig: continue

            # Check if today is the exit date
            rc.execute('SELECT result,exit_price FROM signals WHERE symbol=? AND date<=? AND date>=? ORDER BY date DESC LIMIT 1',
                      (sym, today, pos['entry_date']))
            # Simulate day by day using raw price
            c.execute('SELECT high,low FROM daily_prices WHERE symbol=? AND date=?', (sym, today))
            bar = c.fetchone()
            if not bar: continue

            if bar['low'] <= pos['sl']:
                pnl = (pos['sl'] - pos['entry']) * pos['shares']
                capital += pos['shares'] * pos['entry'] + pnl
                monthly[pos['month']]['losses'] += 1
                monthly[pos['month']]['pnl']    += pnl
                sl_dates[sym] = today
                to_close.append(sym)
                L(f"CLOSE_SL  | {today} | {sym:<15} | Entry {pos['entry']:.2f} -> SL {pos['sl']:.2f} | P&L Rs {pnl:+.0f} | HVAge {pos['hv_age']}d | R:R {pos['rr']}x | Capital Rs {capital:,.0f}")
            elif bar['high'] >= pos['target']:
                pnl = (pos['target'] - pos['entry']) * pos['shares']
                capital += pos['shares'] * pos['entry'] + pnl
                monthly[pos['month']]['wins']  += 1
                monthly[pos['month']]['pnl']   += pnl
                to_close.append(sym)
                L(f"CLOSE_WIN | {today} | {sym:<15} | Entry {pos['entry']:.2f} -> TGT {pos['target']:.2f} | P&L Rs {pnl:+.0f} | HVAge {pos['hv_age']}d | R:R {pos['rr']}x | Capital Rs {capital:,.0f}")
        for sym in to_close:
            del open_pos[sym]

        # Day header
        open_str = ', '.join(f"{s}({open_pos[s]['entry']:.0f}->{open_pos[s]['target']:.0f})" for s in open_pos) or 'NONE'
        L(f"\nDAY       | {today} | Open({len(open_pos)}/{MAX_OPEN}): {open_str} | Capital Rs {capital:,.0f}")

        # Get filter log for today
        rc.execute('SELECT symbol, reason FROM filter_log WHERE date=? ORDER BY symbol', (today,))
        for row in rc.fetchall():
            L(f"FILTER    | {today} | {row['symbol']:<15} | {row['reason']}")

        # Get signals for today
        rc.execute('''
            SELECT * FROM signals WHERE date=?
            ORDER BY rr DESC
        ''', (today,))
        sigs = rc.fetchall()

        for sig in sigs:
            sym = sig['symbol']
            monthly[month]['signals'] += 1
            L(f"SIGNAL    | {today} | {sym:<15} | Entry {sig['entry']:.2f} | SL {sig['sl']:.2f} | Tgt {sig['target']:.2f} | R:R {sig['rr']}x | Vol {sig['vol_ratio']:.1f}x | HV {sig['hv_vol_r']:.1f}x | HVAge {sig['hv_age']}d | {sig['shares']}sh")

        for sig in sigs:
            sym = sig['symbol']
            if sym in open_pos:
                L(f"SKIP_DUP  | {today} | {sym:<15} | Already open @ {open_pos[sym]['entry']:.2f}")
                continue

            # Cooldown
            if sym in sl_dates:
                sl_dt = datetime.strptime(sl_dates[sym], '%Y-%m-%d')
                cd_left = COOLDOWN_DAYS - (today_dt - sl_dt).days
                if cd_left > 0:
                    L(f"COOLDOWN  | {today} | {sym:<15} | {cd_left}d left after SL {sl_dates[sym]}")
                    monthly[month]['skipped_cool'] += 1
                    continue

            if len(open_pos) >= MAX_OPEN:
                L(f"SKIP_CAP  | {today} | {sym:<15} | {MAX_OPEN} slots full: {', '.join(open_pos.keys())} | R:R {sig['rr']}x")
                monthly[month]['skipped_cap'] += 1
                continue

            pos_val = sig['shares'] * sig['entry']
            if pos_val > capital:
                L(f"SKIP_CASH | {today} | {sym:<15} | Need Rs {pos_val:,.0f} have Rs {capital:,.0f}")
                monthly[month]['skipped_cap'] += 1
                continue

            capital -= sig['shares'] * sig['entry']
            open_pos[sym] = {
                'entry': sig['entry'], 'sl': sig['sl'],
                'target': sig['target'], 'shares': sig['shares'],
                'rr': sig['rr'], 'hv_age': sig['hv_age'],
                'month': month, 'entry_date': today
            }
            monthly[month]['trades'] += 1
            L(f"BUY       | {today} | {sym:<15} | Entry {sig['entry']:.2f} | SL {sig['sl']:.2f} | Tgt {sig['target']:.2f} | R:R {sig['rr']}x | Vol {sig['vol_ratio']:.1f}x | HV {sig['hv_vol_r']:.1f}x | HVAge {sig['hv_age']}d | {sig['shares']}sh | Capital left Rs {capital:,.0f}")

    # Monthly summary
    L(f"\n{'='*110}")
    L(f"MONTHLY SUMMARY | Capital Rs {MAX_CAPITAL:,}")
    L(f"{'='*110}")
    L(f"{'Month':<10} {'Signals':>8} {'Trades':>8} {'SkpCap':>8} {'SkpCool':>8} {'Wins':>6} {'Loss':>6} {'P&L':>10} {'Capital':>12}")
    L(f"{'-'*85}")

    running = MAX_CAPITAL
    tot_sig=tot_tr=tot_win=tot_loss=tot_sc=tot_cool=0
    tot_pnl=0.0

    for month in sorted(monthly.keys()):
        m = monthly[month]
        running  += m['pnl']
        tot_sig  += m['signals']
        tot_tr   += m['trades']
        tot_win  += m['wins']
        tot_loss += m['losses']
        tot_sc   += m['skipped_cap']
        tot_cool += m['skipped_cool']
        tot_pnl  += m['pnl']
        L(f"{month:<10} {m['signals']:>8} {m['trades']:>8} {m['skipped_cap']:>8} {m['skipped_cool']:>8} {m['wins']:>6} {m['losses']:>6} {m['pnl']:>+10,.0f} {running:>12,.0f}")

    L(f"{'-'*85}")
    wr  = round(tot_win/(tot_win+tot_loss)*100,1) if (tot_win+tot_loss)>0 else 0
    ret = round(tot_pnl/MAX_CAPITAL*100,1)
    L(f"{'TOTAL':<10} {tot_sig:>8} {tot_tr:>8} {tot_sc:>8} {tot_cool:>8} {tot_win:>6} {tot_loss:>6} {tot_pnl:>+10,.0f} {running:>12,.0f}")
    L(f"\nWin rate     : {wr}%")
    L(f"Total return : {ret}%")
    L(f"Avg/month    : Rs {round(tot_pnl/24):,}")
    L(f"Trades/month : {round(tot_tr/24,1)}")

    log.close()
    print(f"Rs {MAX_CAPITAL:,} done! WR={wr}% Return={ret}% Avg/mo=Rs {round(tot_pnl/24):,}", flush=True)

conn.close()
rdb.close()
print("All done!", flush=True)
