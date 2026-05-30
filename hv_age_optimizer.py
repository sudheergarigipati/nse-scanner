import sqlite3
from datetime import datetime, timedelta

conn = sqlite3.connect('nse_data.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

print("Starting HV age optimization...")

for MIN_HV_AGE in [1, 3, 5, 7, 10, 14, 21]:
    MAX_HV_AGE     = 150
    RISK_PER_TRADE = 1000
    MAX_POS_VAL    = 20000
    MAX_CAPITAL    = 50000
    MAX_OPEN       = 2
    MIN_RR         = 5.0
    MAX_L3_HV_PCT  = 4.0
    VOL_MULTIPLIER = 0.5

    c.execute('SELECT DISTINCT date FROM daily_prices WHERE date >= ? AND date <= ? ORDER BY date', ('2024-05-01','2026-05-27'))
    trading_days = [r['date'] for r in c.fetchall()]
    c.execute('SELECT symbol FROM daily_prices GROUP BY symbol HAVING COUNT(*) >= 252')
    valid_symbols = {r['symbol'] for r in c.fetchall()}

    def get_camarilla(h, l, cl):
        rng = h - l
        return {'l3': cl - rng*1.1/4, 'l4': cl - rng*1.1/2}

    open_pos = {}
    capital  = MAX_CAPITAL
    tot_tr = tot_win = tot_loss = 0
    tot_pnl = 0.0

    for i, today in enumerate(trading_days):
        if i == 0: continue

        # Close positions
        to_close = []
        for sym, pos in open_pos.items():
            c.execute('SELECT high,low FROM daily_prices WHERE symbol=? AND date=?', (sym, today))
            r = c.fetchone()
            if not r: continue
            if r['low'] <= pos['sl']:
                pnl = (pos['sl'] - pos['entry']) * pos['shares']
                capital += pos['shares'] * pos['entry'] + pnl
                tot_loss += 1
                tot_pnl  += pnl
                to_close.append(sym)
            elif r['high'] >= pos['target']:
                pnl = (pos['target'] - pos['entry']) * pos['shares']
                capital += pos['shares'] * pos['entry'] + pnl
                tot_win += 1
                tot_pnl += pnl
                to_close.append(sym)
        for sym in to_close:
            del open_pos[sym]

        date_max = (datetime.strptime(today, '%Y-%m-%d') - timedelta(days=MIN_HV_AGE)).strftime('%Y-%m-%d')
        date_min = (datetime.strptime(today, '%Y-%m-%d') - timedelta(days=MAX_HV_AGE)).strftime('%Y-%m-%d')

        c.execute('''
            SELECT symbol, date as hv_date, high as hv_high, low as hv_low
            FROM daily_prices d1
            WHERE date >= ? AND date <= ?
            AND close >= (low + (high-low)*0.5)
            AND volume = (
                SELECT MAX(volume) FROM daily_prices d2
                WHERE d2.symbol=d1.symbol AND d2.date>=? AND d2.date<=?
            )
            AND low > 0 AND volume > 0
        ''', (date_min, date_max, date_min, date_max))
        candidates = [r for r in c.fetchall() if r['symbol'] in valid_symbols]

        sigs = []
        for row in candidates:
            sym     = row['symbol']
            hv_high = row['hv_high']
            hv_low  = row['hv_low']
            if sym in open_pos: continue

            c.execute('SELECT date,open,high,low,close,volume FROM daily_prices WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 2', (sym, today))
            bars = c.fetchall()
            if len(bars) < 2: continue
            tb = bars[0]; pb = bars[1]
            if tb['date'] != today: continue

            cam = get_camarilla(pb['high'], pb['low'], pb['close'])
            l3 = cam['l3']; l4 = cam['l4']

            c.execute('SELECT AVG(volume) as av FROM (SELECT volume FROM daily_prices WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 20)', (sym, today))
            avg_vol = c.fetchone()['av'] or 0

            if not (tb['low'] <= l3): continue
            if not (tb['close'] > tb['open']): continue
            if not (tb['close'] >= l3): continue
            if not (abs(l3 - hv_low) / hv_low * 100 <= MAX_L3_HV_PCT): continue
            if not (tb['volume'] >= avg_vol * VOL_MULTIPLIER): continue
            if not (tb['close'] >= 50): continue

            risk   = l3 - l4
            reward = hv_high - l3
            if risk <= 0 or reward <= 0: continue
            rr = reward / risk
            if rr < MIN_RR: continue

            shares = min(int(RISK_PER_TRADE/risk), int(MAX_POS_VAL/l3))
            if shares < 1: continue

            sigs.append({
                'symbol': sym, 'entry': round(l3,2), 'sl': round(l4,2),
                'target': round(hv_high,2), 'shares': shares,
                'rr': round(rr,1), 'pos_val': round(shares*l3,0)
            })

        for sig in sorted(sigs, key=lambda x: x['rr'], reverse=True):
            sym = sig['symbol']
            if sym in open_pos: continue
            if len(open_pos) >= MAX_OPEN: continue
            if sig['pos_val'] > capital: continue
            capital -= sig['shares'] * sig['entry']
            open_pos[sym] = {**sig}
            tot_tr += 1

    wr  = round(tot_win/(tot_win+tot_loss)*100, 1) if (tot_win+tot_loss) > 0 else 0
    ret = round(tot_pnl/MAX_CAPITAL*100, 1)
    print(f"MIN_HV_AGE={MIN_HV_AGE:>3}d | Trades={tot_tr:>4} | Wins={tot_win:>3} | Loss={tot_loss:>3} | WR={wr:>5}% | Return={ret:>6}% | Profit=Rs {tot_pnl:>+8,.0f}")

conn.close()
print("Done!")
