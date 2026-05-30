#!/bin/bash
# ═══════════════════════════════════════════════════
#  stop.sh — Stop NSE Scanner
#  Usage: bash stop.sh
# ═══════════════════════════════════════════════════

echo "======================================"
echo "  NSE Scanner — Shutting Down"
echo "======================================"

echo ""
echo "Stopping services..."

pkill -f price_api.py 2>/dev/null && echo "  ✅ Price API stopped" || echo "  ⚠️  Price API was not running"
pkill -f scheduler.py 2>/dev/null && echo "  ✅ Scheduler stopped"  || echo "  ⚠️  Scheduler was not running"
pkill -f serve.py     2>/dev/null && echo "  ✅ Web server stopped"  || echo "  ⚠️  Web server was not running"

sleep 1

# Confirm nothing left
RUNNING=$(ps aux | grep -E "price_api|scheduler|serve\.py" | grep -v grep | wc -l)
echo ""
if [ "$RUNNING" -eq 0 ]; then
  echo "  All services stopped cleanly."
else
  echo "  Warning: $RUNNING process(es) still running — force killing..."
  kill -9 $(ps aux | grep -E "price_api|scheduler|serve\.py" | grep -v grep | awk '{print $2}') 2>/dev/null
  echo "  Force killed."
fi

echo ""
echo "======================================"
echo "  NSE Scanner stopped."
echo "  You can now safely stop the VM"
echo "  from Oracle Console if needed."
echo "======================================"
