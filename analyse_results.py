import sqlite3
from collections import defaultdict

print("Analysing optimised backtest results...", flush=True)

rdb = sqlite3.connect('optimised_backtest.db')
rdb.row_factory = sqlite3.Row
rc  = rdb.cursor()

# ── Overall stats ─────────────────────────────────────────────
rc.execute('SELECT COUNT(*) as n FROM signals')
total = rc.fetchone()['n']
rc.execute('SELECT COUNT(*) as n FROM signals WHERE result=?', ('WIN',))
wins = rc.fetchone()['n']
rc.execute('SELECT COUNT(*) as n FROM signals WHERE result=?', ('SL',))
losses = rc.fetchone()['n']
rc.execute('SELECT COUNT(*) as n FROM signals WHERE result=?', ('OPEN',))
opens = rc.fetchone()['n']

print(f"\n{'='*65}")
print(f"  OVERALL STATS")
print(f"{'='*65}")
print(f"  Total signals : {total}")
print(f"  Wins          : {wins}  ({round(wins/total*100,1)}%)")
print(f"  SL hits       : {losses} ({round(losses/total*100,1)}%)")
print(f"  Open/timeout  : {opens}  ({round(opens/total*100,1)}%)")
rc.execute('SELECT AVG(pnl_rs) as ap FROM signals WHERE result=?',('WIN',))
print(f"  Avg win Rs    : {round(rc.fetchone()['ap'] or 0):,}")
rc.execute('SELECT AVG(pnl_rs) as ap FROM signals WHERE result=?',('SL',))
print(f"  Avg loss Rs   : {round(rc.fetchone()['ap'] or 0):,}")
rc.execute('SELECT AVG(days_held) as d FROM signals WHERE result=?',('WIN',))
print(f"  Avg days(win) : {round(rc.fetchone()['d'] or 0,1)}")
rc.execute('SELECT AVG(days_held) as d FROM signals WHERE result=?',('SL',))
print(f"  Avg days(loss): {round(rc.fetchone()['d'] or 0,1)}")

# ── Month by month ────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  MONTH BY MONTH SIGNAL ANALYSIS")
print(f"{'='*65}")
print(f"  {'Month':<10} {'Signals':>8} {'Wins':>6} {'Loss':>6} {'Open':>6} {'WR%':>6} {'AvgPnL':>10} {'TotalPnL':>12}")
print(f"  {'-'*70}")

rc.execute('''
    SELECT substr(date,1,7) as month,
           COUNT(*) as n,
           SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as w,
           SUM(CASE WHEN result='SL'  THEN 1 ELSE 0 END) as l,
           SUM(CASE WHEN result='OPEN' THEN 1 ELSE 0 END) as o,
           AVG(pnl_rs) as ap,
           SUM(pnl_rs) as tp
    FROM signals
    GROUP BY month
    ORDER BY month
''')
for r in rc.fetchall():
    wr = round(r['w']/r['n']*100,1) if r['n']>0 else 0
    print(f"  {r['month']:<10} {r['n']:>8} {r['w']:>6} {r['l']:>6} {r['o']:>6} {wr:>5}% {round(r['ap'] or 0):>+10,} {round(r['tp'] or 0):>+12,}")

# ── HV Age breakdown ──────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  HV AGE BREAKDOWN")
print(f"{'='*65}")
print(f"  {'Age Range':<15} {'Signals':>8} {'WR%':>6} {'AvgWin':>10} {'AvgLoss':>10} {'AvgPnL':>10}")
print(f"  {'-'*65}")
for a1,a2 in [(7,15),(16,30),(31,45),(46,60)]:
    rc.execute('''SELECT COUNT(*) as n,
        SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as w,
        AVG(CASE WHEN result='WIN' THEN pnl_rs END) as aw,
        AVG(CASE WHEN result='SL'  THEN pnl_rs END) as al,
        AVG(pnl_rs) as ap
        FROM signals WHERE hv_age>=? AND hv_age<=?''',(a1,a2))
    r=rc.fetchone()
    if r['n']==0: continue
    wr=round(r['w']/r['n']*100,1)
    print(f"  {a1:>3}-{a2:>3} days    {r['n']:>8} {wr:>5}% {round(r['aw'] or 0):>+10,} {round(r['al'] or 0):>+10,} {round(r['ap'] or 0):>+10,}")

# ── Volume ratio breakdown ────────────────────────────────────
print(f"\n{'='*65}")
print(f"  VOLUME RATIO BREAKDOWN")
print(f"{'='*65}")
print(f"  {'Vol Range':<15} {'Signals':>8} {'WR%':>6} {'AvgWin':>10} {'AvgLoss':>10} {'AvgPnL':>10}")
print(f"  {'-'*65}")
for v1,v2 in [(1.5,2.0),(2.0,3.0),(3.0,5.0),(5.0,99)]:
    rc.execute('''SELECT COUNT(*) as n,
        SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as w,
        AVG(CASE WHEN result='WIN' THEN pnl_rs END) as aw,
        AVG(CASE WHEN result='SL'  THEN pnl_rs END) as al,
        AVG(pnl_rs) as ap
        FROM signals WHERE vol_ratio>=? AND vol_ratio<?''',(v1,v2))
    r=rc.fetchone()
    if not r or r['n']==0: continue
    wr=round(r['w']/r['n']*100,1)
    print(f"  {v1:.1f}x-{v2:.1f}x       {r['n']:>8} {wr:>5}% {round(r['aw'] or 0):>+10,} {round(r['al'] or 0):>+10,} {round(r['ap'] or 0):>+10,}")

# ── RR breakdown ──────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  R:R BREAKDOWN")
print(f"{'='*65}")
print(f"  {'RR Range':<15} {'Signals':>8} {'WR%':>6} {'AvgWin':>10} {'AvgLoss':>10} {'AvgPnL':>10}")
print(f"  {'-'*65}")
for r1,r2 in [(5,7),(7,10),(10,15),(15,20)]:
    rc.execute('''SELECT COUNT(*) as n,
        SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as w,
        AVG(CASE WHEN result='WIN' THEN pnl_rs END) as aw,
        AVG(CASE WHEN result='SL'  THEN pnl_rs END) as al,
        AVG(pnl_rs) as ap
        FROM signals WHERE rr>=? AND rr<?''',(r1,r2))
    r=rc.fetchone()
    if not r or r['n']==0: continue
    wr=round(r['w']/r['n']*100,1)
    print(f"  {r1:>2}x-{r2:>2}x         {r['n']:>8} {wr:>5}% {round(r['aw'] or 0):>+10,} {round(r['al'] or 0):>+10,} {round(r['ap'] or 0):>+10,}")

# ── Top winning stocks ────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  TOP 20 STOCKS BY TOTAL PROFIT")
print(f"{'='*65}")
print(f"  {'Symbol':<15} {'Trades':>7} {'Wins':>6} {'Loss':>6} {'WR%':>6} {'TotalPnL':>12} {'AvgPnL':>10}")
print(f"  {'-'*65}")
rc.execute('''
    SELECT symbol,
           COUNT(*) as n,
           SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as w,
           SUM(CASE WHEN result='SL'  THEN 1 ELSE 0 END) as l,
           SUM(pnl_rs) as tp,
           AVG(pnl_rs) as ap
    FROM signals
    GROUP BY symbol
    ORDER BY tp DESC
    LIMIT 20
''')
for r in rc.fetchall():
    wr=round(r['w']/r['n']*100,1) if r['n']>0 else 0
    print(f"  {r['symbol']:<15} {r['n']:>7} {r['w']:>6} {r['l']:>6} {wr:>5}% {round(r['tp'] or 0):>+12,} {round(r['ap'] or 0):>+10,}")

# ── Top losing stocks ─────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  TOP 10 STOCKS BY TOTAL LOSS")
print(f"{'='*65}")
print(f"  {'Symbol':<15} {'Trades':>7} {'Wins':>6} {'Loss':>6} {'WR%':>6} {'TotalPnL':>12}")
print(f"  {'-'*65}")
rc.execute('''
    SELECT symbol,
           COUNT(*) as n,
           SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as w,
           SUM(CASE WHEN result='SL'  THEN 1 ELSE 0 END) as l,
           SUM(pnl_rs) as tp
    FROM signals
    GROUP BY symbol
    ORDER BY tp ASC
    LIMIT 10
''')
for r in rc.fetchall():
    wr=round(r['w']/r['n']*100,1) if r['n']>0 else 0
    print(f"  {r['symbol']:<15} {r['n']:>7} {r['w']:>6} {r['l']:>6} {wr:>5}% {round(r['tp'] or 0):>+12,}")

# ── Consecutive losses analysis ───────────────────────────────
print(f"\n{'='*65}")
print(f"  DRAWDOWN ANALYSIS")
print(f"{'='*65}")
rc.execute('SELECT date, result, pnl_rs FROM signals ORDER BY date')
all_trades = rc.fetchall()
cumulative = 0
peak = 0
max_dd = 0
max_dd_date = ''
consec_loss = 0
max_consec = 0

for t in all_trades:
    cumulative += t['pnl_rs']
    if t['result'] == 'SL':
        consec_loss += 1
        max_consec = max(max_consec, consec_loss)
    else:
        consec_loss = 0
    if cumulative > peak:
        peak = cumulative
    dd = peak - cumulative
    if dd > max_dd:
        max_dd = dd
        max_dd_date = t['date']

print(f"  Max drawdown      : Rs {max_dd:,.0f} on {max_dd_date}")
print(f"  Max consec losses : {max_consec}")
print(f"  Total P&L (all)   : Rs {cumulative:+,.0f}")

# ── Filter rejection analysis ─────────────────────────────────
print(f"\n{'='*65}")
print(f"  TOP FILTER REJECTION REASONS")
print(f"{'='*65}")
rc.execute('''
    SELECT reason, COUNT(*) as n
    FROM filter_log
    GROUP BY reason
    ORDER BY n DESC
    LIMIT 10
''')
for r in rc.fetchall():
    print(f"  {r['reason']:<40} : {r['n']:,}")

# ── Best performing months ────────────────────────────────────
print(f"\n{'='*65}")
print(f"  BEST vs WORST MONTHS")
print(f"{'='*65}")
rc.execute('''
    SELECT substr(date,1,7) as month, SUM(pnl_rs) as tp,
           COUNT(*) as n,
           SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as w
    FROM signals GROUP BY month ORDER BY tp DESC
''')
rows = rc.fetchall()
print("  TOP 5 BEST:")
for r in rows[:5]:
    wr = round(r['w']/r['n']*100,1)
    print(f"    {r['month']} → Rs {round(r['tp']):>+8,} | {r['n']} trades | WR {wr}%")
print("  TOP 5 WORST:")
for r in rows[-5:]:
    wr = round(r['w']/r['n']*100,1)
    print(f"    {r['month']} → Rs {round(r['tp']):>+8,} | {r['n']} trades | WR {wr}%")

rdb.close()
print(f"\nDone!", flush=True)
