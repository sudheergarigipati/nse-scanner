import sqlite3, yfinance as yf, time, json
from datetime import datetime

# Create/connect 15-min database
conn15 = sqlite3.connect('/home/ubuntu/nse-scanner/intraday_15min.db')
c15 = conn15.cursor()

c15.execute('''
    CREATE TABLE IF NOT EXISTS prices_15min (
        symbol   TEXT,
        datetime TEXT,
        open     REAL,
        high     REAL,
        low      REAL,
        close    REAL,
        volume   INTEGER,
        PRIMARY KEY (symbol, datetime)
    )
''')
c15.execute('CREATE INDEX IF NOT EXISTS idx_sym_dt ON prices_15min(symbol, datetime)')
conn15.commit()

# Get all symbols from daily DB
conn_daily = sqlite3.connect('/home/ubuntu/nse-scanner/nse_data.db')
conn_daily.row_factory = sqlite3.Row
cd = conn_daily.cursor()
cd.execute('SELECT DISTINCT symbol FROM daily_prices ORDER BY symbol')
all_symbols = [r['symbol'] for r in cd.fetchall()]
conn_daily.close()

# Index symbols use different Yahoo format
INDEX_MAP = {
    'NIFTY50'  : '^NSEI',
    'BANKNIFTY': '^NSEBANK',
}

print(f"Total symbols to fetch: {len(all_symbols)}", flush=True)
print(f"Starting 15-min data fetch...", flush=True)
print(f"{'='*70}", flush=True)

success = 0
failed  = 0
skipped = 0

for i, sym in enumerate(all_symbols):
    # Check if already have data
    c15.execute('SELECT COUNT(*) as cnt FROM prices_15min WHERE symbol=?', (sym,))
    existing = c15.fetchone()[0]
    if existing > 0:
        skipped += 1
        continue

    # Get Yahoo symbol
    if sym in INDEX_MAP:
        yf_sym = INDEX_MAP[sym]
    else:
        yf_sym = sym + '.NS'

    try:
        ticker = yf.Ticker(yf_sym)
        df = ticker.history(period='60d', interval='15m', auto_adjust=True)

        if df is None or df.empty:
            print(f"  [{i+1}/{len(all_symbols)}] {sym:<15} ❌ No data", flush=True)
            failed += 1
            time.sleep(0.3)
            continue

        df = df.dropna()
        rows = 0
        for idx, row in df.iterrows():
            dt = str(idx)[:19]
            try:
                c15.execute('''
                    INSERT OR IGNORE INTO prices_15min
                    (symbol, datetime, open, high, low, close, volume)
                    VALUES (?,?,?,?,?,?,?)
                ''', (sym, dt,
                      round(float(row['Open']),2),
                      round(float(row['High']),2),
                      round(float(row['Low']),2),
                      round(float(row['Close']),2),
                      int(row['Volume'])))
                rows += 1
            except:
                pass

        conn15.commit()
        success += 1

        if i % 50 == 0 or rows == 0:
            print(f"  [{i+1}/{len(all_symbols)}] {sym:<15} {rows:>6} candles ✅", flush=True)

        time.sleep(0.3)

    except Exception as e:
        print(f"  [{i+1}/{len(all_symbols)}] {sym:<15} ❌ {str(e)[:50]}", flush=True)
        failed += 1
        time.sleep(0.5)

# Final summary
c15.execute('SELECT COUNT(*) as cnt FROM prices_15min')
total_candles = c15.fetchone()[0]
c15.execute('SELECT COUNT(DISTINCT symbol) as cnt FROM prices_15min')
total_syms = c15.fetchone()[0]
c15.execute('SELECT MIN(datetime) as mn, MAX(datetime) as mx FROM prices_15min')
r = c15.fetchone()

print(f"\n{'='*70}", flush=True)
print(f"FETCH COMPLETE!", flush=True)
print(f"  Success : {success}", flush=True)
print(f"  Failed  : {failed}", flush=True)
print(f"  Skipped : {skipped}", flush=True)
print(f"  Total candles: {total_candles:,}", flush=True)
print(f"  Total symbols: {total_syms}", flush=True)
print(f"  Date range   : {r[0]} to {r[1]}", flush=True)
print(f"  DB size      : /home/ubuntu/nse-scanner/intraday_15min.db", flush=True)

conn15.close()
print("Done!", flush=True)
