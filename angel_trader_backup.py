#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  angel_trader.py
#  Automated trader for HV signals via Angel One SmartAPI
#
#  Capital : Rs 50,000
#  Risk    : 2% per trade = Rs 1,000
#  Position: Rs 1,000 / 3% SL = Rs 33,333 per trade
#  Max open: 2 positions simultaneously
#  Type    : CNC (delivery, not intraday)
#
#  Flow:
#    1. Receive signal (symbol, entry, sl, target)
#    2. Check if max positions reached
#    3. Check daily loss limit
#    4. Calculate shares = Rs 33,333 / entry price
#    5. Place CNC LIMIT BUY at entry price
#    6. On fill confirmation → place SL order
#    7. Send Telegram confirmation
#    8. Log everything to DB
#
#  Usage:
#    from angel_trader import place_trade
#    place_trade('RAIN', 153.20, 148.60, 169.00)
# ═══════════════════════════════════════════════════════════════

import os
import json
import sqlite3
import time
import urllib.request
import urllib.parse
from datetime import datetime, date, timedelta
from angel_auth import get_smart_api

BASE_DIR    = os.path.expanduser('~/nse-scanner')
DB_PATH     = os.path.join(BASE_DIR, 'nse_data.db')
LOG_FILE    = os.path.join(BASE_DIR, 'logs/angel_trader.log')
TRADES_FILE = os.path.join(BASE_DIR, 'angel_trades.json')

TELEGRAM_TOKEN   = '8788684553:AAHfZ_q0Hh2mdUNOwELu_PQPePpptKtixGM'
TELEGRAM_CHAT_ID = '-5282064943'

# ── Trading config ─────────────────────────────────────────────
CAPITAL          = 50000   # Rs 50,000 total capital
RISK_PER_TRADE   = 750    # Rs 750 risk per trade (3% of Rs 25k)
MAX_POSITION_VAL = 25000   # Max Rs 25,000 per trade
MAX_OPEN_POS     = 2       # Max 2 open positions
DAILY_LOSS_LIMIT = 1500    # Stop trading if daily loss > Rs 1,500
SL_PCT           = 3.0     # 3% SL (matches backtest)
ORDER_TYPE       = 'CNC'   # Delivery (not intraday)

# ── Helpers ───────────────────────────────────────────────────
def log(msg):
    ts   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

def get_ist():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

def send_telegram(message):
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            'chat_id': TELEGRAM_CHAT_ID,
            'text':    message,
        }).encode()
        req  = urllib.request.Request(url, data=data, method='POST')
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            return result.get('ok', False)
    except Exception as e:
        log(f"Telegram error: {e}")
        return False

# ── Trade tracking ────────────────────────────────────────────
def load_trades():
    """Load today's trades from file."""
    today = str(date.today())
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE) as f:
                data = json.load(f)
            if data.get('date') == today:
                return data.get('trades', [])
        except:
            pass
    return []

def save_trades(trades):
    with open(TRADES_FILE, 'w') as f:
        json.dump({'date': str(date.today()), 'trades': trades}, f)

def get_open_positions():
    """Get currently open positions from Angel One."""
    try:
        smart_api, _ = get_smart_api()
        positions    = smart_api.position()
        if not positions['status']:
            return []
        open_pos = []
        for p in (positions.get('data') or []):
            if int(p.get('netqty', 0)) > 0:
                open_pos.append({
                    'symbol':   p['tradingsymbol'],
                    'qty':      int(p['netqty']),
                    'avg_price': float(p.get('netavgprice', 0)),
                    'ltp':      float(p.get('ltp', 0)),
                    'pnl':      float(p.get('unrealised', 0)),
                })
        return open_pos
    except Exception as e:
        log(f"Error getting positions: {e}")
        return []

def get_daily_pnl():
    """Get today's realized P&L."""
    try:
        smart_api, _ = get_smart_api()
        positions    = smart_api.position()
        if not positions['status']:
            return 0
        total_pnl = sum(
            float(p.get('realised', 0))
            for p in (positions.get('data') or [])
        )
        return total_pnl
    except:
        return 0

def get_symbol_token(symbol):
    """Get Angel One instrument token for a symbol."""
    try:
        smart_api, _ = get_smart_api()
        result = smart_api.searchScrip('NSE', symbol)
        if result['status'] and result.get('data'):
            for item in result['data']:
                if item['tradingsymbol'] == symbol:
                    return item['symboltoken']
        # Try with -EQ suffix
        result = smart_api.searchScrip('NSE', f"{symbol}-EQ")
        if result['status'] and result.get('data'):
            return result['data'][0]['symboltoken']
    except Exception as e:
        log(f"Error getting token for {symbol}: {e}")
    return None

# ═══════════════════════════════════════════════════════════════
#  PLACE TRADE — Main function called by signal scanner
# ═══════════════════════════════════════════════════════════════
def place_trade(symbol, entry, sl, target, signal_source='HV'):
    """
    Place a CNC limit buy order for the given symbol.
    Args:
        symbol: NSE symbol (e.g. 'RAIN')
        entry : Limit buy price (HV Low)
        sl    : Stop loss price (3% below entry)
        target: Target price (HV High)
        signal_source: 'HV' or 'CAMARILLA'
    Returns:
        dict with order_id and status
    """
    ist_time = get_ist().strftime('%d %b %Y %H:%M IST')
    log(f"Trade signal: {symbol} | Entry:{entry} | SL:{sl} | Target:{target}")

    # ── Safety checks ─────────────────────────────────────────

    # Check 1: Daily loss limit (from trades file — avoids API rate limit)
    trades_check = load_trades()
    daily_pnl = sum(t.get('exit_pnl', 0) for t in trades_check)
    if daily_pnl <= -DAILY_LOSS_LIMIT:
        msg = (
            f"TRADING PAUSED — Daily loss limit hit\n"
            f"Daily P&L: Rs {daily_pnl:.0f}\n"
            f"Limit: Rs {DAILY_LOSS_LIMIT}\n"
            f"No new trades today. Resume tomorrow."
        )
        log(msg)
        send_telegram(msg)
        return {'status': 'BLOCKED', 'reason': 'daily_loss_limit'}

    # Check 2: Max open positions
    open_positions = get_open_positions()
    if len(open_positions) >= MAX_OPEN_POS:
        msg = (
            f"TRADE SKIPPED: {symbol}\n"
            f"Max {MAX_OPEN_POS} positions already open:\n"
            + '\n'.join(f"  {p['symbol']}: {p['qty']} shares" 
                       for p in open_positions)
        )
        log(msg)
        send_telegram(msg)
        return {'status': 'SKIPPED', 'reason': 'max_positions'}

    # Check 3: Already have this symbol
    trades_today = load_trades()
    if any(t['symbol'] == symbol for t in trades_today):
        log(f"{symbol}: already traded today — skip")
        return {'status': 'SKIPPED', 'reason': 'already_traded'}

    # ── Calculate position size ────────────────────────────────
    # Risk = Rs 1,000 per trade
    # SL distance = entry - sl
    sl_distance = entry - sl
    if sl_distance <= 0:
        log(f"Invalid SL: entry={entry}, sl={sl}")
        return {'status': 'ERROR', 'reason': 'invalid_sl'}

    shares = int(RISK_PER_TRADE / sl_distance)
    if shares < 1:
        shares = 1

    position_value = shares * entry
    if position_value > MAX_POSITION_VAL:
        shares = int(MAX_POSITION_VAL / entry)

    if shares < 1:
        log(f"{symbol}: position too small at entry {entry}")
        return {'status': 'ERROR', 'reason': 'position_too_small'}

    position_value = round(shares * entry, 2)
    risk_amount    = round(shares * sl_distance, 2)
    reward_amount  = round(shares * (target - entry), 2)
    rr             = round(reward_amount / risk_amount, 1) if risk_amount > 0 else 0

    log(f"  Shares: {shares} | Value: Rs {position_value} | "
        f"Risk: Rs {risk_amount} | Reward: Rs {reward_amount}")

    # ── Get symbol token ───────────────────────────────────────
    time.sleep(0.5)  # avoid rate limit
    token, trading_symbol = get_symbol_token(symbol)
    if not token:
        log(f"Could not find token for {symbol}")
        send_telegram(f"TRADE ERROR: {symbol}\nCould not find instrument token")
        return {'status': 'ERROR', 'reason': 'token_not_found'}

    # ── Place buy order ────────────────────────────────────────
    try:
        smart_api, _ = get_smart_api()

        order_params = {
            'variety':          'NORMAL',
            'tradingsymbol':    trading_symbol,
            'symboltoken':      token,
            'transactiontype':  'BUY',
            'exchange':         'NSE',
            'ordertype':        'LIMIT',
            'producttype':      'DELIVERY',  # CNC
            'duration':         'DAY',
            'price':            str(entry),
            'quantity':         str(shares),
            'squareoff':        '0',
            'stoploss':         '0',
        }

        log(f"Placing order: {order_params}")
        response = smart_api.placeOrder(order_params)

        if not response['status']:
            error_msg = response.get('message', 'Unknown error')
            log(f"Order failed: {error_msg}")
            send_telegram(
                f"ORDER FAILED: {symbol}\n"
                f"Error: {error_msg}\n"
                f"Place manually: {shares} shares at Rs {entry}"
            )
            return {'status': 'ERROR', 'reason': error_msg}

        order_id = response['data']['orderid']
        log(f"Order placed! Order ID: {order_id}")

        # ── Save trade record ──────────────────────────────────
        trade_record = {
            'symbol':        symbol,
            'order_id':      order_id,
            'entry':         entry,
            'sl':            sl,
            'target':        target,
            'shares':        shares,
            'position_value': position_value,
            'risk_amount':   risk_amount,
            'reward_amount': reward_amount,
            'rr':            rr,
            'status':        'OPEN',
            'signal_source': signal_source,
            'placed_at':     ist_time,
            'sl_order_id':   None,
        }
        trades_today.append(trade_record)
        save_trades(trades_today)

        # ── Place SL order ─────────────────────────────────────
        sl_order_id = place_sl_order(
            smart_api, symbol, token, shares, sl, entry
        )
        if sl_order_id:
            trade_record['sl_order_id'] = sl_order_id
            save_trades(trades_today)

        # ── Send Telegram confirmation ─────────────────────────
        sl_status = f"SL order placed: {sl_order_id}" if sl_order_id else "SL: Place manually"
        msg = (
            f"ORDER PLACED: {symbol}\n"
            f"{ist_time}\n"
            f"\n"
            f"Shares  : {shares}\n"
            f"Entry   : Rs {entry} (limit)\n"
            f"SL      : Rs {sl}\n"
            f"Target  : Rs {target}\n"
            f"Value   : Rs {position_value:,.0f}\n"
            f"Risk    : Rs {risk_amount:,.0f}\n"
            f"Reward  : Rs {reward_amount:,.0f}\n"
            f"R:R     : 1:{rr}\n"
            f"\n"
            f"Order ID: {order_id}\n"
            f"{sl_status}\n"
            f"\n"
            f"Hold max 15 days\n"
            f"Exit at target or SL"
        )
        send_telegram(msg)

        return {
            'status':   'PLACED',
            'order_id': order_id,
            'shares':   shares,
            'value':    position_value,
        }

    except Exception as e:
        log(f"Order placement error: {e}")
        send_telegram(
            f"ORDER ERROR: {symbol}\n"
            f"Error: {e}\n"
            f"Place manually: {shares} shares at Rs {entry}\n"
            f"SL: Rs {sl} | Target: Rs {target}"
        )
        return {'status': 'ERROR', 'reason': str(e)}

# ═══════════════════════════════════════════════════════════════
#  PLACE SL ORDER
# ═══════════════════════════════════════════════════════════════
def place_sl_order(smart_api, symbol, token, shares, sl_price, entry_price):
    """Place a stoploss-market order to exit if SL is hit."""
    try:
        # Trigger price = SL price
        # If price falls to trigger → sell at market
        sl_params = {
            'variety':          'STOPLOSS',
            'tradingsymbol':    trading_symbol,
            'symboltoken':      token,
            'transactiontype':  'SELL',
            'exchange':         'NSE',
            'ordertype':        'STOPLOSS_MARKET',
            'producttype':      'DELIVERY',
            'duration':         'DAY',
            'price':            '0',
            'triggerprice':     str(sl_price),
            'quantity':         str(shares),
            'squareoff':        '0',
            'stoploss':         '0',
        }

        response = smart_api.placeOrder(sl_params)
        if response['status']:
            sl_order_id = response['data']['orderid']
            log(f"SL order placed: {sl_order_id} at Rs {sl_price}")
            return sl_order_id
        else:
            log(f"SL order failed: {response.get('message')}")
            return None
    except Exception as e:
        log(f"SL order error: {e}")
        return None

# ═══════════════════════════════════════════════════════════════
#  CHECK POSITIONS — Run every hour
# ═══════════════════════════════════════════════════════════════
def check_positions():
    """
    Check open positions against targets.
    If target hit → place sell order.
    Run hourly during market hours.
    """
    ist_time = get_ist().strftime('%d %b %Y %H:%M IST')
    trades   = load_trades()
    if not trades:
        return

    open_trades = [t for t in trades if t['status'] == 'OPEN']
    if not open_trades:
        return

    log(f"Checking {len(open_trades)} open positions...")

    try:
        smart_api, _ = get_smart_api()
        positions    = smart_api.position()
        if not positions['status']:
            return

        pos_map = {}
        for p in (positions.get('data') or []):
            pos_map[p['tradingsymbol']] = p

    except Exception as e:
        log(f"Error fetching positions: {e}")
        return

    for trade in open_trades:
        sym = trade['symbol']
        if sym not in pos_map:
            continue

        pos = pos_map[sym]
        ltp = float(pos.get('ltp', 0))
        qty = int(pos.get('netqty', 0))

        if qty <= 0:
            # Position closed (SL hit or manual exit)
            trade['status'] = 'CLOSED'
            pnl = float(pos.get('realised', 0))
            trade['exit_pnl'] = pnl
            log(f"{sym}: position closed | P&L: Rs {pnl:.0f}")
            send_telegram(
                f"POSITION CLOSED: {sym}\n"
                f"{ist_time}\n"
                f"P&L: Rs {pnl:+.0f}\n"
                f"Status: {'PROFIT' if pnl > 0 else 'LOSS'}"
            )
            continue

        # Check if target hit
        if ltp >= trade['target']:
            log(f"{sym}: TARGET HIT at {ltp} (target: {trade['target']})")
            token = get_symbol_token(sym)
            if token:
                # Place market sell order
                sell_params = {
                    'variety':         'NORMAL',
                    'tradingsymbol':   sym,
                    'symboltoken':     token,
                    'transactiontype': 'SELL',
                    'exchange':        'NSE',
                    'ordertype':       'MARKET',
                    'producttype':     'DELIVERY',
                    'duration':        'DAY',
                    'price':           '0',
                    'quantity':        str(qty),
                    'squareoff':       '0',
                    'stoploss':        '0',
                }
                try:
                    response = smart_api.placeOrder(sell_params)
                    if response['status']:
                        sell_id = response['data']['orderid']
                        trade['status']      = 'TARGET_HIT'
                        trade['exit_order']  = sell_id
                        trade['exit_price']  = ltp
                        pnl_est = round(qty * (ltp - trade['entry']), 0)
                        send_telegram(
                            f"TARGET HIT: {sym}\n"
                            f"{ist_time}\n"
                            f"Exit price : Rs {ltp}\n"
                            f"Est P&L    : Rs +{pnl_est:,.0f}\n"
                            f"Order ID   : {sell_id}"
                        )
                        log(f"{sym}: sell order placed: {sell_id}")
                except Exception as e:
                    log(f"Sell order error for {sym}: {e}")
                    send_telegram(
                        f"TARGET HIT: {sym} at Rs {ltp}\n"
                        f"SELL MANUALLY — {qty} shares\n"
                        f"Auto-sell failed: {e}"
                    )

    save_trades(trades)

# ═══════════════════════════════════════════════════════════════
#  EOD SQUARE OFF — Run at 3:15 PM
# ═══════════════════════════════════════════════════════════════
def eod_check():
    """
    End of day check:
    - CNC positions are held overnight (no square off needed)
    - Just send summary of open positions
    - Cancel any unfilled limit orders
    """
    ist_time = get_ist().strftime('%d %b %Y %H:%M IST')
    trades   = load_trades()
    open_t   = [t for t in trades if t['status'] == 'OPEN']

    if not open_t:
        log("EOD: No open positions")
        return

    try:
        smart_api, _ = get_smart_api()

        # Cancel unfilled buy orders (price never reached HV Low)
        orders = smart_api.orderBook()
        if orders['status']:
            for order in (orders.get('data') or []):
                if (order.get('status') == 'open'
                        and order.get('transactiontype') == 'BUY'
                        and order.get('producttype') == 'DELIVERY'):
                    # Cancel unfilled limit buy orders
                    try:
                        smart_api.cancelOrder(
                            order['orderid'], 'NORMAL'
                        )
                        log(f"Cancelled unfilled order: {order['orderid']} "
                            f"for {order['tradingsymbol']}")
                        # Remove from trades
                        for t in open_t:
                            if t.get('order_id') == order['orderid']:
                                t['status'] = 'CANCELLED'
                    except:
                        pass

        # Send EOD summary of filled positions
        positions = smart_api.position()
        if positions['status']:
            held = [p for p in (positions.get('data') or [])
                    if int(p.get('netqty', 0)) > 0]

            if held:
                lines = [f"EOD POSITIONS SUMMARY — {ist_time}", ""]
                for p in held:
                    sym   = p['tradingsymbol']
                    qty   = int(p['netqty'])
                    avg   = float(p.get('netavgprice', 0))
                    ltp   = float(p.get('ltp', 0))
                    pnl   = float(p.get('unrealised', 0))
                    # Find target from trades
                    tgt   = next((t['target'] for t in open_t
                                  if t['symbol'] == sym), 0)
                    lines.append(
                        f"{sym}: {qty} shares @ Rs {avg:.2f}\n"
                        f"  LTP: Rs {ltp:.2f} | P&L: Rs {pnl:+.0f}\n"
                        f"  Target: Rs {tgt} | Holding overnight"
                    )
                send_telegram('\n'.join(lines))

        save_trades(trades)

    except Exception as e:
        log(f"EOD check error: {e}")

# ═══════════════════════════════════════════════════════════════
#  MAIN — for testing
# ═══════════════════════════════════════════════════════════════
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--positions', action='store_true')
    parser.add_argument('--eod',       action='store_true')
    parser.add_argument('--test-trade', action='store_true')
    args = parser.parse_args()

    os.makedirs(os.path.join(BASE_DIR, 'logs'), exist_ok=True)

    if args.positions:
        pos = get_open_positions()
        print(f"Open positions: {len(pos)}")
        for p in pos:
            print(f"  {p}")

    elif args.eod:
        eod_check()

    elif args.test_trade:
        # Test with a paper trade (will actually place order!)
        print("WARNING: This places a real order!")
        print("Use --positions to check positions instead")

    else:
        print("Usage:")
        print("  python3 angel_trader.py --positions")
        print("  python3 angel_trader.py --eod")
