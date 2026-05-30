import sqlite3, yfinance as yf, json, time

conn = sqlite3.connect('nse_data.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()
c.execute("""
    SELECT symbol, hv_date, hv_low, hv_high, hv_close, hv_volume,
           ROUND((hv_close-hv_low)/(hv_high-hv_low)*100,1) as candle_pos,
           ROUND((hv_high-hv_low)/hv_low*100,1) as hv_range_pct
    FROM hv_summary
    WHERE hv_date >= '2024-01-01'
    AND hv_date <= '2026-03-01'
    AND (hv_close-hv_low)/(hv_high-hv_low) >= 0.5
    AND hv_low > 0 AND hv_volume > 0
    AND hv_high > hv_low * 1.05
    ORDER BY hv_date
""")
stocks = c.fetchall()
conn.close()
print(f'Testing {len(stocks)} setups...', flush=True)

results  = []
skipped  = 0

for idx_s, s in enumerate(stocks):
    sym          = s['symbol']
    hv_date      = s['hv_date']
    hv_low       = s['hv_low']
    hv_high      = s['hv_high']
    candle_pos   = s['candle_pos']
    hv_range_pct = s['hv_range_pct']

    if idx_s % 50 == 0:
        print(f'Progress: {idx_s}/{len(stocks)} | Results: {len(results)} | Skipped: {skipped}', flush=True)

    try:
        t = yf.Ticker(f'{sym}.NS')
        h = t.history(start=hv_date, end='2026-05-20')
        if len(h) < 10:
            continue

        h['avg_vol']   = h['Volume'].rolling(20).mean().fillna(h['Volume'].mean())
        h['vol_ratio'] = h['Volume'] / h['avg_vol']
        h_after        = h.iloc[1:]
        h_list         = list(h_after.iterrows())

        for i, (date_idx, row) in enumerate(h_list):
            close     = float(row['Close'])
            low       = float(row['Low'])
            vol_ratio = float(row['vol_ratio'])
            green     = row['Close'] > row['Open']
            pct_above = (close - hv_low) / hv_low * 100

            # Signal conditions
            if not (low <= hv_low * 1.03 and
                    vol_ratio >= 2.0 and
                    green and
                    pct_above <= 3.0):
                continue

            # Signal found on day i
            # Find fill day: first day after signal where low <= hv_low
            entry_price = hv_low
            sl_price    = round(hv_low * 0.97, 2)
            target      = float(hv_high)
            sl_dist     = entry_price - sl_price
            rr          = round((target - entry_price) / sl_dist, 1) if sl_dist > 0 else 0

            fill_day_idx = None
            for k in range(i+1, min(i+6, len(h_list))):
                kdate, krow = h_list[k]
                if float(krow['Low']) <= entry_price:
                    fill_day_idx = k
                    break

            if fill_day_idx is None:
                skipped += 1
                break

            # Now track from day AFTER fill
            outcome    = 'OPEN'
            exit_price = None
            exit_days  = 0

            for m in range(fill_day_idx + 1, min(fill_day_idx + 21, len(h_list))):
                mdate, mrow = h_list[m]
                mlow  = float(mrow['Low'])
                mhigh = float(mrow['High'])
                days_held = m - fill_day_idx

                if mlow <= sl_price:
                    outcome    = 'SL_HIT'
                    exit_price = sl_price
                    exit_days  = days_held
                    break
                if mhigh >= target:
                    outcome    = 'TARGET_HIT'
                    exit_price = target
                    exit_days  = days_held
                    break
                if days_held >= 15:
                    outcome    = 'TIME_EXIT'
                    exit_price = float(mrow['Close'])
                    exit_days  = days_held
                    break

            if outcome != 'OPEN':
                pnl = round((exit_price - entry_price) / entry_price * 100, 2)
                results.append({
                    'symbol'      : sym,
                    'hv_date'     : hv_date,
                    'candle_pos'  : candle_pos,
                    'hv_range_pct': hv_range_pct,
                    'signal_date' : str(date_idx.date()),
                    'fill_date'   : str(h_list[fill_day_idx][0].date()),
                    'vol_ratio'   : round(vol_ratio, 1),
                    'pct_above'   : round(pct_above, 2),
                    'entry'       : entry_price,
                    'sl'          : sl_price,
                    'target'      : target,
                    'rr'          : rr,
                    'outcome'     : outcome,
                    'exit_price'  : round(exit_price, 2),
                    'exit_days'   : exit_days,
                    'pnl_pct'     : pnl,
                })
            break

        time.sleep(0.15)
    except Exception as e:
        pass

with open('backtest_limit.json', 'w') as f:
    json.dump(results, f, indent=2)

total = len(results)
print(f'', flush=True)
print(f'=== LIMIT ORDER BACKTEST RESULTS ===', flush=True)
print(f'Total filled : {total}', flush=True)
print(f'Never filled : {skipped}', flush=True)

if total > 0:
    winners = [r for r in results if r['outcome'] == 'TARGET_HIT']
    losers  = [r for r in results if r['outcome'] == 'SL_HIT']
    texits  = [r for r in results if r['outcome'] == 'TIME_EXIT']
    wr      = len(winners)/total*100
    avg_w   = sum(r['pnl_pct'] for r in winners)/len(winners) if winners else 0
    avg_l   = sum(r['pnl_pct'] for r in losers)/len(losers) if losers else 0
    avg_t   = sum(r['pnl_pct'] for r in texits)/len(texits) if texits else 0
    avg_d   = sum(r['exit_days'] for r in results)/total
    ev      = (wr/100*avg_w)+(len(losers)/total*avg_l)+(len(texits)/total*avg_t)

    print(f'Winners  : {len(winners)} ({wr:.1f}%)', flush=True)
    print(f'Losers   : {len(losers)} ({len(losers)/total*100:.1f}%)', flush=True)
    print(f'TimeExit : {len(texits)} ({len(texits)/total*100:.1f}%)', flush=True)
    print(f'Avg win  : +{avg_w:.2f}%', flush=True)
    print(f'Avg loss : {avg_l:.2f}%', flush=True)
    print(f'Avg time : {avg_t:.2f}%', flush=True)
    print(f'Avg days : {avg_d:.1f}', flush=True)
    print(f'Exp val  : {ev:.2f}% per trade', flush=True)
    print(f'', flush=True)

    print('Volume filter:', flush=True)
    for vmin in [1.5, 2.0, 2.5, 3.0]:
        f = [r for r in results if r['vol_ratio'] >= vmin]
        if len(f) >= 5:
            w   = [r for r in f if r['outcome']=='TARGET_HIT']
            l   = [r for r in f if r['outcome']=='SL_HIT']
            wr2 = len(w)/len(f)*100
            aw  = sum(r['pnl_pct'] for r in w)/len(w) if w else 0
            al  = sum(r['pnl_pct'] for r in l)/len(l) if l else 0
            ev2 = (wr2/100*aw)+((len(l)/len(f))*al)
            print(f'  Vol>={vmin}x: {len(f):>4} | Win:{wr2:.0f}% | EV:{ev2:.2f}%', flush=True)

    print('Candle position:', flush=True)
    for cmin in [50, 60, 70, 80]:
        f = [r for r in results if r['candle_pos'] >= cmin]
        if len(f) >= 5:
            w   = [r for r in f if r['outcome']=='TARGET_HIT']
            l   = [r for r in f if r['outcome']=='SL_HIT']
            wr2 = len(w)/len(f)*100
            aw  = sum(r['pnl_pct'] for r in w)/len(w) if w else 0
            al  = sum(r['pnl_pct'] for r in l)/len(l) if l else 0
            ev2 = (wr2/100*aw)+((len(l)/len(f))*al)
            print(f'  Candle>={cmin}%: {len(f):>4} | Win:{wr2:.0f}% | EV:{ev2:.2f}%', flush=True)

    print('HV Range:', flush=True)
    for rng in [10, 15, 20]:
        f = [r for r in results if r['hv_range_pct'] >= rng]
        if len(f) >= 5:
            w   = [r for r in f if r['outcome']=='TARGET_HIT']
            l   = [r for r in f if r['outcome']=='SL_HIT']
            wr2 = len(w)/len(f)*100
            aw  = sum(r['pnl_pct'] for r in w)/len(w) if w else 0
            al  = sum(r['pnl_pct'] for r in l)/len(l) if l else 0
            ev2 = (wr2/100*aw)+((len(l)/len(f))*al)
            print(f'  Range>={rng}%: {len(f):>4} | Win:{wr2:.0f}% | EV:{ev2:.2f}%', flush=True)

    print('', flush=True)
    print('Sample trades:', flush=True)
    print(f'{"Symbol":<12} {"Signal":<12} {"Fill":<12} {"Vol":>5} {"Entry":>8} {"Exit":>8} {"Days":>5} {"PnL%":>7} Outcome', flush=True)
    print('-'*85, flush=True)
    for r in sorted(results, key=lambda x: x['signal_date'])[:30]:
        print(f'{r["symbol"]:<12} {r["signal_date"]:<12} {r["fill_date"]:<12} '
              f'{r["vol_ratio"]:>4.1f}x {r["entry"]:>8.2f} {r["exit_price"]:>8.2f} '
              f'{r["exit_days"]:>5} {r["pnl_pct"]:>6.2f}% {r["outcome"]}', flush=True)

print('DONE!', flush=True)
