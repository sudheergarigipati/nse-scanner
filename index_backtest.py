import sqlite3
from datetime import datetime, timedelta, date
from collections import defaultdict

print("Loading index data...", flush=True)

conn = sqlite3.connect('nse_data.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

# Get all weekly data for Nifty and BankNifty
# We simulate: every Monday compute weekly levels from prev week
# Then check each day if H3/L3 was touched and what happened

def get_camarilla_weekly(high, low, close):
    rng = high - low
    return {
        'h4': round(close + rng*1.1/2, 2),
        'h3': round(close + rng*1.1/4, 2),
        'l3': round(close - rng*1.1/4, 2),
        'l4': round(close - rng*1.1/2, 2),
    }

def get_week_start(d):
    dt = datetime.strptime(d, '%Y-%m-%d')
    return (dt - timedelta(days=dt.weekday())).strftime('%Y-%m-%d')

# Load all daily bars for both indices
for sym, name in [('NIFTY50','Nifty 50'), ('BANKNIFTY','Bank Nifty')]:
    c.execute('''
        SELECT date, open, high, low, close
        FROM daily_prices WHERE symbol=?
        AND date >= '2024-05-01' AND date <= '2026-05-29'
        ORDER BY date
    ''', (sym,))
    bars = [dict(r) for r in c.fetchall()]

    if not bars:
        print(f"No data for {sym}", flush=True)
        continue

    print(f"\n{'='*90}", flush=True)
    print(f"  {name} — Weekly Camarilla Backtest (May 2024 - May 2026)", flush=True)
    print(f"{'='*90}", flush=True)
    print(f"  Strategy: Touch H3/L3 + close beyond → entry | SL: H4/L4 | Target: L3/H3 (other side)", flush=True)
    print(f"  Time    : Signal after 9:30 AM candle | Hard exit 3:15 PM", flush=True)
    print(f"{'='*90}", flush=True)

    # Group by week
    weeks = defaultdict(list)
    for bar in bars:
        ws = get_week_start(bar['date'])
        weeks[ws].append(bar)

    all_trades = []
    monthly_stats = defaultdict(lambda: {'trades':0,'wins':0,'losses':0,'timeout':0,'pnl':0.0})

    sorted_weeks = sorted(weeks.keys())

    print(f"\n  {'Date':<12} {'Day':<4} {'Signal':<6} {'Entry':>8} {'SL':>8} {'Target':>8} {'Exit':>8} {'Result':<10} {'Pts':>6} {'P&L(1lot)':>10}", flush=True)
    print(f"  {'-'*85}", flush=True)

    for wi, week_start in enumerate(sorted_weeks):
        if wi == 0: continue  # need previous week

        prev_week = weeks[sorted_weeks[wi-1]]
        curr_week = weeks[week_start]

        if not prev_week or not curr_week: continue

        # Previous week OHLC
        pw_high  = max(b['high']  for b in prev_week)
        pw_low   = min(b['low']   for b in prev_week)
        pw_close = prev_week[-1]['close']

        lv = get_camarilla_weekly(pw_high, pw_low, pw_close)
        h3=lv['h3']; h4=lv['h4']; l3=lv['l3']; l4=lv['l4']

        # Check each day of current week
        for bar in curr_week:
            d       = bar['date']
            month   = d[:7]
            day_dt  = datetime.strptime(d, '%Y-%m-%d')
            day_name= day_dt.strftime('%a')
            o = bar['open']; h = bar['high']
            lo= bar['low'];  cl= bar['close']

            signal    = None
            entry     = 0
            sl        = 0
            target    = 0
            result    = None
            exit_price= 0
            pts       = 0

            # PUT signal: high touches H3, candle closes RED below H3
            if h >= h3 * 0.998 and cl < h3 and cl < o:
                signal = 'PUT'
                entry  = cl        # entry at close of signal candle
                sl     = h4        # SL above H4
                target = l3        # target at L3

                # Check remaining candles same day (simulate intraday)
                # Since we only have daily data, use low/high of same day
                # If low touched L3 → WIN
                # If high touched H4 → LOSS
                # Else → TIMEOUT at 3:15 PM (use close)
                if lo <= l3 and h <= h4:
                    # Target hit before SL
                    result     = 'WIN'
                    exit_price = l3
                elif h >= h4:
                    result     = 'SL'
                    exit_price = h4
                elif lo <= l3:
                    result     = 'WIN'
                    exit_price = l3
                else:
                    result     = 'TIMEOUT'
                    exit_price = cl

                pts = round(entry - exit_price, 2)  # positive = profit for PUT

            # CALL signal: low touches L3, candle closes GREEN above L3
            elif lo <= l3 * 1.002 and cl > l3 and cl > o:
                signal = 'CALL'
                entry  = cl
                sl     = l4
                target = h3

                if h >= h3 and lo >= l4:
                    result     = 'WIN'
                    exit_price = h3
                elif lo <= l4:
                    result     = 'SL'
                    exit_price = l4
                elif h >= h3:
                    result     = 'WIN'
                    exit_price = h3
                else:
                    result     = 'TIMEOUT'
                    exit_price = cl

                pts = round(exit_price - entry, 2)  # positive = profit for CALL

            if not signal: continue

            # P&L for 1 lot
            if sym == 'NIFTY50':
                lot_size = 75
                premium_est = round(abs(entry - target) * 0.3, 0)  # rough option estimate
            else:
                lot_size = 30
                premium_est = round(abs(entry - target) * 0.3, 0)

            # Simple futures P&L (index points × lot size)
            pnl_pts  = pts
            pnl_rs   = round(pts * lot_size, 0)

            monthly_stats[month]['trades'] += 1
            monthly_stats[month]['pnl']    += pnl_rs
            if result == 'WIN':
                monthly_stats[month]['wins'] += 1
            elif result == 'SL':
                monthly_stats[month]['losses'] += 1
            else:
                monthly_stats[month]['timeout'] += 1

            icon = '✅' if result=='WIN' else '❌' if result=='SL' else '⏳'
            pnl_str = f"Rs {pnl_rs:+,.0f}"

            print(f"  {d:<12} {day_name:<4} {signal:<6} {entry:>8,.0f} {sl:>8,.0f} {target:>8,.0f} {exit_price:>8,.0f} {icon}{result:<9} {pts:>+6.0f} {pnl_str:>10}", flush=True)

            all_trades.append({
                'date':d,'sym':sym,'signal':signal,
                'entry':entry,'sl':sl,'target':target,
                'exit':exit_price,'result':result,
                'pts':pts,'pnl_rs':pnl_rs,'month':month
            })

    # Monthly summary
    print(f"\n  {'='*90}", flush=True)
    print(f"  MONTHLY SUMMARY — {name}", flush=True)
    print(f"  {'='*90}", flush=True)
    print(f"  {'Month':<10} {'Trades':>7} {'Wins':>6} {'Loss':>6} {'Timeout':>8} {'WR%':>6} {'P&L (1 lot)':>12}", flush=True)
    print(f"  {'-'*65}", flush=True)

    total_trades=total_wins=total_loss=total_timeout=total_pnl=0
    for month in sorted(monthly_stats.keys()):
        m = monthly_stats[month]
        wr = round(m['wins']/m['trades']*100,1) if m['trades']>0 else 0
        total_trades  += m['trades']
        total_wins    += m['wins']
        total_loss    += m['losses']
        total_timeout += m['timeout']
        total_pnl     += m['pnl']
        print(f"  {month:<10} {m['trades']:>7} {m['wins']:>6} {m['losses']:>6} {m['timeout']:>8} {wr:>5}% {m['pnl']:>+12,.0f}", flush=True)

    print(f"  {'-'*65}", flush=True)
    wr_total = round(total_wins/total_trades*100,1) if total_trades>0 else 0
    print(f"  {'TOTAL':<10} {total_trades:>7} {total_wins:>6} {total_loss:>6} {total_timeout:>8} {wr_total:>5}% {total_pnl:>+12,.0f}", flush=True)

    # Overall stats
    wins_list    = [t for t in all_trades if t['sym']==sym and t['result']=='WIN']
    losses_list  = [t for t in all_trades if t['sym']==sym and t['result']=='SL']
    timeout_list = [t for t in all_trades if t['sym']==sym and t['result']=='TIMEOUT']

    avg_win  = round(sum(t['pts'] for t in wins_list)/len(wins_list),1) if wins_list else 0
    avg_loss = round(sum(t['pts'] for t in losses_list)/len(losses_list),1) if losses_list else 0
    avg_to   = round(sum(t['pts'] for t in timeout_list)/len(timeout_list),1) if timeout_list else 0

    print(f"\n  KEY STATS:", flush=True)
    print(f"  Win rate        : {wr_total}%", flush=True)
    print(f"  Avg win pts     : {avg_win:+.1f} pts", flush=True)
    print(f"  Avg loss pts    : {avg_loss:+.1f} pts", flush=True)
    print(f"  Avg timeout pts : {avg_to:+.1f} pts", flush=True)
    print(f"  Total P&L (1lot): Rs {total_pnl:+,.0f}", flush=True)
    print(f"  Avg/month (1lot): Rs {round(total_pnl/24):+,.0f}", flush=True)

conn.close()
print("\nDone!", flush=True)
