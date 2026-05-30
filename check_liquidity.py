#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  check_liquidity.py
#  Checks avg daily trading value for camarilla watchlist stocks
#  Filters out illiquid stocks (< 5 Crore daily value)
#  Run manually anytime to review watchlist quality
#
#  Usage:
#    python3 check_liquidity.py
# ═══════════════════════════════════════════════════════════════

import sqlite3
import os
from datetime import date

BASE_DIR = os.path.expanduser('~/nse-scanner')
DB_PATH  = os.path.join(BASE_DIR, 'nse_data.db')

# Minimum average daily traded value in Crores
# Below this = illiquid = hard to exit in swing trade
MIN_VALUE_CR = 5.0

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c    = conn.cursor()

    today      = date.today().strftime('%Y-%m-%d')
    week_start = '2026-05-18'

    # Get current watchlist
    c.execute("""
        SELECT symbol, direction, l3, h3, l4, h4
        FROM   camarilla_watchlist
        WHERE  status = 'WATCHING'
        AND    week_start = ?
        ORDER  BY direction, symbol
    """, (week_start,))
    watchlist = c.fetchall()

    if not watchlist:
        print("No WATCHING stocks found in camarilla_watchlist.")
        conn.close()
        return

    print(f"\n{'='*72}")
    print(f"  CAMARILLA WATCHLIST — LIQUIDITY CHECK")
    print(f"  Week: {week_start}  |  Min daily value: ₹{MIN_VALUE_CR}Cr")
    print(f"{'='*72}")
    print(f"{'SYM':<14} {'DIR':<8} {'AVG VOL':>12} {'AVG CLOSE':>10} "
          f"{'VALUE CR':>10} {'STATUS':>10}")
    print(f"{'-'*72}")

    liquid   = []
    illiquid = []

    for w in watchlist:
        sym  = w['symbol']
        dir_ = w['direction']

        # 20-day avg volume and price
        c.execute("""
            SELECT AVG(volume) as avg_vol,
                   AVG(close)  as avg_close
            FROM (
                SELECT volume, close
                FROM   daily_prices
                WHERE  symbol = ? AND date < ?
                ORDER  BY date DESC LIMIT 20
            )
        """, (sym, week_start))
        r = c.fetchone()

        if not r or not r['avg_vol']:
            print(f"{sym:<14} {dir_:<8} {'NO DATA':>12}")
            continue

        avg_vol   = int(r['avg_vol'])
        avg_close = round(r['avg_close'], 2)
        avg_value = round(avg_vol * avg_close / 1e7, 2)   # in Crores
        status    = 'LIQUID ✅' if avg_value >= MIN_VALUE_CR else 'LOW ❌'

        print(f"{sym:<14} {dir_:<8} {avg_vol:>12,} {avg_close:>10.2f} "
              f"{avg_value:>9.2f}Cr {status:>10}")

        if avg_value >= MIN_VALUE_CR:
            liquid.append({
                'symbol':    sym,
                'direction': dir_,
                'avg_vol':   avg_vol,
                'avg_close': avg_close,
                'avg_value': avg_value,
                'l3':        w['l3'],
                'h3':        w['h3'],
                'l4':        w['l4'],
                'h4':        w['h4'],
            })
        else:
            illiquid.append(sym)

    # Summary
    print(f"\n{'='*72}")
    print(f"  LIQUID   : {len(liquid)} stocks  ✅")
    print(f"  ILLIQUID : {len(illiquid)} stocks  ❌  {illiquid}")
    print(f"{'='*72}")

    # Final clean watchlist
    if liquid:
        print(f"\n  FINAL TRADEABLE WATCHLIST ({len(liquid)} stocks)")
        print(f"  {'SYM':<14} {'DIR':<8} {'ENTRY':>8} {'SL':>8} "
              f"{'T1':>8} {'T2':>8} {'VALUE':>10}")
        print(f"  {'-'*68}")
        for s in sorted(liquid, key=lambda x: x['avg_value'], reverse=True):
            entry = s['l3'] if s['direction'] == 'BULLISH' else s['h3']
            sl    = s['l4'] if s['direction'] == 'BULLISH' else s['h4']
            t1    = s['h3'] if s['direction'] == 'BULLISH' else s['l3']
            t2    = s['h4'] if s['direction'] == 'BULLISH' else s['l4']
            print(f"  {s['symbol']:<14} {s['direction']:<8} "
                  f"{entry:>8.2f} {sl:>8.2f} "
                  f"{t1:>8.2f} {t2:>8.2f} "
                  f"{s['avg_value']:>8.2f}Cr")

    conn.close()

if __name__ == '__main__':
    main()
