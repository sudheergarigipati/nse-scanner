#!/bin/bash
# ═══════════════════════════════════════════════════
#  start.sh — Start NSE Scanner + Angel One Trader
#  Usage: bash ~/nse-scanner/start.sh
# ═══════════════════════════════════════════════════
cd ~/nse-scanner
source venv/bin/activate

echo "======================================"
echo "  NSE Scanner — Starting Up"
echo "======================================"

# Step 1 — Update DB
echo ""
echo "[1/6] Updating database (fetching missed days)..."
python3 db_manager.py --update
echo "      Done!"

# Step 2 — Copy scanner HTML
echo ""
echo "[2/6] Restoring scanner HTML..."
cp ~/nse-scanner/NSE_Volume_Scanner_Ver1.0.html ~/nse-scanner/www/index.html
echo "      Done!"

# Step 3 — Kill old processes
echo ""
echo "[3/6] Stopping old processes..."
pkill -f price_api.py       2>/dev/null
pkill -f scheduler.py       2>/dev/null
pkill -f serve.py           2>/dev/null
pkill -f camarilla_scanner  2>/dev/null
pkill -f camarilla_monitor  2>/dev/null
pkill -f angel_auth         2>/dev/null
sleep 2

# Step 4 — Start services
echo ""
echo "[4/6] Starting services..."
nohup python3 price_api.py  > logs/price_api.log  2>&1 &
echo "      Price API started (PID $!)"
nohup python3 scheduler.py  > logs/scheduler.log  2>&1 &
echo "      Scheduler started (PID $!)"
nohup python3 serve.py      > logs/serve.log      2>&1 &
echo "      Web server started (PID $!)"
  nohup streamlit run dashboard.py --server.port 8502 --server.headless true > logs/dashboard.log 2>&1 &
      echo "      Dashboard started (PID $!)"
sleep 2

# Step 5 — Angel One Login
echo ""
echo "[5/6] Angel One login..."
python3 angel_auth.py
echo "      Angel One ready!"

# Step 6 — Notify Telegram
echo ""
echo "[6/6] Sending Telegram notification..."
python3 -c "
import urllib.request, urllib.parse, json
from datetime import datetime, timedelta
token    = '8788684553:AAHfZ_q0Hh2mdUNOwELu_PQPePpptKtixGM'
group_id = '-5282064943'
ist_time = (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime('%d %b %Y %H:%M IST')
msg = (
    'NSE Scanner is LIVE!\n'
    + ist_time + '\n\n'
    'HV SIGNAL SCANNER (AUTO TRADE)\n'
    'Angel One connected | Capital: Rs 60,000\n'
    'Max 3 positions | Risk: Rs 1,000/trade\n'
    'Strategy: HV + Daily Camarilla L3 entry\n'
    'Filters: HV 7-60d | Vol>=1.5x | RR 5-20x\n'
    'Target: HV High | SL: Daily L4\n'
    'Backtest: 48.5% WR | Rs 10,527/mo | 421% pa\n'
    'Loss limits: Daily Rs 3,000 | Monthly Rs 10,000\n'
    'Auto buy + GTT SL + GTT Target on fill\n\n'
    'CAMARILLA SCANNER\n'
    'Weekly levels | BankNifty + Nifty options\n\n'
    'Scanner: http://140.245.221.168:8080'
)
url  = 'https://api.telegram.org/bot' + token + '/sendMessage'
data = urllib.parse.urlencode({'chat_id':group_id,'text':msg}).encode()
req  = urllib.request.Request(url, data=data, method='POST')
try:
    r = urllib.request.urlopen(req, timeout=10)
    result = json.loads(r.read())
    print('      Telegram notified!' if result.get('ok') else '      Telegram failed')
except Exception as e:
    print(f'      Telegram error: {e}')
"

echo ""
echo "======================================"
echo "  All services running:"
ps aux | grep -E "price_api|scheduler|serve\.py" | grep -v grep | awk '{print "  ✅ "$11}'
echo ""
echo "  Scanner URL : http://140.245.221.168:8080"
echo "  Volume      : HV alerts + EMA monitor active"
echo "  Camarilla   : Weekly scan + Live monitor active"
echo "  Index Opts  : BankNifty + Nifty monitor active"
echo "  HV Signal   : Auto-trade via Angel One active"
  echo "  Dashboard   : http://140.245.221.168:8502 active"
echo "  Telegram    : Group notified"
echo "======================================"
