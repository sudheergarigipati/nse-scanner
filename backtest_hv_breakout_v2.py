"""
HV HIGH BREAKOUT BACKTEST V2 — Proper Version
Fixed parameters based on data analysis:
  - HV window: 7-180 days
  - Entry within 3% of HV High
  - Volume >= 5x on breakout candle
  - Resistance tested 10+ times
  - Fundamental score >= 50 (from yfinance)
  - Capital: Rs 60,000 | 3 positions | Rs 1,000 risk
  - Cooldown: 7 days
"""
import sqlite3, json, time, yfinance as yf
from datetime import datetime, timedelta
from collections import defaultdict

print("="*70, flush=True)
print("  HV HIGH BREAKOUT BACKTEST V2", flush=True)
print("  180d HV window | 5x volume | 10+ tests | Fundamentals", flush=True)
print("="*70, flush=True)

conn_d  = sqlite3.connect('/home/ubuntu/nse-scanner/nse_data.db')
conn_15 = sqlite3.connect('/home/ubuntu/nse-scanner/intraday_15min.db')
conn_d.row_factory  = sqlite3.Row
conn_15.row_factory = sqlite3.Row
cd  = conn_d.cursor()
c15 = conn_15.cursor()

bl = json.load(open('cautionary_stocks.json')).get('stocks',[])
cd.execute('SELECT symbol FROM daily_prices GROUP BY symbol HAVING COUNT(*) >= 252')
valid_symbols = {r['symbol'] for r in cd.fetchall()}

c15.execute("SELECT DISTINCT DATE(datetime) as dt FROM prices_15min ORDER BY dt")
available_days = [r['dt'] for r in c15.fetchall()]

print(f"Available days: {len(available_days)}", flush=True)
print(f"Date range    : {available_days[0]} to {available_days[-1]}", flush=True)

# ── Step 1: Fetch fundamentals for all stocks ─────────────────
print(f"\nFetching fundamental scores...", flush=True)
print(f"(This takes ~15 mins for all stocks)", flush=True)

def get_fundamental_score(sym):
    """Score 0-100 based on yfinance data"""
    try:
        t    = yf.Ticker(sym + '.NS')
        info = t.info
        if not info or 'symbol' not in str(info): return 0, {}

        score = 0
        data  = {}

        # Revenue growth (0-20 pts)
        rev_gr = info.get('revenueGrowth') or 0
        data['rev_gr'] = round(rev_gr*100, 1)
        if rev_gr >= 0.20: score += 20
        elif rev_gr >= 0.10: score += 10
        elif rev_gr >= 0.05: score += 5

        # Earnings growth (0-20 pts)
        earn_gr = info.get('earningsGrowth') or 0
        data['earn_gr'] = round(earn_gr*100, 1)
        if earn_gr >= 0.25: score += 20
        elif earn_gr >= 0.15: score += 10
        elif earn_gr >= 0.05: score += 5

        # ROE (0-15 pts)
        roe = info.get('returnOnEquity') or 0
        data['roe'] = round(roe*100, 1)
        if roe >= 0.20: score += 15
        elif roe >= 0.12: score += 8
        elif roe >= 0.06: score += 3

        # Profit margin (0-15 pts)
        margin = info.get('profitMargins') or 0
        data['margin'] = round(margin*100, 1)
        if margin >= 0.15: score += 15
        elif margin >= 0.08: score += 8
        elif margin >= 0.04: score += 3

        # Analyst recommendation (0-15 pts)
        rec = info.get('recommendationKey') or ''
        data['rec'] = rec
        if 'buy' in rec.lower() or 'outperform' in rec.lower():
            score += 15
        elif 'hold' in rec.lower() or 'neutral' in rec.lower():
            score += 5

        # Analyst target vs current (0-15 pts)
        target_mean = info.get('targetMeanPrice') or 0
        current     = info.get('currentPrice') or info.get('regularMarketPrice') or 0
        data['target'] = target_mean
        data['current'] = current
        if target_mean > 0 and current > 0:
            upside = (target_mean - current) / current
            data['upside'] = round(upside*100, 1)
            if upside >= 0.30: score += 15
            elif upside >= 0.20: score += 10
            elif upside >= 0.10: score += 5

        # Debt/Equity penalty (-10 pts if very high)
        de = info.get('debtToEquity') or 0
        data['de'] = round(de, 1)
        if de > 100: score -= 10
        elif de > 50: score -= 5

        return max(0, score), data

    except Exception as e:
        return 0, {}

# Load or build fundamental scores
import os, pickle
scores_file = '/home/ubuntu/nse-scanner/fundamental_scores.pkl'

if os.path.exists(scores_file):
    print(f"Loading cached fundamental scores...", flush=True)
    with open(scores_file, 'rb') as f:
        fund_scores = pickle.load(f)
    print(f"Loaded {len(fund_scores)} scores", flush=True)
else:
    print(f"Building fundamental scores for {len(valid_symbols)} stocks...", flush=True)
    fund_scores = {}
    syms_list   = sorted(valid_symbols)
    for i, sym in enumerate(syms_list):
        if sym in bl: continue
        score, data = get_fundamental_score(sym)
        fund_scores[sym] = {'score': score, 'data': data}
        if i % 50 == 0:
            print(f"  [{i}/{len(syms_list)}] {sym}: score={score}", flush=True)
        time.sleep(0.3)

    with open(scores_file, 'wb') as f:
        pickle.dump(fund_scores, f)
    print(f"Saved {len(fund_scores)} scores", flush=True)

# Score distribution
score_dist = defaultdict(int)
for sym, d in fund_scores.items():
    bucket = (d['score']//10)*10
    score_dist[bucket] += 1

print(f"\nFundamental score distribution:", flush=True)
for bucket in sorted(score_dist.keys(), reverse=True):
    print(f"  Score {bucket:>3}-{bucket+9}: {score_dist[bucket]:>4} stocks", flush=True)

strong_stocks = {s for s,d in fund_scores.items() if d['score'] >= 50}
print(f"\nStrong fundamental stocks (score>=50): {len(strong_stocks)}", flush=True)

# ── Step 2: Run backtest ──────────────────────────────────────
print(f"\n{'='*70}", flush=True)
print(f"Running HV Breakout V2 backtest...", flush=True)

CAPITAL     = 60000
MAX_POS     = 3
RISK        = 1000
MAX_POS_VAL = 20000
COOLDOWN    = 7
HV_WIN_MAX  = 180
HV_WIN_MIN  = 7
MIN_VOL_R   = 5.0     # 5x volume on breakout
MIN_TESTS   = 10      # 10+ resistance tests
MAX_ENTRY_PCT = 3.0   # entry within 3% of HV High
SL_MULT     = 0.5
TGT_MULT    = 1.5
MIN_FUND_SCORE = 50   # fundamental filter

open_positions = {}
capital        = CAPITAL
trade_log      = []
cooldown_map   = {}
monthly_stats  = defaultdict(lambda:{
    'signals':0,'qual_signals':0,'filled':0,
    'trades':0,'wins':0,'sl':0,'open':0,'pnl':0.0
})

skip_reasons = defaultdict(int)
start_time   = datetime.now()

for day_idx, today in enumerate(available_days):
    if day_idx == 0: continue
    today_dt = datetime.strptime(today, '%Y-%m-%d')
    month    = today[:7]

    if day_idx % 10 == 0:
        elapsed = (datetime.now()-start_time).seconds
        print(f"  Day {day_idx}/{len(available_days)}: {today} | "
              f"Capital: Rs{capital:,.0f} | "
              f"Open: {len(open_positions)} | "
              f"Trades: {len(trade_log)} | "
              f"Time: {elapsed}s", flush=True)
        time.sleep(0.05)

    # Load 15-min candles
    c15.execute('''
        SELECT symbol, datetime, open, high, low, close, volume
        FROM prices_15min WHERE DATE(datetime)=?
        ORDER BY symbol, datetime
    ''', (today,))
    all_today = c15.fetchall()
    sym_candles = defaultdict(list)
    for bar in all_today:
        sym_candles[bar['symbol']].append(bar)

    # ── Monitor open positions ────────────────────────────────
    to_close = []
    for sym, pos in list(open_positions.items()):
        candles = sym_candles.get(sym, [])
        for bar in candles:
            if bar['low'] <= pos['sl']:
                pnl = (pos['sl']-pos['entry'])*pos['shares']
                capital += pos['shares']*pos['entry']+pnl
                monthly_stats[pos['entry_dt'][:7]]['sl']     += 1
                monthly_stats[pos['entry_dt'][:7]]['pnl']    += pnl
                monthly_stats[pos['entry_dt'][:7]]['trades'] += 1
                cooldown_map[sym] = today
                to_close.append(sym)
                trade_log.append({
                    'sym'      : sym,
                    'entry_dt' : pos['entry_dt'],
                    'exit_dt'  : bar['datetime'][:16],
                    'entry'    : pos['entry'],
                    'exit'     : pos['sl'],
                    'result'   : 'SL',
                    'pnl'      : round(pnl,0),
                    'shares'   : pos['shares'],
                    'hv_high'  : pos['hv_high'],
                    'hv_range' : pos['hv_range'],
                    'hv_age'   : pos['hv_age'],
                    'vol_r'    : pos['vol_r'],
                    'tests'    : pos['tests'],
                    'fund_score': pos['fund_score'],
                    'month'    : pos['entry_dt'][:7],
                    'days_held': pos.get('days_held',0),
                })
                break
            elif bar['high'] >= pos['target']:
                pnl = (pos['target']-pos['entry'])*pos['shares']
                capital += pos['shares']*pos['entry']+pnl
                monthly_stats[pos['entry_dt'][:7]]['wins']   += 1
                monthly_stats[pos['entry_dt'][:7]]['pnl']    += pnl
                monthly_stats[pos['entry_dt'][:7]]['trades'] += 1
                cooldown_map[sym] = today
                to_close.append(sym)
                trade_log.append({
                    'sym'      : sym,
                    'entry_dt' : pos['entry_dt'],
                    'exit_dt'  : bar['datetime'][:16],
                    'entry'    : pos['entry'],
                    'exit'     : pos['target'],
                    'result'   : 'WIN',
                    'pnl'      : round(pnl,0),
                    'shares'   : pos['shares'],
                    'hv_high'  : pos['hv_high'],
                    'hv_range' : pos['hv_range'],
                    'hv_age'   : pos['hv_age'],
                    'vol_r'    : pos['vol_r'],
                    'tests'    : pos['tests'],
                    'fund_score': pos['fund_score'],
                    'month'    : pos['entry_dt'][:7],
                    'days_held': pos.get('days_held',0),
                })
                break

    for sym in to_close:
        if sym in open_positions: del open_positions[sym]
    for sym in open_positions:
        open_positions[sym]['days_held'] = \
            open_positions[sym].get('days_held',0)+1

    # ── Find breakout signals ─────────────────────────────────
    date_min = (today_dt-timedelta(days=HV_WIN_MAX)).strftime('%Y-%m-%d')
    date_max = (today_dt-timedelta(days=HV_WIN_MIN)).strftime('%Y-%m-%d')

    cd.execute('''
        SELECT d1.symbol,
               d1.high  as hv_high,
               d1.low   as hv_low,
               d1.volume as hv_vol,
               d1.date  as hv_date
        FROM daily_prices d1
        WHERE d1.date>=? AND d1.date<=?
        AND d1.close>=(d1.low+(d1.high-d1.low)*0.5)
        AND d1.volume=(SELECT MAX(volume) FROM daily_prices d2
            WHERE d2.symbol=d1.symbol AND d2.date>=? AND d2.date<=?)
        AND d1.low>0 AND d1.volume>0
    ''', (date_min, date_max, date_min, date_max))
    candidates = [r for r in cd.fetchall()
                  if r['symbol'] in valid_symbols
                  and r['symbol'] not in bl]

    signals_today = set()

    for row in candidates:
        sym      = row['symbol']
        hv_high  = row['hv_high']
        hv_low   = row['hv_low']
        hv_vol   = row['hv_vol']
        hv_date  = row['hv_date']
        hv_range = hv_high - hv_low
        hv_age   = (today_dt-datetime.strptime(hv_date,'%Y-%m-%d')).days

        if hv_range <= 0: continue
        if sym in open_positions: continue
        if sym in signals_today: continue
        if len(open_positions) >= MAX_POS: continue

        monthly_stats[month]['signals'] += 1

        # Cooldown
        if sym in cooldown_map:
            last_exit = datetime.strptime(cooldown_map[sym],'%Y-%m-%d')
            if (today_dt-last_exit).days < COOLDOWN:
                skip_reasons['cooldown'] += 1
                continue

        # Fundamental filter
        fund_score = fund_scores.get(sym, {}).get('score', 0)
        if fund_score < MIN_FUND_SCORE:
            skip_reasons['weak_fundamentals'] += 1
            continue

        # Avg volume
        cd.execute('''
            SELECT AVG(volume) as av FROM (
                SELECT volume FROM daily_prices
                WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 20
            )
        ''', (sym, today))
        avg_vol = cd.fetchone()['av'] or 0
        if avg_vol == 0: continue

        # Calculate levels
        sl     = round(hv_high - SL_MULT*hv_range, 2)
        target = round(hv_high + TGT_MULT*hv_range, 2)
        risk   = hv_high - sl
        if risk <= 0: continue

        shares = min(int(RISK/risk), int(MAX_POS_VAL/hv_high))
        if shares < 1: continue

        # Stock below HV High for 5+ days
        cd.execute('''
            SELECT COUNT(*) as cnt FROM daily_prices
            WHERE symbol=? AND date<? AND date>=?
            AND close < ?
        ''', (sym, today,
              (today_dt-timedelta(days=30)).strftime('%Y-%m-%d'),
              hv_high))
        below_count = cd.fetchone()['cnt']
        if below_count < 5:
            skip_reasons['not_below_long_enough'] += 1
            continue

        # Resistance tests
        cd.execute('''
            SELECT COUNT(*) as tests FROM daily_prices
            WHERE symbol=? AND date<? AND date>=?
            AND high >= ? AND close < ?
        ''', (sym, today,
              (today_dt-timedelta(days=180)).strftime('%Y-%m-%d'),
              hv_high*0.98, hv_high))
        tests = cd.fetchone()['tests']
        if tests < MIN_TESTS:
            skip_reasons['not_enough_tests'] += 1
            continue

        monthly_stats[month]['qual_signals'] += 1

        # 15-min breakout detection
        candles = sym_candles.get(sym, [])
        if len(candles) < 2: continue

        for ci, bar in enumerate(candles[:-1]):
            # Breakout candle: close > HV High with 5x volume
            candle_avg_vol = avg_vol/26
            candle_vol_r   = round(bar['volume']/candle_avg_vol, 1) \
                             if candle_avg_vol > 0 else 0

            if bar['close'] <= hv_high: continue
            if candle_vol_r < MIN_VOL_R: continue

            # Entry: next candle open
            next_bar  = candles[ci+1]
            next_open = next_bar['open']

            # Entry must be within 3% of HV High
            entry_pct = (next_open-hv_high)/hv_high*100
            if entry_pct < 0: continue           # below HV High
            if entry_pct > MAX_ENTRY_PCT: continue # too far above

            if next_open >= target: continue
            if shares*next_open > capital: continue

            # ✅ Valid entry!
            actual_risk   = next_open - sl
            if actual_risk <= 0: continue
            actual_shares = min(int(RISK/actual_risk),
                               int(MAX_POS_VAL/next_open))
            if actual_shares < 1: continue

            capital -= actual_shares*next_open
            open_positions[sym] = {
                'entry'     : next_open,
                'sl'        : sl,
                'target'    : target,
                'shares'    : actual_shares,
                'entry_dt'  : next_bar['datetime'][:16],
                'hv_high'   : hv_high,
                'hv_range'  : round(hv_range,2),
                'hv_age'    : hv_age,
                'vol_r'     : candle_vol_r,
                'tests'     : tests,
                'fund_score': fund_score,
                'days_held' : 0,
            }
            signals_today.add(sym)
            monthly_stats[month]['filled'] += 1

            # Check rest of today
            for future_bar in candles[ci+2:]:
                if sym not in open_positions: break
                if future_bar['low'] <= sl:
                    pnl = (sl-next_open)*actual_shares
                    capital += actual_shares*next_open+pnl
                    monthly_stats[month]['sl']     += 1
                    monthly_stats[month]['pnl']    += pnl
                    monthly_stats[month]['trades'] += 1
                    cooldown_map[sym] = today
                    del open_positions[sym]
                    trade_log.append({
                        'sym':sym,'entry_dt':next_bar['datetime'][:16],
                        'exit_dt':future_bar['datetime'][:16],
                        'entry':next_open,'exit':sl,'result':'SL',
                        'pnl':round(pnl,0),'shares':actual_shares,
                        'hv_high':hv_high,'hv_range':round(hv_range,2),
                        'hv_age':hv_age,'vol_r':candle_vol_r,
                        'tests':tests,'fund_score':fund_score,
                        'month':month,'days_held':0,
                    })
                    break
                elif future_bar['high'] >= target:
                    pnl = (target-next_open)*actual_shares
                    capital += actual_shares*next_open+pnl
                    monthly_stats[month]['wins']   += 1
                    monthly_stats[month]['pnl']    += pnl
                    monthly_stats[month]['trades'] += 1
                    cooldown_map[sym] = today
                    del open_positions[sym]
                    trade_log.append({
                        'sym':sym,'entry_dt':next_bar['datetime'][:16],
                        'exit_dt':future_bar['datetime'][:16],
                        'entry':next_open,'exit':target,'result':'WIN',
                        'pnl':round(pnl,0),'shares':actual_shares,
                        'hv_high':hv_high,'hv_range':round(hv_range,2),
                        'hv_age':hv_age,'vol_r':candle_vol_r,
                        'tests':tests,'fund_score':fund_score,
                        'month':month,'days_held':0,
                    })
                    break
            break

# ── Close remaining ───────────────────────────────────────────
print(f"\nClosing remaining open positions...", flush=True)
for sym, pos in open_positions.items():
    cd.execute('SELECT close FROM daily_prices WHERE symbol=? ORDER BY date DESC LIMIT 1',(sym,))
    r = cd.fetchone()
    if r:
        pnl = (r['close']-pos['entry'])*pos['shares']
        monthly_stats[available_days[-1][:7]]['open'] += 1
        monthly_stats[available_days[-1][:7]]['pnl']  += pnl
        trade_log.append({
            'sym':sym,'entry_dt':pos['entry_dt'],'exit_dt':'OPEN',
            'entry':pos['entry'],'exit':r['close'],'result':'OPEN',
            'pnl':round(pnl,0),'shares':pos['shares'],
            'hv_high':pos['hv_high'],'hv_range':pos['hv_range'],
            'hv_age':pos['hv_age'],'vol_r':pos['vol_r'],
            'tests':pos['tests'],'fund_score':pos['fund_score'],
            'month':pos['entry_dt'][:7],'days_held':pos.get('days_held',0),
        })

# ── Results ───────────────────────────────────────────────────
print(f"\n{'='*70}", flush=True)
print(f"  HV HIGH BREAKOUT V2 RESULTS", flush=True)
print(f"{'='*70}", flush=True)

print(f"\n  SKIP REASONS:", flush=True)
for reason, cnt in sorted(skip_reasons.items(), key=lambda x: -x[1]):
    print(f"  {reason:<30}: {cnt:>6}", flush=True)

print(f"\n  FUNDAMENTAL SCORE BREAKDOWN:", flush=True)
for sym, d in sorted(fund_scores.items(), key=lambda x: -x[1]['score'])[:20]:
    if d['score'] >= 50:
        print(f"  {sym:<15}: {d['score']:>3} | "
              f"Rev:{d['data'].get('rev_gr',0):>5.1f}% "
              f"Earn:{d['data'].get('earn_gr',0):>5.1f}% "
              f"ROE:{d['data'].get('roe',0):>5.1f}% "
              f"Margin:{d['data'].get('margin',0):>5.1f}% "
              f"Rec:{d['data'].get('rec','?'):<10} "
              f"Upside:{d['data'].get('upside',0):>5.1f}%", flush=True)

total_t=total_w=total_sl=total_o=0
total_pnl=0.0
total_sig=total_qual=total_fill=0

print(f"\n  {'Month':<10} {'Sig':>6} {'Qual':>6} {'Fill':>5} {'Trades':>7} {'Wins':>6} {'SL':>5} {'WR%':>6} {'P&L':>12}", flush=True)
print(f"  {'-'*70}", flush=True)
for month in sorted(monthly_stats.keys()):
    m  = monthly_stats[month]
    wr = round(m['wins']/m['trades']*100,1) if m['trades']>0 else 0
    total_t  +=m['trades']; total_w+=m['wins']
    total_sl +=m['sl'];     total_o+=m['open']
    total_pnl+=m['pnl']
    total_sig+=m['signals']; total_qual+=m['qual_signals']
    total_fill+=m['filled']
    print(f"  {month:<10} {m['signals']:>6} {m['qual_signals']:>6} "
          f"{m['filled']:>5} {m['trades']:>7} {m['wins']:>6} "
          f"{m['sl']:>5} {wr:>5}% {m['pnl']:>+12,.0f}", flush=True)

print(f"  {'-'*70}", flush=True)
wr_t = round(total_w/total_t*100,1) if total_t>0 else 0
print(f"  {'TOTAL':<10} {total_sig:>6} {total_qual:>6} "
      f"{total_fill:>5} {total_t:>7} {total_w:>6} "
      f"{total_sl:>5} {wr_t:>5}% {total_pnl:>+12,.0f}", flush=True)

wins_l = [t for t in trade_log if t['result']=='WIN']
sl_l   = [t for t in trade_log if t['result']=='SL']
avg_w  = round(sum(t['pnl'] for t in wins_l)/len(wins_l),0) if wins_l else 0
avg_l  = round(sum(t['pnl'] for t in sl_l)/len(sl_l),0) if sl_l else 0
wl_r   = round(abs(avg_w/avg_l),1) if avg_l and avg_w else 0

print(f"\n  KEY STATS:", flush=True)
print(f"  Signals total  : {total_sig}", flush=True)
print(f"  Qualified      : {total_qual} (after fund+tests+vol filter)", flush=True)
print(f"  Filled         : {total_fill} (15-min entry within 3%)", flush=True)
print(f"  Trades closed  : {total_t}", flush=True)
print(f"  Win rate       : {wr_t}%", flush=True)
print(f"  Avg win        : Rs {avg_w:+,.0f}", flush=True)
print(f"  Avg loss       : Rs {avg_l:+,.0f}", flush=True)
print(f"  Win:Loss ratio : {wl_r}:1", flush=True)
print(f"  Total P&L      : Rs {total_pnl:+,.0f}", flush=True)
print(f"  Final capital  : Rs {capital:,.0f}", flush=True)

# HV Age analysis
print(f"\n  HV AGE ANALYSIS:", flush=True)
for a_min,a_max,label in [(0,60,'7-60d'),(60,120,'60-120d'),(120,181,'120-180d')]:
    b=[t for t in trade_log if a_min<=t['hv_age']<a_max and t['result'] in ['WIN','SL']]
    if not b: continue
    w=[t for t in b if t['result']=='WIN']
    wr=round(len(w)/len(b)*100,1)
    avg_p=round(sum(t['pnl'] for t in b)/len(b),0)
    print(f"  HV {label:<10}: Trades={len(b):>4} WR={wr:>5}% AvgPnL=Rs{avg_p:>+7,.0f}", flush=True)

# Fundamental score analysis
print(f"\n  FUNDAMENTAL SCORE ANALYSIS:", flush=True)
for s_min,s_max,label in [(50,65,'50-64'),(65,80,'65-79'),(80,101,'80-100')]:
    b=[t for t in trade_log if s_min<=t['fund_score']<s_max and t['result'] in ['WIN','SL']]
    if not b: continue
    w=[t for t in b if t['result']=='WIN']
    wr=round(len(w)/len(b)*100,1)
    avg_p=round(sum(t['pnl'] for t in b)/len(b),0)
    print(f"  Score {label:<8}: Trades={len(b):>4} WR={wr:>5}% AvgPnL=Rs{avg_p:>+7,.0f}", flush=True)

# Days held
print(f"\n  DAYS HELD:", flush=True)
for d_min,d_max,label in [(0,1,'Same day'),(1,2,'Day 1'),(2,3,'Day 2'),(3,4,'Day 3'),(4,99,'Day 4+')]:
    b=[t for t in trade_log if d_min<=t['days_held']<d_max and t['result'] in ['WIN','SL']]
    if not b: continue
    w=[t for t in b if t['result']=='WIN']
    wr=round(len(w)/len(b)*100,1)
    avg_p=round(sum(t['pnl'] for t in b)/len(b),0)
    print(f"  {label:<12}: Trades={len(b):>4} WR={wr:>5}% AvgPnL=Rs{avg_p:>+7,.0f}", flush=True)

# Trade log
print(f"\n  TRADE LOG:", flush=True)
print(f"  {'Stock':<12} {'Entry DT':<18} {'Exit DT':<18} {'Entry':>8} {'Exit':>8} "
      f"{'HVHigh':>8} {'Age':>5} {'Vol':>6} {'Tests':>6} {'Fund':>5} {'Days':>5} {'Result':<10} {'P&L':>8}", flush=True)
print(f"  {'-'*125}", flush=True)
for t in sorted(trade_log, key=lambda x: x['entry_dt']):
    icon='✅' if t['result']=='WIN' else '❌' if t['result']=='SL' else '⏳'
    print(f"  {t['sym']:<12} {t['entry_dt']:<18} {t['exit_dt']:<18} "
          f"{t['entry']:>8.2f} {t['exit']:>8.2f} "
          f"{t['hv_high']:>8.2f} {t['hv_age']:>4}d "
          f"{t['vol_r']:>5.1f}x {t['tests']:>6} {t['fund_score']:>5} "
          f"{t['days_held']:>5} {icon}{t['result']:<9} "
          f"Rs{t['pnl']:>+7,.0f}", flush=True)

print(f"\n  FINAL COMPARISON:", flush=True)
print(f"  {'Strategy':<45} {'Trades':>7} {'WR%':>6} {'P&L':>12}", flush=True)
print(f"  {'-'*75}", flush=True)
print(f"  {'HV Low Bounce (Approach B)':<45} {'49':>7} {'57.1%':>6} {'Rs +28,249':>12}", flush=True)
print(f"  {'HV Breakout V1 (no fundamentals)':<45} {'14':>7} {'35.7%':>6} {'Rs  +1,057':>12}", flush=True)
print(f"  {'HV Breakout V2 (with fundamentals)':<45} {total_t:>7} {wr_t:>5}% {total_pnl:>+12,.0f}", flush=True)

elapsed=(datetime.now()-start_time).seconds
print(f"\n  Total time: {elapsed}s", flush=True)
conn_d.close()
conn_15.close()
print("\nDone!", flush=True)
