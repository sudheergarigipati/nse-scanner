#!/usr/bin/env python3
"""
Backtest: HV Day Entry Strategy
Entry  : HV day close (bullish candle)
SL     : HV Low
Target : HV High
Period : 2021-2026
"""
import sqlite3
from datetime import date, timedelta, datetime
import os

BASE_DIR = os.path.expanduser('~/nse-scanner')
DB_PATH  = os.path.join(BASE_DIR, 'nse_data.db')

def backtest(min_upside=8.0, min_candle_pos=0.5,
             max_hold=60, position_size=20000,
             total_capital=50000, min_volume_ratio=2.0):

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Get all HV days with bullish candle
    c.execute('''
        SELECT h.symbol, h.hv_date, h.hv_low, h.hv_high,
               h.hv_close, h.hv_volume,
               ROUND((h.hv_close-h.hv_low)/(h.hv_high-h.hv_low)*100,1) as candle_pos,
               ROUND((h.hv_high-h.hv_low)/h.hv_low*100,1) as upside_pct
        FROM hv_summary h
        WHERE h.hv_low > 0
        AND h.hv_high > h.hv_low * 1.05
        AND (h.hv_close-h.hv_low)/(h.hv_high-h.hv_low) >= ?
        AND h.hv_date >= "2021-01-01"
        AND h.hv_date <= "2026-05-01"
        ORDER BY h.hv_date ASC
    ''', (min_candle_pos,))
    setups = c.fetchall()

    # Raw signals
    raw_signals = []

    for s in setups:
        sym        = s['symbol']
        hv_date    = s['hv_date']
        hv_low     = float(s['hv_low'])
        hv_high    = float(s['hv_high'])
        hv_close   = float(s['hv_close'])
        hv_vol     = float(s['hv_volume'])
        candle_pos = float(s['candle_pos'])
        upside     = float(s['upside_pct'])

        if upside < min_upside: continue

        # Volume check — must be >= 2x average
        c.execute('''SELECT AVG(volume) FROM (
                     SELECT volume FROM daily_prices
                     WHERE symbol=? AND date < ?
                     ORDER BY date DESC LIMIT 20)''',
                  (sym, hv_date))
        avg_vol = c.fetchone()[0] or 1
        vol_ratio = hv_vol / avg_vol
        if vol_ratio < min_volume_ratio: continue

        # Get prices from HV day onwards
        c.execute('''SELECT date,open,high,low,close,volume
                     FROM daily_prices
                     WHERE symbol=? AND date >= ?
                     ORDER BY date ASC''', (sym, hv_date))
        prices = c.fetchall()
        if not prices: continue

        raw_signals.append({
            'signal_date': hv_date,
            'symbol'     : sym,
            'entry'      : hv_close,  # Enter at HV day close
            'sl'         : hv_low,    # SL at HV Low
            'target'     : hv_high,   # Target = HV High
            'candle_pos' : candle_pos,
            'upside'     : upside,
            'vol_ratio'  : round(vol_ratio, 1),
            'prices'     : prices,
        })

    conn.close()

    # Sort by date
    raw_signals.sort(key=lambda x: x['signal_date'])

    # Simulate with capital management
    capital     = total_capital
    open_trades = []
    all_trades  = []
    skipped     = 0

    for sig in raw_signals:
        sig_date = sig['signal_date']
        sym      = sig['symbol']

        # Close finished trades
        still_open = []
        for ot in open_trades:
            if ot['exit_date'] <= sig_date:
                capital += ot['capital_used'] + ot['total_pnl']
                all_trades.append(ot)
            else:
                still_open.append(ot)
        open_trades = still_open

        # Skip if already in this stock
        if any(ot['symbol'] == sym for ot in open_trades):
            skipped += 1
            continue

        # Check capital
        if capital < position_size:
            skipped += 1
            continue

        prices  = sig['prices']
        entry   = sig['entry']
        sl      = sig['sl']
        target  = sig['target']

        if sl >= entry: continue
        if target <= entry: continue

        risk   = entry - sl
        reward = target - entry
        rr     = round(reward / risk, 1)
        if rr < 1.0: continue

        shares = int(position_size / entry)
        if shares < 1: continue

        # Simulate from day after HV day
        result    = 'TIMEOUT'
        exit_price= float(prices[min(max_hold, len(prices)-1)]['close'])
        exit_date = prices[min(max_hold, len(prices)-1)]['date']
        days_held = 0

        for future in prices[1:max_hold+1]:  # start from day after HV
            days_held += 1
            fh = float(future['high'])
            fl = float(future['low'])
            fc = float(future['close'])

            if fl <= sl:
                result     = 'SL'
                exit_price = sl
                exit_date  = future['date']
                break

            if fh >= target:
                result     = 'TARGET'
                exit_price = target
                exit_date  = future['date']
                break

        pnl_pct = round((exit_price - entry) / entry * 100, 2)
        pnl_rs  = round(pnl_pct / 100 * position_size, 0)

        capital_used = shares * entry
        capital -= capital_used

        open_trades.append({
            'symbol'      : sym,
            'signal_date' : sig_date,
            'exit_date'   : exit_date,
            'entry'       : round(entry, 2),
            'sl'          : round(sl, 2),
            'target'      : round(target, 2),
            'shares'      : shares,
            'capital_used': round(capital_used, 0),
            'result'      : result,
            'pnl_pct'     : pnl_pct,
            'total_pnl'   : pnl_rs,
            'days_held'   : days_held,
            'upside'      : sig['upside'],
            'vol_ratio'   : sig['vol_ratio'],
            'candle_pos'  : sig['candle_pos'],
            'rr'          : rr,
        })

    # Close remaining
    for ot in open_trades:
        capital += ot['capital_used'] + ot['total_pnl']
        all_trades.append(ot)

    return all_trades, skipped, capital

def analyze(trades, skipped, final_capital,
            label, total_capital=50000, position_size=20000):
    if not trades:
        print(f"\n{label}: No trades")
        return None

    total_months = 64
    winners  = [t for t in trades if t['result'] == 'TARGET']
    losers   = [t for t in trades if t['result'] == 'SL']
    timeouts = [t for t in trades if t['result'] == 'TIMEOUT']
    total_pnl= sum(t['total_pnl'] for t in trades)
    win_rate = round(len(winners)/len(trades)*100, 1)
    ev       = round(total_pnl/len(trades), 0)
    per_month= round(len(trades)/total_months, 1)
    avg_days = round(sum(t['days_held'] for t in trades)/len(trades), 1)

    avg_win  = round(sum(t['total_pnl'] for t in winners)/len(winners), 0) if winners else 0
    avg_loss = round(sum(t['total_pnl'] for t in losers)/len(losers), 0) if losers else 0
    avg_win_pct = round(sum(t['pnl_pct'] for t in winners)/len(winners), 1) if winners else 0
    avg_loss_pct= round(sum(t['pnl_pct'] for t in losers)/len(losers), 1) if losers else 0

    # Monthly
    months = {}
    for t in trades:
        m = t['signal_date'][:7]
        if m not in months: months[m] = []
        months[m].append(t)

    monthly_pnls = [sum(t['total_pnl'] for t in v) for v in months.values()]
    pos_months = [m for m in monthly_pnls if m > 0]
    neg_months = [m for m in monthly_pnls if m < 0]
    all_m = set()
    d = date(2021,1,1)
    while d <= date(2026,5,1):
        all_m.add(d.strftime('%Y-%m'))
        if d.month == 12: d = date(d.year+1,1,1)
        else: d = date(d.year, d.month+1, 1)
    zero = len(all_m - set(months.keys()))

    # Drawdown
    cum=0; peak=0; max_dd=0
    for t in trades:
        cum += t['total_pnl']
        peak = max(peak, cum)
        max_dd = max(max_dd, peak-cum)

    # Consecutive losses
    max_cl=0; cl=0
    for t in trades:
        if t['total_pnl']<=0: cl+=1; max_cl=max(max_cl,cl)
        else: cl=0

    print(f"\n{'='*65}")
    print(f"  {label}")
    print(f"  Capital: Rs {total_capital:,} | Position: Rs {position_size:,}")
    print(f"{'='*65}")
    print(f"Trades/month  : {per_month} | Skipped: {skipped}")
    print(f"Win rate      : {win_rate}%")
    print(f"Targets hit   : {len(winners)}")
    print(f"SL hit        : {len(losers)}")
    print(f"Timeouts      : {len(timeouts)}")
    print(f"Avg hold      : {avg_days} days")
    print(f"")
    print(f"Avg win       : Rs +{avg_win:,} ({avg_win_pct}%)")
    print(f"Avg loss      : Rs {avg_loss:,} ({avg_loss_pct}%)")
    print(f"EV/trade      : Rs {ev:,}")
    print(f"Monthly avg   : Rs {round(total_pnl/total_months):,}")
    print(f"Total 5yr P&L : Rs {total_pnl:+,}")
    print(f"Final capital : Rs {final_capital:,.0f}")
    print(f"Return        : {round((final_capital-total_capital)/total_capital*100,1)}%")
    print(f"Max drawdown  : Rs {max_dd:,.0f} ({round(max_dd/total_capital*100,1)}%)")
    print(f"Max consec L  : {max_cl}")
    print(f"")
    print(f"Monthly stats :")
    print(f"  Profitable  : {len(pos_months)} | Avg: Rs {round(sum(pos_months)/len(pos_months)):+,}" if pos_months else "  Profitable: 0")
    print(f"  Loss months : {len(neg_months)} | Avg: Rs {round(sum(neg_months)/len(neg_months)):,}" if neg_months else "  Loss months: 0")
    print(f"  Zero months : {zero}")
    print(f"")
    print(f"Year by Year:")
    for year in range(2021,2027):
        yt = [t for t in trades if t['signal_date'].startswith(str(year))]
        if not yt: continue
        yp = sum(t['total_pnl'] for t in yt)
        yw = [t for t in yt if t['result']=='TARGET']
        print(f"  {year}: {len(yt):>3} trades | "
              f"WR:{round(len(yw)/len(yt)*100,1):>5}% | "
              f"P&L: Rs {yp:>+8,.0f} | "
              f"Monthly: Rs {round(yp/12):>+6,}")

    return {
        'label'     : label,
        'trades'    : len(trades),
        'per_month' : per_month,
        'win_rate'  : win_rate,
        'ev'        : ev,
        'total_pnl' : total_pnl,
        'monthly'   : round(total_pnl/total_months),
        'max_dd'    : max_dd,
        'max_cl'    : max_cl,
        'pos_months': len(pos_months),
        'neg_months': len(neg_months),
        'avg_pos'   : round(sum(pos_months)/len(pos_months)) if pos_months else 0,
        'avg_neg'   : round(sum(neg_months)/len(neg_months)) if neg_months else 0,
        'final'     : final_capital,
    }

if __name__ == '__main__':
    print("HV DAY ENTRY BACKTEST (2021-2026)")
    print("Entry=HV Close | SL=HV Low | Target=HV High")
    print("="*65)

    configs = [
        # (pos_size, capital, vol_ratio, candle_pos, label)
        (20000, 50000,  2.0, 0.5, 'Vol=2x | Candle=50% | Rs20k | Rs50k'),
        (20000, 50000,  2.0, 0.7, 'Vol=2x | Candle=70% | Rs20k | Rs50k'),
        (20000, 50000,  3.0, 0.5, 'Vol=3x | Candle=50% | Rs20k | Rs50k'),
        (20000, 50000,  3.0, 0.7, 'Vol=3x | Candle=70% | Rs20k | Rs50k'),
        (20000, 100000, 2.0, 0.5, 'Vol=2x | Candle=50% | Rs20k | Rs1L'),
        (20000, 100000, 2.0, 0.7, 'Vol=2x | Candle=70% | Rs20k | Rs1L'),
        (30000, 100000, 2.0, 0.5, 'Vol=2x | Candle=50% | Rs30k | Rs1L'),
        (30000, 100000, 2.0, 0.7, 'Vol=2x | Candle=70% | Rs30k | Rs1L'),
        (40000, 100000, 2.0, 0.7, 'Vol=2x | Candle=70% | Rs40k | Rs1L'),
        (50000, 100000, 2.0, 0.7, 'Vol=2x | Candle=70% | Rs50k | Rs1L'),
    ]

    results = []
    for pos, cap, vol, candle, label in configs:
        t, sk, final = backtest(
            position_size=pos,
            total_capital=cap,
            min_volume_ratio=vol,
            min_candle_pos=candle
        )
        r = analyze(t, sk, final, label, cap, pos)
        if r: results.append(r)

    print(f"\n{'='*90}")
    print(f"  COMPARISON TABLE")
    print(f"{'='*90}")
    print(f"{'Config':<42} {'/Mo':>4} {'WR%':>5} {'EV':>7} "
          f"{'Monthly':>9} {'MaxDD':>8} {'+Mo':>4} {'-Mo':>4} "
          f"{'AvgWin':>8} {'AvgLoss':>8} {'Target'}")
    print(f"{'─'*90}")
    for r in results:
        hit = '✅' if r['monthly'] >= 5000 else \
              '⚠️' if r['monthly'] >= 3000 else \
              '📈' if r['monthly'] >= 2000 else '❌'
        print(f"{r['label']:<42} {r['per_month']:>4} "
              f"{r['win_rate']:>4}% Rs{r['ev']:>5,} "
              f"Rs{r['monthly']:>7,} "
              f"Rs{r['max_dd']:>6,.0f} "
              f"{r['pos_months']:>4} {r['neg_months']:>4} "
              f"Rs{r['avg_pos']:>6,} Rs{r['avg_neg']:>6,} {hit}")
