#!/usr/bin/env python3
# price_api.py — GCP VM price + HV alert API — Port 8081

from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import sqlite3
import time
import os
import json
from datetime import datetime, date, timedelta
import concurrent.futures

app    = Flask(__name__)
CORS(app)

BASE_DIR = os.path.expanduser('~/nse-scanner')
DB_PATH  = os.path.join(BASE_DIR, 'nse_data.db')

price_cache    = {}
cache_time     = {}
CACHE_TTL      = 180

hv_cache       = []
hv_cache_time  = 0
HV_CACHE_TTL   = 600

def get_db():
    if not os.path.exists(DB_PATH): return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def is_market_open():
    """NSE market hours: 9:15 AM to 3:30 PM IST = 3:45 to 10:00 UTC"""
        ist_hour   = (now_utc.hour + 5) % 24
    ist_minute = (now_utc.minute + 30) % 60
    if now_utc.minute + 30 >= 60:
        ist_hour = (ist_hour + 1) % 24
    ist_mins = ist_hour * 60 + ist_minute
    open_mins  = 9 * 60 + 15
    close_mins = 15 * 60 + 30
    weekday = now_utc.weekday()
    return weekday < 5 and open_mins <= ist_mins <= close_mins

def get_ist_time():
        ist = datetime.now()
    return ist

def pct_market_day_done():
    ist = get_ist_time()
    ist_mins = ist.hour * 60 + ist.minute
    open_mins  = 9 * 60 + 15
    close_mins = 15 * 60 + 30
    total = close_mins - open_mins
    if ist_mins <= open_mins: return 0.0
    if ist_mins >= close_mins: return 100.0
    return round((ist_mins - open_mins) / total * 100, 1)

# ── Price endpoints ──────────────────────────────────────────────
def fetch_price(sym):
    try:
        t = yf.Ticker(f"{sym}.NS")
        h = t.history(period='2d', interval='1d')
        if h.empty: return None
        last = h.iloc[-1]
        return {'price': round(float(last['Close']),2),
                'volume': int(last['Volume']),
                'high':   round(float(last['High']),2),
                'low':    round(float(last['Low']),2),
                'open':   round(float(last['Open']),2),
                'date':   str(h.index[-1].date()), 'live': True}
    except: return None

@app.route('/price/<sym>')
def get_price(sym):
    sym = sym.upper().strip()
    now = time.time()
    if sym in price_cache and (now - cache_time.get(sym,0)) < CACHE_TTL:
        return jsonify(price_cache[sym])
    data = fetch_price(sym)
    if not data: return jsonify({'error':'not found'}), 404
    price_cache[sym] = data; cache_time[sym] = now
    return jsonify(data)

@app.route('/prices')
def get_prices():
    syms_raw = request.args.get('symbols','')
    if not syms_raw: return jsonify({'error':'provide symbols'}), 400
    syms = [s.strip().upper() for s in syms_raw.split(',') if s.strip()]
    now = time.time(); result = {}; to_fetch = []
    for sym in syms:
        if sym in price_cache and (now - cache_time.get(sym,0)) < CACHE_TTL:
            result[sym] = price_cache[sym]
        else: to_fetch.append(sym)
    if to_fetch:
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
            futures = {ex.submit(fetch_price,sym): sym for sym in to_fetch}
            for future in concurrent.futures.as_completed(futures):
                sym = futures[future]; data = future.result()
                if data: result[sym]=data; price_cache[sym]=data; cache_time[sym]=now
    return jsonify({'prices':result,'count':len(result),
                    'timestamp':datetime.now().strftime('%d-%b-%Y %H:%M:%S')})

# ── New HV endpoint ──────────────────────────────────────────────
@app.route('/new-hv')
def get_new_hv():
    global hv_cache, hv_cache_time

    now       = time.time()
    mkt_open  = is_market_open()
    pct_day   = pct_market_day_done()
    ist_now   = get_ist_time()

    if mkt_open and (now - hv_cache_time) < HV_CACHE_TTL and hv_cache:
        return jsonify({'alerts':hv_cache,'cached':True,
                        'market_hours':True,'pct_day_done':pct_day,
                        'timestamp':ist_now.strftime('%d-%b-%Y %H:%M IST')})

    if not mkt_open:
        conn = get_db()
        if not conn:
            return jsonify({'alerts':[],'error':'DB not initialized'})
        c = conn.cursor()
        today_str = str(date.today())
        c.execute('''SELECT symbol, new_volume, old_volume, old_hv_date, price, alerted_at
                     FROM hv_alerts
                     WHERE alert_date = ?
                     ORDER BY new_volume DESC''', (today_str,))
        rows = c.fetchall()
        alerts = []
        for r in rows:
            sym = r['symbol']
            c.execute('''SELECT high, low, close, volume FROM daily_prices
                         WHERE symbol=? AND date=?''', (sym, today_str))
            dp = c.fetchone()
            alerts.append({
                'symbol':       sym,
                'status':       'CONFIRMED',
                'today_vol':    r['new_volume'],
                'hv_vol':       r['old_volume'],
                'prev_hv_date': r['old_hv_date'],
                'price':        dp['close'] if dp else r['price'],
                'high':         dp['high']  if dp else 0,
                'low':          dp['low']   if dp else 0,
                'pct_above':    round((r['new_volume'] - r['old_volume']) / r['old_volume'] * 100, 1),
                'projected':    r['new_volume'],
                'date':         today_str,
            })
        conn.close()
        msg = f"Market closed ({ist_now.strftime('%H:%M IST')}) — showing confirmed new HV stocks for today"
        return jsonify({
            'alerts':       alerts,
            'count':        len(alerts),
            'confirmed':    len(alerts),
            'on_track':     0,
            'market_hours': False,
            'pct_day_done': pct_day,
            'timestamp':    ist_now.strftime('%d-%b-%Y %H:%M IST'),
            'message':      msg,
        })

    conn = get_db()
    if not conn:
        return jsonify({'alerts':[],'error':'DB not initialized'})
    c = conn.cursor()
    c.execute("SELECT symbol, hv_date, hv_volume FROM hv_summary WHERE hv_volume > 0")
    hv_data = {r['symbol']:{'hv_vol':r['hv_volume'],'hv_date':r['hv_date']} for r in c.fetchall()}
    conn.close()

    if not hv_data:
        return jsonify({'alerts':[],'error':'No HV data in DB'})

    symbols = list(hv_data.keys())
    alerts  = []
    pct_day_dec = pct_day / 100.0

    def check_sym(sym):
        try:
            t = yf.Ticker(f"{sym}.NS")
            h = t.history(period='1d', interval='1d')
            if h.empty: return None
            last  = h.iloc[-1]
            vol   = int(last['Volume'])
            close = round(float(last['Close']),2)
            high  = round(float(last['High']),2)
            low   = round(float(last['Low']),2)
            dt    = str(h.index[-1].date())
            if vol <= 0: return None
            hv_vol  = hv_data[sym]['hv_vol']
            hv_date = hv_data[sym]['hv_date']
            proj = int(vol / pct_day_dec) if pct_day_dec > 0.05 else vol
            if vol > hv_vol:
                return {'symbol':sym,'status':'CONFIRMED','today_vol':vol,
                        'hv_vol':hv_vol,'prev_hv_date':hv_date,'price':close,
                        'high':high,'low':low,
                        'pct_above':round((vol-hv_vol)/hv_vol*100,1),
                        'projected':vol,'date':dt}
            elif proj > hv_vol and pct_day_dec >= 0.3:
                vol_pct_of_hv = vol / hv_vol * 100
                if vol_pct_of_hv >= 40:
                    return {'symbol':sym,'status':'ON_TRACK','today_vol':vol,
                            'hv_vol':hv_vol,'prev_hv_date':hv_date,'price':close,
                            'high':high,'low':low,
                            'pct_above':round((proj-hv_vol)/hv_vol*100,1),
                            'projected':proj,'pct_day_done':pct_day,'date':dt}
        except: return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as ex:
        futures = {ex.submit(check_sym,sym): sym for sym in symbols}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result: alerts.append(result)

    alerts.sort(key=lambda x:(x['status']=='CONFIRMED', x['today_vol']), reverse=True)
    hv_cache = alerts; hv_cache_time = time.time()

    return jsonify({'alerts':alerts,'count':len(alerts),
                    'confirmed':sum(1 for a in alerts if a['status']=='CONFIRMED'),
                    'on_track':sum(1 for a in alerts if a['status']=='ON_TRACK'),
                    'market_hours':True,'pct_day_done':pct_day,
                    'timestamp':ist_now.strftime('%d-%b-%Y %H:%M IST')})

@app.route('/hv-summary')
def get_hv_summary():
    conn = get_db()
    if not conn:
        json_path = os.path.join(BASE_DIR,'summary.json')
        if os.path.exists(json_path):
            with open(json_path) as f: data = json.load(f)
            return jsonify({'stocks':data,'source':'json'})
        return jsonify({'error':'DB not found'}), 404
    c = conn.cursor()
    c.execute('''SELECT symbol,hv_date,hv_volume,hv_high,hv_low,hv_close,
                        latest_date,latest_close,latest_volume,total_days
                 FROM hv_summary WHERE hv_date IS NOT NULL AND latest_close > 0''')
    rows = c.fetchall(); conn.close()
    stocks = [{'s':r['symbol'],'mvd':r['hv_date'],'mv':r['hv_volume'] or 0,
               'hh':r['hv_high'] or 0,'hl':r['hv_low'] or 0,'hc':r['hv_close'] or 0,
               'lc':r['latest_close'] or 0,'ld':r['latest_date'] or '',
               'lv':r['latest_volume'] or 0,'td':r['total_days'] or 0} for r in rows]
    return jsonify({'stocks':stocks,'count':len(stocks),
                    'timestamp':datetime.now().strftime('%d-%b-%Y %H:%M:%S'),'source':'db'})

@app.route('/health')
def health():
    db_ok = os.path.exists(DB_PATH); stock_count = 0
    if db_ok:
        try:
            conn=get_db(); c=conn.cursor()
            c.execute('SELECT COUNT(*) FROM hv_summary')
            stock_count=c.fetchone()[0]; conn.close()
        except: pass
    ist = get_ist_time()
    return jsonify({'status':'ok','db':'ready' if db_ok else 'not initialized',
                    'stocks_in_db':stock_count,'price_cached':len(price_cache),
                    'market_open':is_market_open(),
                    'ist_time':ist.strftime('%H:%M IST'),
                    'pct_day_done':pct_market_day_done(),
                    'time':datetime.now().strftime('%d-%b-%Y %H:%M:%S')})

@app.route('/')
def index():
    return jsonify({'service':'NSE Price + HV Alert + Camarilla API',
                    'endpoints':{'/price/RELIANCE':'single price',
                                 '/prices?symbols=TCS,INFY':'batch prices',
                                 '/new-hv':'stocks hitting new 5yr HV today',
                                 '/hv-summary':'full HV summary from DB',
                                 '/camarilla':'camarilla weekly watchlist',
                                 '/camarilla/triggers':'today entry triggers',
                                 '/health':'status'}})

# ── EMA Status endpoint ──────────────────────────────────────────
@app.route('/ema-status')
def get_ema_status():
    conn = get_db()
    if not conn:
        return jsonify({'stocks':[],'error':'DB not initialized'})
    c = conn.cursor()
    try:
        c.execute('''SELECT symbol, check_time, close, ema21, ema50, ema100, ema200,
                            above_count, entry_signal, ema_aligned, candle_time
                     FROM ema_status
                     ORDER BY entry_signal DESC, above_count DESC''')
        rows = c.fetchall()
        stocks = [{
            'symbol':       r['symbol'],
            'check_time':   r['check_time'],
            'close':        r['close'],
            'ema21':        r['ema21'],
            'ema50':        r['ema50'],
            'ema100':       r['ema100'],
            'ema200':       r['ema200'],
            'above_count':  r['above_count'],
            'entry_signal': bool(r['entry_signal']),
            'ema_aligned':  bool(r['ema_aligned']),
            'candle_time':  r['candle_time'],
        } for r in rows]
        conn.close()
        ist = get_ist_time()
        return jsonify({
            'stocks':       stocks,
            'count':        len(stocks),
            'entry_ready':  sum(1 for s in stocks if s['entry_signal']),
            'timestamp':    ist.strftime('%d-%b-%Y %H:%M IST'),
            'market_open':  is_market_open(),
        })
    except Exception as e:
        conn.close()
        return jsonify({'stocks':[],'error':str(e),'note':'Run EMA monitor first'})

# ── HV Watchlist endpoints ───────────────────────────────────────
@app.route('/watchlist', methods=['GET'])
def get_watchlist():
    conn = get_db()
    if not conn: return jsonify({'stocks':[]})
    c = conn.cursor()
    try:
        c.execute('''CREATE TABLE IF NOT EXISTS watchlist (
            symbol TEXT PRIMARY KEY, hv_low REAL, hv_high REAL,
            hv_date TEXT, score INTEGER, grade TEXT,
            stop_loss REAL, added TEXT)''')
        c.execute('SELECT * FROM watchlist ORDER BY added DESC')
        rows    = c.fetchall()
        stocks  = [dict(r) for r in rows]
        conn.close()
        return jsonify({'stocks': stocks, 'count': len(stocks)})
    except Exception as e:
        conn.close()
        return jsonify({'stocks':[], 'error': str(e)})

@app.route('/watchlist/add', methods=['POST'])
def add_watchlist():
    data = request.get_json()
    if not data or not data.get('symbol'):
        return jsonify({'error': 'symbol required'}), 400
    conn = get_db()
    if not conn: return jsonify({'error': 'DB not ready'}), 500
    c = conn.cursor()
    try:
        c.execute('''CREATE TABLE IF NOT EXISTS watchlist (
            symbol TEXT PRIMARY KEY, hv_low REAL, hv_high REAL,
            hv_date TEXT, score INTEGER, grade TEXT,
            stop_loss REAL, added TEXT)''')
        c.execute('''INSERT OR REPLACE INTO watchlist
            (symbol, hv_low, hv_high, hv_date, score, grade, stop_loss, added)
            VALUES (?,?,?,?,?,?,?,?)''',
            (data['symbol'], data.get('hv_low',0), data.get('hv_high',0),
             data.get('hv_date',''), data.get('score',0), data.get('grade',''),
             data.get('stop_loss',0), data.get('added', str(date.today()))))
        conn.commit(); conn.close()
        return jsonify({'ok': True, 'symbol': data['symbol']})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/watchlist/remove/<sym>', methods=['DELETE', 'GET'])
def remove_watchlist(sym):
    conn = get_db()
    if not conn: return jsonify({'error': 'DB not ready'}), 500
    c = conn.cursor()
    try:
        c.execute('DELETE FROM watchlist WHERE symbol=?', (sym.upper(),))
        conn.commit(); conn.close()
        return jsonify({'ok': True, 'symbol': sym.upper()})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500

# ── Camarilla endpoints ──────────────────────────────────────────
@app.route('/camarilla')
def get_camarilla():
    """Full Camarilla watchlist with live price vs levels."""
    conn = get_db()
    if not conn:
        return jsonify({'error': 'DB not ready'}), 500

    c          = conn.cursor()
    today      = str(date.today())
    d          = date.today()
    week_start = (d - timedelta(days=d.weekday())).strftime('%Y-%m-%d')

    # Get watchlist for current week
    c.execute('''
        SELECT w.symbol, w.direction, w.status,
               w.h3, w.h4, w.h5, w.l3, w.l4, w.l5,
               w.prev_close, w.ema20w, w.added_date,
               p.close  AS latest_close,
               p.high   AS today_high,
               p.low    AS today_low,
               p.volume AS today_vol
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

    watchlist = []
    for r in rows:
        sym       = r['symbol']
        direction = r['direction']
        close     = r['latest_close'] or r['prev_close'] or 0

        l3 = r['l3']; l4 = r['l4']; l5 = r['l5']
        h3 = r['h3']; h4 = r['h4']; h5 = r['h5']

        if direction == 'BULLISH':
            entry    = l3; sl_level = l4
            t1_level = h3; t2_level = h4
        else:
            entry    = h3; sl_level = h4
            t1_level = l3; t2_level = l4

        dist_pct = round(abs(close - entry) / close * 100, 2) if close else 0
        sl_pct   = round(abs(entry - sl_level) / entry * 100, 1) if entry else 0

        watchlist.append({
            'symbol':     sym,
            'direction':  direction,
            'status':     r['status'],
            'close':      round(close, 2),
            'today_high': round(r['today_high'],  2) if r['today_high']  else 0,
            'today_low':  round(r['today_low'],   2) if r['today_low']   else 0,
            'today_vol':  r['today_vol'] or 0,
            'l3':         round(l3, 2),
            'l4':         round(l4, 2),
            'l5':         round(l5, 2),
            'h3':         round(h3, 2),
            'h4':         round(h4, 2),
            'h5':         round(h5, 2),
            'entry':      round(entry,    2),
            'stop_loss':  round(sl_level, 2),
            'target1':    round(t1_level, 2),
            'target2':    round(t2_level, 2),
            'sl_pct':     sl_pct,
            'dist_pct':   dist_pct,
            'ema20w':     round(r['ema20w'], 2) if r['ema20w'] else 0,
            'added_date': r['added_date'],
        })

    conn.close()

    bullish   = [s for s in watchlist if s['direction'] == 'BULLISH']
    bearish   = [s for s in watchlist if s['direction'] == 'BEARISH']
    watching  = [s for s in watchlist if s['status'] == 'WATCHING']
    triggered = [s for s in watchlist if s['status'] == 'TRIGGERED']

    return jsonify({
        'week_start': week_start,
        'today':      today,
        'watchlist':  watchlist,
        'bullish':    bullish,
        'bearish':    bearish,
        'triggers':   triggers,
        'counts': {
            'total':          len(watchlist),
            'bullish':        len(bullish),
            'bearish':        len(bearish),
            'watching':       len(watching),
            'triggered':      len(triggered),
            'triggers_today': len(triggers),
        },
        'timestamp': get_ist_time().strftime('%d-%b-%Y %H:%M IST'),
    })


@app.route('/camarilla/triggers')
def get_camarilla_triggers():
    """Today's fired entry signals only."""
    conn = get_db()
    if not conn:
        return jsonify({'error': 'DB not ready'}), 500
    c     = conn.cursor()
    today = str(date.today())
    c.execute('''
        SELECT symbol, direction, trigger_type,
               entry_price, stop_loss, target1, target2,
               risk_reward, trigger_date, trigger_time
        FROM   camarilla_triggers
        WHERE  trigger_date = ?
        ORDER  BY trigger_time DESC
    ''', (today,))
    triggers = [dict(t) for t in c.fetchall()]
    conn.close()
    return jsonify({
        'triggers':  triggers,
        'count':     len(triggers),
        'date':      today,
        'timestamp': get_ist_time().strftime('%d-%b-%Y %H:%M IST'),
    })


@app.route('/camarilla/levels/<sym>')
def get_camarilla_levels(sym):
    """Full Camarilla levels for a single symbol this week."""
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
        return jsonify({'error': f'No levels found for {sym.upper()} this week'}), 404
    return jsonify(dict(row))


if __name__ == '__main__':
    print("NSE Price + HV Alert + Camarilla API on port 8081")
    app.run(host='0.0.0.0', port=8081, threaded=True)
