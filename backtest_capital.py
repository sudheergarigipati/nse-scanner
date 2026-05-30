#!/usr/bin/env python3
"""
Backtest with capital management:
- Rs 50,000 total capital
- Rs 20,000 per trade
- Max 2 simultaneous positions
- Track fund availability
- Show exact entry/exit dates
"""
import sqlite3
from datetime import date, timedelta, datetime
import os

BASE_DIR = os.path.expanduser('~/nse-scanner')
DB_PATH  = os.path.join(BASE_DIR, 'nse_data.db')

def get_week_start(d):
    return d - timedelta(days=d.weekday())

def backtest_with_capital(
        l3_vs_hv_max=5.0, above_hv_max=5.0,
        min_upside=8.0, hv_age_min=7, hv_age_max=150,
        max_hold=30, position_size=20000, total_capital=50000):

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute('''
        SELECT h.symbol, h.hv_date, h.hv_low, h.hv_high,
               ROUND((h.hv_high-h.hv_low)/h.hv_low*100,1) as upside_pct
        FROM hv_summary h
        WHERE h.hv_low > 0
        AND h.hv_high > h.hv_low * 1.05
        AND (h.hv_close-h.hv_low)/(h.hv_high-h.hv_low) >= 0.5
        AND h.hv_date >= "2021-01-01"
        AND h.hv_date <= "2026-05-01"
        ORDER BY h.hv_date ASC
    ''')
    setups = c.fetchall()

    # First pass — find all raw signals
    raw_signals = []

    for s in setups:
        sym     = s['symbol']
        hv_date = s['hv_date']
        hv_low  = float(s['hv_low'])
        hv_high = float(s['hv_high'])
        upside  = float(s['upside_pct'])

        if upside < min_upside: continue

        c.execute('''SELECT date,open,high,low,close,volume
                     FROM daily_prices WHERE symbol=? AND date > ?
                     ORDER BY date ASC''', (sym, hv_date))
        prices = c.fetchall()
        if not prices: continue

        hv_dt = datetime.strptime(hv_date, '%Y-%m-%d').date()

        for i, row in enumerate(prices):
            curr_dt = datetime.strptime(row['date'], '%Y-%m-%d').date()
            age = (curr_dt - hv_dt).days

            if age < hv_age_min: continue
            if age > hv_age_max: break
            if i == 0: continue

            # Weekly cam
            week_start = get_week_start(curr_dt)
            prev_w_end = week_start - timedelta(days=1)
            prev_w_st  = week_start - timedelta(days=7)

            c.execute('''SELECT MAX(high) as wh, MIN(low) as wl
                         FROM daily_prices WHERE symbol=? AND date>=? AND date<=?''',
                      (sym, prev_w_st.strftime('%Y-%m-%d'),
                       prev_w_end.strftime('%Y-%m-%d')))
            wr = c.fetchone()
            c.execute('''SELECT close FROM daily_prices WHERE symbol=? AND date<=?
                         ORDER BY date DESC LIMIT 1''',
                      (sym, prev_w_end.strftime('%Y-%m-%d')))
            wc = c.fetchone()

            if not wr or not wr['wh'] or not wc: continue
            w_rng   = float(wr['wh']) - float(wr['wl'])
            w_close = float(wc['close'])
            if w_rng <= 0: continue

            wl3 = round(w_close - w_rng * 0.55/2, 2)
            wh3 = round(w_close + w_rng * 0.55/2, 2)

            # Daily L4
            prev  = prices[i-1]
            d_rng = float(prev['high']) - float(prev['low'])
            d_l4  = round(float(prev['close']) - d_rng * 1.1/2, 2)

            l3_vs_hv = abs(wl3 - hv_low) / hv_low * 100
            if l3_vs_hv > l3_vs_hv_max: continue

            curr_low   = float(row['low'])
            curr_close = float(row['close'])
            curr_open  = float(row['open'])

            if curr_low > wl3 * 1.005: continue
            if curr_close <= curr_open: continue

            pct_above = (curr_close - hv_low) / hv_low * 100
            if pct_above > above_hv_max: continue
            if d_l4 >= wl3: continue
            if hv_high <= wl3: continue

            risk = wl3 - d_l4
            if risk <= 0: continue
            rr_t2 = round((hv_high - wl3) / risk, 1)
            if rr_t2 < 3.0: continue

            raw_signals.append({
                'signal_date': row['date'],
                'symbol'     : sym,
                'entry'      : wl3,
                'sl'         : d_l4,
                'wh3'        : wh3,
                'hv_high'    : hv_high,
                'prices_idx' : i,
                'prices'     : prices,
                'sl_pct'     : round(risk/wl3*100, 2),
            })
            break

    conn.close()

    # Sort by signal date
    raw_signals.sort(key=lambda x: x['signal_date'])

    # Second pass — simulate with capital management
    capital     = total_capital
    open_trades = []  # currently open positions
    all_trades  = []
    skipped     = []

    for sig in raw_signals:
        sig_date = sig['signal_date']
        sym      = sig['symbol']

        # Close any finished trades before this signal
        still_open = []
        for ot in open_trades:
            if ot['exit_date'] <= sig_date:
                capital += ot['capital_used']
                capital += ot['total_pnl']
                all_trades.append(ot)
            else:
                still_open.append(ot)
        open_trades = still_open

        # Check if already in this stock
        if any(ot['symbol'] == sym for ot in open_trades):
            skipped.append({'date': sig_date, 'symbol': sym,
                           'reason': 'already_in_position'})
            continue

        # Check capital availability
        if capital < position_size:
            skipped.append({'date': sig_date, 'symbol': sym,
                           'reason': f'insufficient_capital_Rs{capital:.0f}'})
            continue

        # Simulate trade
        prices    = sig['prices']
        i         = sig['prices_idx']
        wl3       = sig['entry']
        d_l4      = sig['sl']
        wh3       = sig['wh3']
        hv_high   = sig['hv_high']
        shares    = int(position_size / wl3)
        if shares < 1: continue
        l1_sh     = shares // 2
        l2_sh     = shares - l1_sh

        p1_result = 'TIMEOUT'
        p2_result = 'TIMEOUT'
        p1_exit   = float(prices[min(i+max_hold, len(prices)-1)]['close'])
        p2_exit   = p1_exit
        exit_date = prices[min(i+max_hold, len(prices)-1)]['date']
        days_held = 0

        for future in prices[i+1:i+max_hold+1]:
            days_held += 1
            fh = float(future['high'])
            fl = float(future['low'])

            if fl <= d_l4:
                if p1_result == 'TIMEOUT':
                    p1_result = 'SL'; p1_exit = d_l4
                if p2_result == 'TIMEOUT':
                    p2_result = 'SL'; p2_exit = d_l4
                exit_date = future['date']
                break

            if p1_result == 'TIMEOUT' and fh >= wh3:
                p1_result = 'T1'; p1_exit = wh3

            if p2_result == 'TIMEOUT' and fh >= hv_high:
                p2_result = 'T2'; p2_exit = hv_high
                exit_date = future['date']
                break

        p1_pnl = round((p1_exit - wl3) * l1_sh, 0)
        p2_pnl = round((p2_exit - wl3) * l2_sh, 0)
        total  = p1_pnl + p2_pnl

        if p1_result == 'SL':
            overall = 'FULL_LOSS'
        elif p1_result == 'T1' and p2_result == 'T2':
            overall = 'FULL_WIN'
        elif p1_result == 'T1':
            overall = 'T1_ONLY'
        else:
            overall = 'TIMEOUT'

        capital_used = shares * wl3

        trade = {
            'signal_date' : sig_date,
            'symbol'      : sym,
            'entry'       : round(wl3, 2),
            'sl'          : round(d_l4, 2),
            'wh3'         : round(wh3, 2),
            'hv_high'     : round(hv_high, 2),
            'shares'      : shares,
            'capital_used': round(capital_used, 0),
            'p1_result'   : p1_result,
            'p2_result'   : p2_result,
            'p1_pnl'      : p1_pnl,
            'p2_pnl'      : p2_pnl,
            'total_pnl'   : total,
            'overall'     : overall,
            'exit_date'   : exit_date,
            'days_held'   : days_held,
            'sl_pct'      : sig['sl_pct'],
            'capital_before': round(capital, 0),
        }

        # Deduct capital
        capital -= capital_used
        open_trades.append(trade)

    # Close remaining open trades
    for ot in open_trades:
        capital += ot['capital_used']
        capital += ot['total_pnl']
        all_trades.append(ot)

    return all_trades, skipped, capital

def analyze(trades, skipped, final_capital, total_capital=50000):
    if not trades:
        print("No trades")
        return

    print(f"\n{'='*100}")
    print(f"  HYBRID BACKTEST WITH CAPITAL MANAGEMENT")
    print(f"  Entry=W-L3 | SL=Daily-L4 | T1=W-H3(50%) | T2=HV-High(50%)")
    print(f"  Capital: Rs {total_capital:,} | Position: Rs 20,000/trade")
    print(f"{'='*100}")

    # Detailed trade log
    print(f"\n{'Date':<12} {'Symbol':<12} {'Entry':>8} {'SL':>8} "
          f"{'T1':>8} {'T2':>8} {'Shares':>6} {'Capital':>9} "
          f"{'P1':>7} {'P2':>7} {'Total':>8} {'Result':<12} {'Exit Date':<12} {'Days':>4}")
    print('-'*130)

    for t in trades:
        print(f"{t['signal_date']:<12} {t['symbol']:<12} "
              f"Rs{t['entry']:>7.1f} Rs{t['sl']:>7.1f} "
              f"Rs{t['wh3']:>7.1f} Rs{t['hv_high']:>7.1f} "
              f"{t['shares']:>6} Rs{t['capital_before']:>7,.0f} "
              f"Rs{t['p1_pnl']:>+6,.0f} Rs{t['p2_pnl']:>+6,.0f} "
              f"Rs{t['total_pnl']:>+7,.0f} {t['overall']:<12} "
              f"{t['exit_date']:<12} {t['days_held']:>4}d")

    # Summary stats
    winners   = [t for t in trades if t['total_pnl'] > 0]
    losers    = [t for t in trades if t['total_pnl'] <= 0]
    full_wins = [t for t in trades if t['overall'] == 'FULL_WIN']
    t1_only   = [t for t in trades if t['overall'] == 'T1_ONLY']
    full_loss = [t for t in trades if t['overall'] == 'FULL_LOSS']
    timeouts  = [t for t in trades if t['overall'] == 'TIMEOUT']

    total_pnl  = sum(t['total_pnl'] for t in trades)
    win_rate   = round(len(winners)/len(trades)*100, 1)
    avg_win    = round(sum(t['total_pnl'] for t in winners)/len(winners), 0) if winners else 0
    avg_loss   = round(sum(t['total_pnl'] for t in losers)/len(losers), 0) if losers else 0
    avg_days   = round(sum(t['days_held'] for t in trades)/len(trades), 1)

    total_months = 64
    per_month    = round(len(trades)/total_months, 1)

    # Max drawdown
    cum = 0; peak = 0; max_dd = 0
    for t in trades:
        cum += t['total_pnl']
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    # Consecutive losses
    max_cl = 0; cl = 0
    for t in trades:
        if t['total_pnl'] <= 0: cl += 1; max_cl = max(max_cl, cl)
        else: cl = 0

    print(f"\n{'='*70}")
    print(f"OVERALL SUMMARY:")
    print(f"{'='*70}")
    print(f"Total trades    : {len(trades)} ({per_month}/month)")
    print(f"Skipped signals : {len(skipped)}")
    print(f"")
    print(f"Full wins (T1+T2): {len(full_wins)} ({round(len(full_wins)/len(trades)*100,1)}%)")
    print(f"T1 only (H3)    : {len(t1_only)} ({round(len(t1_only)/len(trades)*100,1)}%)")
    print(f"Full losses (SL): {len(full_loss)} ({round(len(full_loss)/len(trades)*100,1)}%)")
    print(f"Timeouts        : {len(timeouts)} ({round(len(timeouts)/len(trades)*100,1)}%)")
    print(f"Win rate        : {win_rate}%")
    print(f"")
    print(f"Avg win         : Rs {avg_win:+,}")
    print(f"Avg loss        : Rs {avg_loss:,}")
    print(f"Avg hold        : {avg_days} days")
    print(f"Max consec loss : {max_cl}")
    print(f"Max drawdown    : Rs {max_dd:,.0f}")
    print(f"")
    print(f"Total P&L       : Rs {total_pnl:+,}")
    print(f"Starting capital: Rs {total_capital:,}")
    print(f"Final capital   : Rs {final_capital:,.0f}")
    print(f"Return          : {round((final_capital-total_capital)/total_capital*100,1)}%")
    print(f"")

    # Monthly breakdown
    print(f"MONTHLY BREAKDOWN:")
    months = {}
    for t in trades:
        m = t['signal_date'][:7]
        if m not in months: months[m] = []
        months[m].append(t)

    monthly_pnl = []
    for m in sorted(months.keys()):
        mt = months[m]
        mp = sum(t['total_pnl'] for t in mt)
        mw = [t for t in mt if t['total_pnl'] > 0]
        monthly_pnl.append(mp)
        sl_hits = [t for t in mt if t['overall'] == 'FULL_LOSS']
        print(f"  {m}: {len(mt):>3} trades | "
              f"WR:{round(len(mw)/len(mt)*100,1):>5}% | "
              f"SL:{len(sl_hits)} | "
              f"P&L: Rs {mp:>+8,.0f}")

    print(f"")
    neg_months = [m for m in monthly_pnl if m < 0]
    pos_months = [m for m in monthly_pnl if m > 0]
    zero_months = 64 - len(monthly_pnl)
    print(f"Profitable months: {len(pos_months)}")
    print(f"Loss months      : {len(neg_months)}")
    print(f"Zero months      : {zero_months}")
    print(f"Avg profit month : Rs {round(sum(pos_months)/len(pos_months)):,}" if pos_months else "")
    print(f"Avg loss month   : Rs {round(sum(neg_months)/len(neg_months)):,}" if neg_months else "")

    # Skipped signals
    if skipped:
        print(f"\nSKIPPED SIGNALS ({len(skipped)}):")
        for s in skipped[:10]:
            print(f"  {s['date']} {s['symbol']}: {s['reason']}")
        if len(skipped) > 10:
            print(f"  ... and {len(skipped)-10} more")

if __name__ == '__main__':
    trades, skipped, final_capital = backtest_with_capital()
    analyze(trades, skipped, final_capital)
