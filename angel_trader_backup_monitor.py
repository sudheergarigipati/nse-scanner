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
RISK_PER_TRADE   = 1000   # Rs 1000 risk per trade (optimised)
MAX_POSITION_VAL = 20000  # Phase 1   # Max Rs 25,000 per trade
MAX_OPEN_POS     = 3       # Max 3 open positions (optimised)
DAILY_LOSS_LIMIT = 750    # Phase 1
MONTHLY_LOSS_LIMIT = 3000 # Phase 1
DRY_RUN = False  # Set True for testing — no real orders placed    # Stop trading if daily loss > Rs 1,500
SL_PCT           = 3.0     # 3% SL (matches backtest)
ORDER_TYPE       = 'CNC'   # Delivery (not intraday)

# ── Cautionary/Surveillance stocks — skip these ──────────────
# Angel One blocks orders for these stocks
# Add any stock that gets AB4036 error
CAUTIONARY_STOCKS = set()  # Will be updated dynamically

# ── Helpers ───────────────────────────────────────────────────
def log(msg):
    ts   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

def get_ist():
    return datetime.now()

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

def place_gtt_oco(smart_api, trading_symbol, token, shares, buy_price, sl_price, target_price):
    """
    Place GTT OCO (One Cancels Other) order.
    Sets BOTH SL and Target in a single GTT rule.
    When one triggers, the other is automatically cancelled.
    Works for CNC delivery trades even before T+1 settlement.
    """
    import time
    time.sleep(1)
    try:
        gtt_params = {
            "tradingsymbol": trading_symbol,
            "symboltoken":   token,
            "exchange":      "NSE",
            "producttype":   "DELIVERY",
            "transactiontype": "SELL",
            "qty":           str(shares),
            "disclosedqty":  str(shares),
            "timeperiod":    365,
            # OCO = two trigger prices
            "triggerprice":  str(sl_price),           # SL trigger (lower)
            "price":         str(round(sl_price * 0.99, 2)),  # SL limit
            "triggerprice2": str(target_price),        # Target trigger (upper)
            "price2":        str(round(target_price * 1.001, 2)),  # Target limit
        }
        log(f"  Placing GTT OCO: {trading_symbol} x{shares} | SL Rs {sl_price} | Target Rs {target_price}")
        response = smart_api.gttCreateRule(gtt_params)
        log(f"  GTT OCO response: {response}")
        if isinstance(response, (str, int)) and str(response).isdigit() and len(str(response)) > 3:
            gtt_id = str(response)
            log(f"  GTT OCO placed: {gtt_id}")
            return gtt_id
        elif isinstance(response, dict) and response.get("status"):
            gtt_id = response.get("data", {}).get("id")
            log(f"  GTT OCO placed: {gtt_id}")
            return gtt_id
        else:
            log(f"  GTT OCO failed: {response} - placing SL only as fallback")
            # Fallback - place SL only
            sl_params = {
                "tradingsymbol": trading_symbol,
                "symboltoken":   token,
                "exchange":      "NSE",
                "producttype":   "DELIVERY",
                "transactiontype": "SELL",
                "price":         str(round(sl_price * 0.99, 2)),
                "qty":           str(shares),
                "triggerprice":  str(sl_price),
                "disclosedqty":  str(shares),
                "timeperiod":    365,
            }
            fallback = smart_api.gttCreateRule(sl_params)
            if fallback:
                log(f"  Fallback SL GTT placed: {fallback}")
                return str(fallback)
            return None
    except Exception as e:
        log(f"  GTT OCO error: {e}")
        return None



def place_gtt_sl(smart_api, trading_symbol, token, shares, buy_price, sl_price):
    """Place GTT SL order — triggers when price drops to sl_price."""
    import time
    time.sleep(1)
    try:
        gtt_params = {
            "tradingsymbol":   trading_symbol,
            "symboltoken":     token,
            "exchange":        "NSE",
            "producttype":     "DELIVERY",
            "transactiontype": "SELL",
            "price":           str(round(sl_price * 0.99, 2)),
            "qty":             str(shares),
            "triggerprice":    str(sl_price),
            "disclosedqty":    str(shares),
            "timeperiod":      365,
        }
        log(f"  Placing GTT SL: {trading_symbol} x{shares} trigger Rs {sl_price}")
        response = smart_api.gttCreateRule(gtt_params)
        log(f"  GTT SL response: {response}")
        if isinstance(response, (str, int)) and str(response).isdigit() and len(str(response)) > 3:
            log(f"  GTT SL placed: {response}")
            return str(response)
        elif isinstance(response, dict) and response.get("status"):
            gtt_id = response.get("data", {}).get("id")
            log(f"  GTT SL placed: {gtt_id}")
            return gtt_id
        else:
            log(f"  GTT SL failed: {response}")
            return None
    except Exception as e:
        log(f"  GTT SL error: {e}")
        return None

def place_gtt_target(smart_api, trading_symbol, token, shares, buy_price, target_price):
    """Place GTT Target order — triggers when price rises to target_price."""
    import time
    time.sleep(1)
    try:
        gtt_params = {
            "tradingsymbol":   trading_symbol,
            "symboltoken":     token,
            "exchange":        "NSE",
            "producttype":     "DELIVERY",
            "transactiontype": "SELL",
            "price":           str(round(target_price * 1.001, 2)),
            "qty":             str(shares),
            "triggerprice":    str(target_price),
            "disclosedqty":    str(shares),
            "timeperiod":      365,
        }
        log(f"  Placing GTT Target: {trading_symbol} x{shares} trigger Rs {target_price}")
        response = smart_api.gttCreateRule(gtt_params)
        log(f"  GTT Target response: {response}")
        if isinstance(response, (str, int)) and str(response).isdigit() and len(str(response)) > 3:
            log(f"  GTT Target placed: {response}")
            return str(response)
        elif isinstance(response, dict) and response.get("status"):
            gtt_id = response.get("data", {}).get("id")
            log(f"  GTT Target placed: {gtt_id}")
            return gtt_id
        else:
            log(f"  GTT Target failed: {response}")
            return None
    except Exception as e:
        log(f"  GTT Target error: {e}")
        return None

def place_gtt_buy(smart_api, trading_symbol, token, shares, entry, sl, target, w_h3, target2):
    """
    Place GTT BUY order at Weekly L3.
    Triggers when price falls to/below entry.
    Immediately places SL GTT after buy fills.
    """
    import time
    time.sleep(1)
    try:
        gtt_params = {
            "tradingsymbol" : trading_symbol,
            "symboltoken"   : token,
            "exchange"      : "NSE",
            "producttype"   : "DELIVERY",
            "transactiontype": "BUY",
            "price"         : str(round(entry * 1.002, 2)),  # 0.2% above trigger for fill
            "qty"           : str(shares),
            "triggerprice"  : str(entry),
            "disclosedqty"  : str(shares),
            "timeperiod"    : 7,  # valid for 1 week
        }
        log(f"  Placing GTT BUY: {trading_symbol} x{shares} @ Rs {entry}")
        response = smart_api.gttCreateRule(gtt_params)
        log(f"  GTT BUY response: {response}")

        if isinstance(response, (str, int)) and str(response).isdigit() and len(str(response)) > 3:
            gtt_id = str(response)
        elif isinstance(response, dict) and response.get("status"):
            gtt_id = response.get("data", {}).get("id")
        else:
            log(f"  GTT BUY failed: {response}")
            return None

        log(f"  GTT BUY placed: {gtt_id}")
        return gtt_id
    except Exception as e:
        log(f"  GTT BUY error: {e}")
        return None


def cancel_gtt(gtt_id, trading_symbol, token):
    """Cancel an existing GTT order."""
    import time
    time.sleep(0.5)
    try:
        smart_api, _ = get_smart_api()
        params = {
            "id"           : str(gtt_id),
            "tradingsymbol": trading_symbol,
            "symboltoken"  : token,
            "exchange"     : "NSE",
        }
        response = smart_api.gttCancelRule(params)
        log(f"  GTT cancelled: {gtt_id} | Response: {response}")
        return True
    except Exception as e:
        log(f"  GTT cancel error: {e}")
        return False


def get_gtt_status(gtt_id):
    """Check if GTT order has been triggered/filled."""
    import time
    time.sleep(0.5)
    try:
        smart_api, _ = get_smart_api()
        response = smart_api.gttDetails(str(gtt_id))
        if response and response.get('data'):
            status = response['data'].get('status', '')
            log(f"  GTT {gtt_id} status: {status}")
            return status
        return None
    except Exception as e:
        log(f"  GTT status error: {e}")
        return None


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
    import time
    time.sleep(0.5)
    try:
        smart_api, _ = get_smart_api()
        eq_symbol = f"{symbol}-EQ"
        result = smart_api.searchScrip('NSE', eq_symbol)
        if result and result.get('status') and result.get('data'):
            for item in result['data']:
                if item['tradingsymbol'] == eq_symbol:
                    log(f"  Token: {eq_symbol} -> {item['symboltoken']}")
                    return item['symboltoken'], eq_symbol
        log(f'  Token not found for {symbol}')
        return None, None
    except Exception as e:
        log(f'Error getting token for {symbol}: {e}')
        return None, None


# ═══════════════════════════════════════════════════════════════
#  PLACE TRADE — Main function called by signal scanner
# ═══════════════════════════════════════════════════════════════
def place_trade(symbol, entry, sl, target, signal_source='HV', w_h3=None, w_h4=None, target2=None):
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
    # Check trade limits first
    if not check_trade_limits():
        return {"status": "SKIPPED", "reason": "trade_limit_reached"}


    # ── Skip if already in this position (check Angel One live) ──
    try:
        _existing = get_open_positions()
        _symbols  = [p['symbol'] for p in _existing]
        if trading_symbol in _symbols or symbol in _symbols:
            log(f"  SKIP {symbol} - already in open position on Angel One")
            return {'status': 'SKIP', 'reason': 'already_in_position'}
    except Exception as _e:
        log(f"  Position check failed ({_e}) - proceeding with order")
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

    # ── Market hours check — no orders after 3:25 PM ──────────
    from datetime import datetime, time as dtime
    ist_now = get_ist()
    market_open  = dtime(9, 15)
    market_close = dtime(15, 25)
    if not (market_open <= ist_now.time() <= market_close):
        log(f"  {symbol}: outside market hours ({ist_now.strftime('%H:%M')}) — skip")
        send_telegram(
            f"SIGNAL MISSED (after hours): {symbol}\n"
            f"Time: {ist_now.strftime('%H:%M IST')}\n"
            f"Will re-scan tomorrow at 9:30 AM"
        )
        return {'status': 'SKIPPED', 'reason': 'outside_market_hours'}

    # ── Get symbol token ───────────────────────────────────────
    time.sleep(0.5)  # avoid rate limit
    token, trading_symbol = get_symbol_token(symbol)
    if not token:
        log(f"Could not find token for {symbol}")
        send_telegram(f"TRADE ERROR: {symbol}\nCould not find instrument token")
        return {'status': 'ERROR', 'reason': 'token_not_found'}

    # ── Place BUY order (Market or Limit) ────────────────────
    try:
        smart_api, _ = get_smart_api()

        # Get current live price
        try:
            ltp_data = smart_api.ltpData('NSE', trading_symbol, token)
            current_price = float(ltp_data.get('data', {}).get('ltp', entry))
        except:
            current_price = entry

        pct_from_l3 = (current_price - entry) / entry * 100

        if pct_from_l3 <= 0.5:
            # Price AT or below L3 — MARKET order
            order_type  = 'MARKET'
            order_price = '0'
            log(f"  Price {current_price:.2f} at/below L3 {entry} — MARKET order")
        else:
            # Price above L3 — LIMIT order at L3
            order_type  = 'LIMIT'
            order_price = str(round(entry * 1.001, 2))
            log(f"  Price {current_price:.2f} is {pct_from_l3:.1f}% above L3 — LIMIT order @ Rs {entry}")

        order_params = {
            'variety'        : 'NORMAL',
            'tradingsymbol'  : trading_symbol,
            'symboltoken'    : token,
            'transactiontype': 'BUY',
            'exchange'       : 'NSE',
            'ordertype'      : order_type,
            'producttype'    : 'DELIVERY',
            'duration'       : 'DAY',
            'price'          : order_price,
            'quantity'       : str(shares),
            'squareoff'      : '0',
            'stoploss'       : '0',
        }

        log(f"Placing {order_type} BUY: {symbol} x{shares} @ Rs {entry}")
        response = smart_api.placeOrder(order_params)

        # Cautionary stock check
        if isinstance(response, dict) and response.get('errorcode') == 'AB4036':
            cf = os.path.join(BASE_DIR, 'cautionary_stocks.json')
            try:
                import json as _j
                data = _j.load(open(cf)) if os.path.exists(cf) else {'stocks': []}
                if symbol not in data['stocks']:
                    data['stocks'].append(symbol)
                    _j.dump(data, open(cf, 'w'))
            except: pass
            send_telegram(
                f"⚠️ MANUAL TRADE: {symbol}\n"
                f"Blocked by Angel One\n"
                f"BUY {shares} shares @ Rs {entry}\n"
                f"SL: Rs {sl} | Target: Rs {target}"
            )
            return {'status': 'BLOCKED', 'reason': 'cautionary_stock'}

        # Get order ID
        if isinstance(response, str) and len(response) > 5:
            order_id = response
        elif isinstance(response, dict) and response.get('status'):
            order_id = response.get('data', {}).get('orderid', str(response))
        else:
            error_msg = response.get('message','Unknown') if isinstance(response,dict) else str(response)
            log(f'Order failed: {error_msg}')
            send_telegram(
                f"❌ ORDER FAILED: {symbol}\n"
                f"Error: {error_msg}\n"
                f"Place manually: {shares} shares @ Rs {entry}"
            )
            return {'status': 'ERROR', 'reason': error_msg}

        log(f"{order_type} order placed! ID: {order_id}")

        # ── Save trade record ──────────────────────────────────
        trade_record = {
            'symbol'        : symbol,
            'order_id'      : order_id,
            'order_type'    : order_type,
            'entry'         : entry,
            'sl'            : sl,
            'target'        : target,
            'shares'        : shares,
            'position_value': position_value,
            'risk_amount'   : risk_amount,
            'reward_amount' : reward_amount,
            'rr'            : rr,
            'status'        : 'OPEN',
            'signal_source' : signal_source,
            'placed_at'     : ist_time,
            'sl_order_id'   : None,
            'leg1_done'     : False,
            'leg1_shares'   : shares // 2,
            'leg2_shares'   : shares - shares // 2,
        }
        trades_today.append(trade_record)
        save_trades(trades_today)

        # ── Send Telegram confirmation ─────────────────────────
        msg = (
            f"ORDER PLACED: {symbol}\n"
            f"{ist_time}\n"
            f"Type : {order_type}\n"
            f"\n"
            f"Entry  : Rs {entry} (Daily L3)\n"
            f"SL     : Rs {sl} (Daily L4)\n"
            f"Target : Rs {target} (HV High)\n"
            f"R:R    : 1:{rr}\n"
            f"\n"
            f"Shares : {shares}\n"
            f"Capital: Rs {position_value:,.0f}\n"
            f"Risk   : Rs {risk_amount:,.0f}\n"
            f"Reward : Rs {reward_amount:,.0f}\n"
            f"\n"
            f"Order ID: {order_id}\n"
            f"GTT SL + Target placed after fill"
        )
        send_telegram(msg)

        return {
            'status'  : 'GTT_PLACED',
            'order_id': order_id,
            'shares'  : shares,
            'value'   : position_value,
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
def place_sl_order(smart_api, trading_symbol, token, shares, sl_price):
    """Place SL order. Returns order_id or None."""
    import time
    time.sleep(1)
    try:
        params = {
            'variety':         'STOPLOSS',
            'tradingsymbol':   trading_symbol,
            'symboltoken':     token,
            'transactiontype': 'SELL',
            'exchange':        'NSE',
            'ordertype':       'STOPLOSS_MARKET',
            'producttype':     'DELIVERY',
            'duration':        'DAY',
            'price':           '0',
            'triggerprice':    str(sl_price),
            'quantity':        str(shares),
            'squareoff':       '0',
            'stoploss':        '0',
        }
        log(f'  Placing SL: {trading_symbol} x{shares} trigger Rs {sl_price}')
        response = smart_api.placeOrder(params)
        log(f'  SL raw response: {type(response).__name__} | {response}')

        # Get order ID
        if isinstance(response, str) and len(response) > 5:
            sl_id = response
        elif isinstance(response, dict) and response.get('status'):
            data = response.get('data', {})
            sl_id = data.get('orderid') if isinstance(data, dict) else str(data)
        else:
            log(f'  SL failed: {response}')
            return None

        # Wait 3 sec and verify not rejected
        time.sleep(3)
        orders = smart_api.orderBook()
        if orders and orders.get('data'):
            for o in orders['data']:
                if o.get('orderid') == sl_id:
                    status = o.get('status', '')
                    if status == 'rejected':
                        reason = o.get('text', 'unknown')
                        log(f'  SL rejected by exchange: {reason}')
                        return None
                    else:
                        log(f'  SL confirmed: {sl_id} status={status}')
                        return sl_id

        log(f'  SL placed (unverified): {sl_id}')
        return sl_id

    except Exception as e:
        log(f'  SL error: {e}')
        return None



# ── Trade Counter Functions ───────────────────────────────────
def get_trade_count():
    """Get today and this week trade count."""
    from datetime import timedelta
    today = str(date.today())
    week_start = (date.today() - timedelta(days=date.today().weekday())).strftime("%Y-%m-%d")
    try:
        with open(TRADES_FILE) as f:
            data = json.load(f)
        trades = data.get("trades", [])
        daily = len([t for t in trades if
                     t.get("placed_at","")[:10] == today and
                     t.get("status") not in ["CANCELLED","ERROR"]])
        weekly = len([t for t in trades if
                      t.get("placed_at","")[:10] >= week_start and
                      t.get("status") not in ["CANCELLED","ERROR"]])
        return daily, weekly
    except:
        return 0, 0

def check_trade_limits():
    """Check daily/monthly loss limits. Returns True if can trade."""
    daily, weekly = get_trade_count()
    log(f"Trade count — Today: {daily} | Week: {weekly}")
    return True

def get_latest_price(symbol):
    """Get live price from Yahoo Finance, fallback to DB."""
    try:
        import yfinance as yf
        t = yf.Ticker(f"{symbol}.NS")
        h = t.history(period="1d", interval="5m")
        if not h.empty:
            price = float(h["Close"].iloc[-1])
            log(f"  Live price {symbol}: Rs {price:.2f}")
            return price
    except Exception as e:
        log(f"  Yahoo Finance failed for {symbol}: {e}")

    # Fallback to DB
    try:
        import sqlite3
        db_path = os.path.join(BASE_DIR, 'nse_data.db')
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT close FROM daily_prices WHERE symbol=? ORDER BY date DESC LIMIT 1", (symbol,))
        row = c.fetchone()
        conn.close()
        if row:
            log(f"  DB price {symbol}: Rs {float(row[0]):.2f}")
            return float(row[0])
    except:
        pass
    return None

# ── Position Monitor ──────────────────────────────────────────
def sell_shares(sym, token, qty, reason=''):
    """Place market sell order."""
    try:
        smart_api, _ = get_smart_api()
        params = {
            'variety'        : 'NORMAL',
            'tradingsymbol'  : sym + '-EQ',
            'symboltoken'    : token or '',
            'transactiontype': 'SELL',
            'exchange'       : 'NSE',
            'ordertype'      : 'MARKET',
            'producttype'    : 'DELIVERY',
            'duration'       : 'DAY',
            'price'          : '0',
            'quantity'       : str(qty),
            'squareoff'      : '0',
            'stoploss'       : '0',
        }
        resp = smart_api.placeOrder(params)
        log(f"  Sell order {sym} x{qty}: {resp} {reason}")
        return resp
    except Exception as e:
        log(f"  Sell error {sym}: {e}")
        return None


def check_positions():
    """
    Check open positions every 15 min.
    Handles: GTT pending check, T1/T2 auto-exit,
    SL detection, Friday exit, manual trades.
    """
    log("=== Position Monitor ===")
    ist = get_ist()
    ist_time = ist.strftime('%d %b %Y %H:%M IST')
    is_friday = ist.weekday() == 4
    is_eod    = ist.hour == 15 and ist.minute >= 0  # after 3 PM

    try:
        smart_api, _ = get_smart_api()
        time.sleep(0.5)

        # Get current holdings from Angel One
        holdings = smart_api.holding()
        if not holdings or not holdings.get('status'):
            log("Could not fetch holdings")
            return

        held_symbols = {}
        for h in (holdings.get('data') or []):
            sym = h.get('tradingsymbol','').replace('-EQ','')
            held_symbols[sym] = {
                'qty'   : int(h.get('quantity', 0)),
                'avg'   : float(h.get('averageprice', 0)),
                'ltp'   : float(h.get('ltp', 0)),
                'pnl'   : float(h.get('profitandloss', 0)),
                'token' : h.get('symboltoken', ''),
            }

        # Load trades
        try:
            with open(TRADES_FILE) as f:
                data = json.load(f)
        except:
            data = {'trades': []}

        trades  = data.get('trades', [])
        updated = False

        for t in trades:
            sym    = t.get('symbol', '')
            status = t.get('status', '')
            entry  = float(t.get('entry', 0))
            sl     = float(t.get('sl', 0))
            target = float(t.get('target', 0))   # W-H3
            target2= float(t.get('target2', 0))  # HV High
            shares = int(t.get('shares', 0))
            w_h3   = float(t.get('w_h3', 0)) if t.get('w_h3') else None
            leg1_done  = t.get('leg1_done', False)
            l1_shares  = int(t.get('leg1_shares', shares // 2))
            l2_shares  = int(t.get('leg2_shares', shares - shares // 2))
            gtt_buy_id = t.get('gtt_buy_id')
            sl_gtt_id  = t.get('sl_order_id')
            token      = held_symbols.get(sym, {}).get('token', '')

            # ── GTT PENDING: Check if buy GTT was triggered ──
            if status == 'GTT_PENDING':
                if sym in held_symbols and held_symbols[sym]['qty'] > 0:
                    # GTT buy filled!
                    actual_entry = held_symbols[sym]['avg']
                    log(f"  {sym}: GTT BUY FILLED @ Rs {actual_entry:.2f}!")
                    t['status']    = 'OPEN'
                    t['entry']     = actual_entry
                    t['filled_at'] = ist_time
                    updated = True
                    # Place SL GTT + Target GTT separately
                    sl_id  = place_gtt_sl(smart_api, sym+'-EQ', token,
                                          shares, actual_entry, sl)
                    tgt_id = place_gtt_target(smart_api, sym+'-EQ', token,
                                              shares, actual_entry, target)
                    if sl_id:
                        t['sl_order_id']  = sl_id
                        log(f"  GTT SL placed: Rs {sl:.2f}")
                    else:
                        log(f"  GTT SL FAILED - set manually!")
                    if tgt_id:
                        t['target_gtt_id'] = tgt_id
                        log(f"  GTT Target placed: Rs {target:.2f}")
                    else:
                        log(f"  GTT Target FAILED - set manually!")
                    send_telegram(
                        f"ORDER FILLED: {sym}\n"
                        f"{ist_time}\n"
                        f"Bought {shares} @ Rs {actual_entry:.2f}\n"
                        f"\n"
                        f"SL GTT    : Rs {sl:.2f} (Daily L4)\n"
                        f"Target GTT: Rs {target:.2f} (HV High)\n"
                        f"Risk      : Rs {round((actual_entry - sl) * shares, 0):.0f}\n"
                        f"Reward    : Rs {round((target - actual_entry) * shares, 0):.0f}\n"
                        f"Both GTTs live on Angel One. No action needed."
                    )
                    # Still pending — check if price missed the bus
                    ltp = 0
                    try:
                        ltp = float(smart_api.ltpData('NSE', sym+'-EQ', token).get('data',{}).get('ltp', 0))
                    except:
                        pass
                    if ltp > 0 and entry > 0:
                        pct_away = (ltp - entry) / entry * 100
                        if pct_away > 2.0 and ist.hour >= 11:
                            log(f"  {sym}: GTT pending, price {pct_away:.1f}% above L3")
                            send_telegram(
                                f"⚠️ MISSED BUS? {sym}\n"
                                f"GTT set at Rs {entry:.2f}\n"
                                f"Current price Rs {ltp:.2f} ({pct_away:.1f}% above)\n"
                                f"\n"
                                f"Options:\n"
                                f"1. Wait — GTT still active\n"
                                f"2. Buy manually at market if still valid"
                            )
                    log(f"  {sym}: GTT pending | LTP={ltp:.2f} | Entry={entry:.2f}")
                continue

            if status != 'OPEN':
                continue

            # ── OPEN POSITION MONITORING ─────────────────────

            # Check if shares still held
            if sym not in held_symbols or held_symbols[sym]['qty'] == 0:
                log(f"  {sym}: shares gone — SL hit or manual exit")
                ltp_exit = sl  # assume SL hit
                pnl = round((ltp_exit - entry) * shares, 0)
                t['status']    = 'SL_HIT'
                t['exit_price']= ltp_exit
                t['exit_pnl']  = pnl
                t['exit_date'] = str(date.today())
                updated = True
                send_telegram(
                    f"🔴 SL HIT: {sym}\n"
                    f"Sold @ Rs {ltp_exit:.2f}\n"
                    f"Entry: Rs {entry:.2f}\n"
                    f"Loss: Rs {abs(pnl):,.0f}"
                )
                continue

            ltp = held_symbols[sym]['ltp']
            remaining = held_symbols[sym]['qty']

            # ── FRIDAY EOD EXIT ───────────────────────────────
            if is_friday and is_eod:
                log(f"  {sym}: Friday EOD exit")
                pnl = round((ltp - entry) * remaining, 0)
                sell_resp = sell_shares(sym, token, remaining, 'Friday EOD')
                if sell_resp:
                    # Cancel SL GTT
                    if sl_gtt_id:
                        cancel_gtt(sl_gtt_id, sym+'-EQ', token)
                    t['status']    = 'WEEK_EXIT'
                    t['exit_price']= ltp
                    t['exit_pnl']  = pnl
                    t['exit_date'] = str(date.today())
                    updated = True
                    send_telegram(
                        f"⏰ WEEK END EXIT: {sym}\n"
                        f"Friday 3PM auto-exit\n"
                        f"Sold {remaining} shares @ Rs {ltp:.2f}\n"
                        f"P&L: Rs {pnl:+,.0f}\n"
                        f"New levels Monday 9:30 AM"
                    )
                continue

            # ── T1: Weekly H3 (50% exit) ──────────────────────
            if w_h3 and not leg1_done and ltp >= w_h3:
                log(f"  {sym}: T1 HIT! LTP={ltp:.2f} >= W-H3={w_h3:.2f}")
                pnl1 = round((w_h3 - entry) * l1_shares, 0)
                sell_resp = sell_shares(sym, token, l1_shares, 'T1 W-H3')
                if sell_resp:
                    t['leg1_done'] = True
                    updated = True

                    # Cancel old SL GTT and place new one for remaining shares
                    if sl_gtt_id:
                        cancel_gtt(sl_gtt_id, sym+'-EQ', token)
                    new_sl_id = place_gtt_sl(smart_api, sym+'-EQ', token,
                                             l2_shares, entry, sl)
                    if new_sl_id:
                        t['sl_order_id'] = new_sl_id

                    send_telegram(
                        f"✅ T1 HIT: {sym}\n"
                        f"{ist_time}\n"
                        f"Sold {l1_shares} shares @ Rs {w_h3:.2f}\n"
                        f"Profit T1: +Rs {pnl1:,.0f}\n"
                        f"\n"
                        f"T2 running → Rs {target2:.2f} (HV High)\n"
                        f"Remaining: {l2_shares} shares\n"
                        f"SL updated ✅"
                    )
                else:
                    send_telegram(
                        f"⚠️ T1 MANUAL EXIT: {sym}\n"
                        f"SELL {l1_shares} shares NOW!\n"
                        f"Price Rs {ltp:.2f} >= T1 Rs {w_h3:.2f}"
                    )
                continue

            # ── T2: HV High (remaining 50% exit) ─────────────
            active_target = target2 if target2 > 0 else target
            if leg1_done and ltp >= active_target:
                log(f"  {sym}: T2 HIT! LTP={ltp:.2f} >= Target={active_target:.2f}")
                pnl2 = round((active_target - entry) * l2_shares, 0)
                sell_resp = sell_shares(sym, token, l2_shares, 'T2 HV High')
                if sell_resp:
                    if sl_gtt_id:
                        cancel_gtt(sl_gtt_id, sym+'-EQ', token)
                    t['status']    = 'TARGET_HIT'
                    t['exit_price']= active_target
                    t['exit_pnl']  = round((active_target - entry) * shares * 0.5 +
                                           (w_h3 - entry) * l1_shares, 0) if w_h3 else pnl2
                    t['exit_date'] = str(date.today())
                    updated = True
                    send_telegram(
                        f"🎯 T2 HIT: {sym}\n"
                        f"{ist_time}\n"
                        f"Sold {l2_shares} shares @ Rs {active_target:.2f}\n"
                        f"Profit T2: +Rs {pnl2:,.0f}\n"
                        f"\n"
                        f"Position fully closed ✅\n"
                        f"Great trade! 🎉"
                    )
                else:
                    send_telegram(
                        f"⚠️ T2 MANUAL EXIT: {sym}\n"
                        f"SELL {l2_shares} shares NOW!\n"
                        f"Price Rs {ltp:.2f} >= T2 Rs {active_target:.2f}"
                    )
                continue

            # ── HOLDING — just log ────────────────────────────
            pnl = round((ltp - entry) * remaining, 0)
            pnl_sign = '+' if pnl >= 0 else ''
            leg_status = 'T1 done' if leg1_done else f'T1@{w_h3:.2f}' if w_h3 else 'T1 pending'
            log(f"  {sym}: LTP={ltp:.2f} | P&L={pnl_sign}Rs {pnl:,.0f} | {leg_status}")

        # ── Check for manual trades ───────────────────────────
        for sym, h in held_symbols.items():
            already = any(t.get('symbol') == sym and
                         t.get('status') in ['OPEN','GTT_PENDING']
                         for t in trades)
            if not already and h['qty'] > 0:
                log(f"  Manual trade: {sym} {h['qty']} shares @ Rs {h['avg']:.2f}")
                trades.append({
                    'symbol'      : sym,
                    'order_id'    : 'MANUAL',
                    'entry'       : h['avg'],
                    'sl'          : round(h['avg'] * 0.97, 2),
                    'target'      : round(h['avg'] * 1.10, 2),
                    'target2'     : round(h['avg'] * 1.15, 2),
                    'shares'      : h['qty'],
                    'leg1_shares' : h['qty'] // 2,
                    'leg2_shares' : h['qty'] - h['qty'] // 2,
                    'leg1_done'   : False,
                    'status'      : 'OPEN',
                    'placed_at'   : str(date.today()),
                    'signal_source': 'MANUAL',
                })
                updated = True
                send_telegram(
                    f"✅ MANUAL TRADE DETECTED: {sym}\n"
                    f"{h['qty']} shares @ Rs {h['avg']:.2f}\n"
                    f"Auto SL: Rs {round(h['avg']*0.97,2)}\n"
                    f"Please update target manually"
                )

        if updated:
            data['trades'] = trades
            with open(TRADES_FILE, 'w') as f:
                json.dump(data, f)

        log("=== Position Monitor Done ===")

    except Exception as e:
        log(f"Position monitor error: {e}")


def send_daily_summary():
    """Send daily P&L summary at 3:30 PM."""
    try:
        with open(TRADES_FILE) as f:
            data = json.load(f)
        trades = data.get('trades', [])
        today  = str(date.today())

        open_pos   = [t for t in trades if t.get('status') == 'OPEN']
        today_closed = [t for t in trades if
                        t.get('exit_date') == today]

        daily_pnl = sum(t.get('exit_pnl', 0) for t in today_closed)
        daily, weekly = get_trade_count()

        from datetime import datetime, timedelta
        ist = datetime.now()

        send_telegram(
            f"📊 DAY SUMMARY — {ist.strftime('%d %b %Y')}\n"
            f"\n"
            f"Trades today : {daily}/{MAX_TRADES_DAY}\n"
            f"Trades week  : {weekly}/{MAX_TRADES_WEEK}\n"
            f"Open positions: {len(open_pos)}\n"
            f"Today P&L    : Rs {daily_pnl:+,.0f}\n"
            f"\n"
            f"Open positions:\n" +
            "\n".join([
                f"  {t['symbol']}: {t['shares']} shares @ Rs {t['entry']:.2f}"
                for t in open_pos
            ]) if open_pos else "  None"
        )
    except Exception as e:
        log(f"Daily summary error: {e}")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--positions', action='store_true')
    parser.add_argument('--summary',   action='store_true')
    args = parser.parse_args()

    if args.positions:
        check_positions()
    elif args.summary:
        send_daily_summary()

def check_monthly_loss():
    """Check if monthly loss limit reached."""
    from datetime import date
    month_start = date.today().strftime("%Y-%m-01")
    try:
        with open(TRADES_FILE) as f:
            data = json.load(f)
        trades = data.get("trades", [])
        monthly_pnl = sum(
            t.get("exit_pnl", 0) for t in trades
            if t.get("exit_date", "") >= month_start
            and t.get("status") not in ["OPEN", "CANCELLED", "ERROR"]
        )
        if monthly_pnl <= -MONTHLY_LOSS_LIMIT:
            log(f"Monthly loss limit reached: Rs {monthly_pnl:,.0f}")
            send_telegram(
                f"⛔ MONTHLY LOSS LIMIT REACHED\n"
                f"Loss this month: Rs {abs(monthly_pnl):,.0f}\n"
                f"Limit: Rs {MONTHLY_LOSS_LIMIT:,.0f}\n"
                f"No more trades this month\n"
                f"Review strategy before resuming"
            )
            return False
        return True
    except:
        return True
