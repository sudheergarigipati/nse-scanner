#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  camarilla_api.py
#  Camarilla data endpoints for the NSE Scanner UI
#  Add this to price_api.py — paste at the end before if __name__
#
#  Endpoints:
#    /camarilla          — full watchlist with live price vs levels
#    /camarilla/triggers — today's fired entry signals
#    /camarilla/levels/<sym> — full levels for one symbol
# ═══════════════════════════════════════════════════════════════

# ── Camarilla watchlist endpoint ──────────────────────────────
@app.route('/camarilla')
def get_camarilla():
    conn = get_db()
    if not conn:
        return jsonify({'error': 'DB not ready'}), 500

    c         = conn.cursor()
    today     = str(date.today())
    # Monday of current week
    from datetime import timedelta
    d          = date.today()
    week_start = (d - timedelta(days=d.weekday())).strftime('%Y-%m-%d')

    # Get watchlist for current week
    c.execute('''
        SELECT w.symbol, w.direction, w.status,
               w.h3, w.h4, w.h5, w.l3, w.l4, w.l5,
               w.prev_close, w.ema20w, w.added_date,
               p.close as latest_close,
               p.high  as today_high,
               p.low   as today_low,
               p.volume as today_vol
        FROM   camarilla_watchlist w
        LEFT JOIN daily_prices p
               ON w.symbol = p.symbol
               AND p.date = (SELECT MAX(date) FROM daily_prices
                              WHERE symbol = w.symbol)
        WHERE  w.week_start = ?
        ORDER  BY w.direction, w.status, w.symbol
    ''', (week_start,))
    rows = c.fetchall()

    # Get today's triggers
    c.execute('''
        SELECT symbol, direction, trigger_type,
               entry_price, stop_loss, target1, target2,
               risk_reward, trigger_time
        FROM   camarilla_triggers
        WHERE  trigger_date = ?
        ORDER  BY trigger_time DESC
    ''', (today,))
    triggers = [dict(t) for t in c.fetchall()]

    # Get avg volumes for distance calculation
    watchlist = []
    for r in rows:
        sym       = r['symbol']
        direction = r['direction']
        close     = r['latest_close'] or r['prev_close']
        l3        = r['l3']
        h3        = r['h3']
        l4        = r['l4']
        h4        = r['h4']

        # Distance from key level
        if direction == 'BULLISH':
            level    = l3
            sl_level = l4
            t1_level = h3
            t2_level = h4
        else:
            level    = h3
            sl_level = h4
            t1_level = l3
            t2_level = l4

        dist_pct = round(abs(close - level) / close * 100, 2) if close else 0
        sl_pct   = round(abs(level - sl_level) / level * 100, 1) if level else 0

        # Status vs level
        if direction == 'BULLISH':
            if close and close >= l3 and close <= h3:
                price_status = 'IN_ZONE'
            elif close and close < l3:
                price_status = 'BELOW_L3'
            else:
                price_status = 'ABOVE_L3'
        else:
            if close and close <= h3 and close >= l3:
                price_status = 'IN_ZONE'
            elif close and close > h3:
                price_status = 'ABOVE_H3'
            else:
                price_status = 'BELOW_H3'

        watchlist.append({
            'symbol':       sym,
            'direction':    direction,
            'status':       r['status'],
            'close':        round(close, 2) if close else 0,
            'today_high':   round(r['today_high'], 2) if r['today_high'] else 0,
            'today_low':    round(r['today_low'],  2) if r['today_low']  else 0,
            'today_vol':    r['today_vol'] or 0,
            'l3':           round(l3, 2),
            'l4':           round(l4, 2),
            'l5':           round(r['l5'], 2),
            'h3':           round(h3, 2),
            'h4':           round(h4, 2),
            'h5':           round(r['h5'], 2),
            'entry':        round(level,    2),
            'stop_loss':    round(sl_level, 2),
            'target1':      round(t1_level, 2),
            'target2':      round(t2_level, 2),
            'sl_pct':       sl_pct,
            'dist_pct':     dist_pct,
            'ema20w':       round(r['ema20w'], 2) if r['ema20w'] else 0,
            'added_date':   r['added_date'],
            'price_status': price_status,
        })

    conn.close()

    bullish  = [s for s in watchlist if s['direction'] == 'BULLISH']
    bearish  = [s for s in watchlist if s['direction'] == 'BEARISH']
    watching = [s for s in watchlist if s['status'] == 'WATCHING']
    triggered = [s for s in watchlist if s['status'] == 'TRIGGERED']

    return jsonify({
        'week_start':  week_start,
        'today':       today,
        'watchlist':   watchlist,
        'bullish':     bullish,
        'bearish':     bearish,
        'triggers':    triggers,
        'counts': {
            'total':     len(watchlist),
            'bullish':   len(bullish),
            'bearish':   len(bearish),
            'watching':  len(watching),
            'triggered': len(triggered),
            'triggers_today': len(triggers),
        },
        'timestamp': get_ist_time().strftime('%d-%b-%Y %H:%M IST'),
    })


@app.route('/camarilla/levels/<sym>')
def get_camarilla_levels(sym):
    """Full Camarilla levels for a single symbol."""
    conn = get_db()
    if not conn:
        return jsonify({'error': 'DB not ready'}), 500
    c          = conn.cursor()
    d          = date.today()
    week_start = (d - timedelta(days=d.weekday())).strftime('%Y-%m-%d')
    c.execute('''
        SELECT * FROM weekly_camarilla
        WHERE  symbol = ? AND week_start = ?
    ''', (sym.upper(), week_start))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'No levels found'}), 404
    return jsonify(dict(row))
