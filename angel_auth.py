#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  angel_auth.py
#  Angel One SmartAPI Authentication
#  - Generates session token using TOTP
#  - Saves token to file for use by trader
#  - Run at 8:30 AM daily via scheduler
#
#  Usage:
#    python3 angel_auth.py          (login and save token)
#    python3 angel_auth.py --test   (test existing token)
# ═══════════════════════════════════════════════════════════════

import os
import json
import pyotp
import argparse
from datetime import datetime, timedelta
from SmartApi import SmartConnect

BASE_DIR    = os.path.expanduser('~/nse-scanner')
TOKEN_FILE  = os.path.join(BASE_DIR, 'angel_token.json')
LOG_FILE    = os.path.join(BASE_DIR, 'logs/angel_auth.log')

# ── Credentials ───────────────────────────────────────────────
CLIENT_ID   = 'S742119'
API_KEY     = 'zlGra4b8'

def read_file(filename):
    path = os.path.join(BASE_DIR, filename)
    with open(path, 'r') as f:
        return f.read().strip()

def log(msg):
    ts   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

def get_ist():
    return datetime.now()

def login():
    """Login to Angel One and save token."""
    log("=" * 50)
    log(f"Angel One Login — {get_ist().strftime('%d %b %Y %H:%M IST')}")
    log("=" * 50)

    try:
        password    = read_file('angel_pass.txt')
        totp_secret = read_file('angel_totp.txt')
        api_secret  = read_file('angel_secret.txt')
    except Exception as e:
        log(f"Error reading credentials: {e}")
        return False

    # Generate TOTP
    totp     = pyotp.TOTP(totp_secret)
    totp_val = totp.now()
    log(f"TOTP generated: {totp_val}")

    # Login
    try:
        smart_api = SmartConnect(api_key=API_KEY)
        data      = smart_api.generateSession(
            CLIENT_ID, password, totp_val
        )

        if data['status'] == False:
            log(f"Login failed: {data['message']}")
            return False

        auth_token    = data['data']['jwtToken']
        refresh_token = data['data']['refreshToken']
        feed_token    = smart_api.getfeedToken()

        # Save token
        token_data = {
            'auth_token':    auth_token,
            'refresh_token': refresh_token,
            'feed_token':    feed_token,
            'login_time':    get_ist().strftime('%Y-%m-%d %H:%M:%S'),
            'client_id':     CLIENT_ID,
            'api_key':       API_KEY,
        }
        with open(TOKEN_FILE, 'w') as f:
            json.dump(token_data, f)
        os.chmod(TOKEN_FILE, 0o600)

        log(f"Login successful!")
        log(f"Token saved to: {TOKEN_FILE}")

        # Get profile to verify
        profile = smart_api.getProfile(refresh_token)
        if profile['status']:
            name = profile['data'].get('name', 'Unknown')
            log(f"Logged in as: {name} ({CLIENT_ID})")

        return True

    except Exception as e:
        log(f"Login error: {e}")
        return False

def test_connection():
    """Test if existing token is valid."""
    log("Testing existing token...")

    if not os.path.exists(TOKEN_FILE):
        log("No token file found — run login first")
        return False

    try:
        with open(TOKEN_FILE) as f:
            token_data = json.load(f)

        smart_api  = SmartConnect(api_key=API_KEY)
        smart_api.setSessionExpiryHook(lambda: None)

        # Set the token
        auth_token = token_data['auth_token']
        smart_api._SmartConnect__auth_token = auth_token

        # Test with profile call
        profile = smart_api.getProfile(token_data['refresh_token'])
        if profile['status']:
            name = profile['data'].get('name', 'Unknown')
            log(f"Token valid — logged in as: {name}")
            log(f"Login time: {token_data.get('login_time', 'unknown')}")

            # Test order book (to verify trading access)
            orders = smart_api.orderBook()
            if orders['status']:
                order_count = len(orders.get('data', []) or [])
                log(f"Order book accessible — {order_count} orders today")
            return True
        else:
            log(f"Token invalid: {profile['message']}")
            return False

    except Exception as e:
        log(f"Test error: {e}")
        return False

def get_smart_api():
    """
    Return authenticated SmartConnect instance.
    Used by other modules (trader, scanner).
    """
    if not os.path.exists(TOKEN_FILE):
        raise Exception("No token file — run angel_auth.py first")

    with open(TOKEN_FILE) as f:
        token_data = json.load(f)

    smart_api = SmartConnect(api_key=API_KEY)

    # Properly set the auth token
    auth_token = token_data['auth_token']

    # Remove 'Bearer ' prefix if present
    if auth_token.startswith('Bearer '):
        auth_token = auth_token[7:]

    smart_api.setAccessToken(auth_token)

    return smart_api, token_data

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--test', action='store_true',
                        help='Test existing token')
    args = parser.parse_args()

    os.makedirs(os.path.join(BASE_DIR, 'logs'), exist_ok=True)

    if args.test:
        success = test_connection()
    else:
        success = login()

    if success:
        log("✅ Angel One ready for trading")
    else:
        log("❌ Authentication failed")
        exit(1)
