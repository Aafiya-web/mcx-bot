---
name: mcx-angel-auth
description: >
  Handles Angel One SmartAPI authentication, TOTP-based auto-login, token
  refresh, and session management for the MCX trading bot. Use this skill
  when the user asks about login issues, token expiry, authentication errors,
  how to automate login, TOTP setup, API session refresh, or gets errors like
  "Invalid token", "Session expired", or "AG8001". Also triggers for
  "bot stopped trading", "login failed", "how to keep the bot logged in",
  or any Angel One API connection issue.
---

# MCX Angel Auth

Manages Angel One SmartAPI session lifecycle. The JWT token expires daily
so this skill handles fully automated re-authentication at 8:50 AM IST.

## Initial Setup (One-Time)

```python
# 1. Install dependencies
# pip install smartapi-python pyotp python-dotenv

# 2. Get TOTP secret from Angel One app:
#    My Profile → Two Factor Auth → Show Secret Key
#    Copy the base32 secret (looks like: JBSWY3DPEHPK3PXP)

# 3. Add to .env
ANGEL_API_KEY   = "your_api_key_from_smartapi_portal"
ANGEL_CLIENT_ID = "your_angel_client_id"     # e.g. A123456
ANGEL_PASSWORD  = "your_login_password"
ANGEL_TOTP_KEY  = "JBSWY3DPEHPK3PXP"        # Base32 TOTP secret
```

## Core Auth Module (broker/auto_login.py)

```python
import pyotp, json, time, schedule, logging
from smartapi import SmartConnect
from pathlib import Path

TOKEN_FILE = Path(".tokens.json")
_api       = None
logger     = logging.getLogger(__name__)

def get_api() -> SmartConnect:
    """Always call this to get an authenticated API instance."""
    global _api
    if _api is None:
        _api = _load_or_login()
    return _api

def login() -> SmartConnect:
    """Perform fresh login with TOTP. Saves tokens to disk."""
    global _api
    from config.settings import (ANGEL_API_KEY, ANGEL_CLIENT_ID,
                                  ANGEL_PASSWORD, ANGEL_TOTP_KEY)
    from notifications.telegram import send_message

    for attempt in range(3):
        try:
            api  = SmartConnect(api_key=ANGEL_API_KEY)
            totp = pyotp.TOTP(ANGEL_TOTP_KEY).now()
            data = api.generateSession(ANGEL_CLIENT_ID, ANGEL_PASSWORD, totp)

            if not data.get('status'):
                raise ValueError(f"Login failed: {data.get('message')}")

            tokens = {
                "jwt"        : data['data']['jwtToken'],
                "refresh"    : data['data']['refreshToken'],
                "feed_token" : api.getfeedToken(),
                "logged_at"  : time.time(),
            }
            TOKEN_FILE.write_text(json.dumps(tokens))
            _api = api
            send_message("✅ Angel One login successful")
            logger.info("Angel One login successful")
            return api

        except Exception as e:
            logger.error(f"Login attempt {attempt+1} failed: {e}")
            if attempt == 2:
                send_message(f"🚨 Angel One login FAILED after 3 attempts: {e}")
                raise
            time.sleep(5)

def _load_or_login() -> SmartConnect:
    """Try to reuse saved token, fall back to fresh login."""
    if TOKEN_FILE.exists():
        try:
            tokens   = json.loads(TOKEN_FILE.read_text())
            age_hrs  = (time.time() - tokens.get("logged_at", 0)) / 3600
            if age_hrs < 20:   # Token valid for ~24h, reuse if < 20h old
                from config.settings import ANGEL_API_KEY
                api = SmartConnect(api_key=ANGEL_API_KEY)
                api.setAccessToken(tokens['jwt'])
                return api
        except Exception as e:
            logger.warning(f"Cached token unusable: {e}")
    return login()

def refresh_token():
    """Use refresh token to extend session without full re-login."""
    global _api
    try:
        tokens  = json.loads(TOKEN_FILE.read_text())
        api     = get_api()
        data    = api.generateToken(tokens['refresh'])
        if data.get('status'):
            tokens['jwt'] = data['data']['jwtToken']
            TOKEN_FILE.write_text(json.dumps(tokens))
            api.setAccessToken(tokens['jwt'])
            logger.info("Token refreshed successfully")
            return True
    except Exception as e:
        logger.error(f"Token refresh failed: {e}")
        login()   # Fall back to full login
    return False

def schedule_auto_login():
    """Call this once at startup to schedule daily re-login."""
    schedule.every().day.at("08:50").do(login)   # 10 min before MCX open
    schedule.every().day.at("15:00").do(refresh_token)  # Midday refresh
    logger.info("Auto-login scheduler started")
```

## Error Code Reference

| Error Code | Meaning                  | Fix                              |
|------------|--------------------------|----------------------------------|
| AG8001     | Invalid token / expired  | Call `login()` immediately       |
| AG8002     | Session limit exceeded   | Wait 30s, retry                  |
| AG8003     | TOTP invalid             | Check system clock sync          |
| AG8004     | Wrong credentials        | Verify .env values               |
| AB1010     | Rate limit hit           | Add sleep between requests       |
| AB1004     | Market closed            | Check scheduler / market hours   |

## Auto-Recovery Wrapper

```python
from functools import wraps

def with_auth_retry(fn):
    """Decorator: retry with fresh login on auth failure."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        for attempt in range(2):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                err = str(e)
                if "AG8001" in err or "Invalid token" in err:
                    logger.warning("Auth error → refreshing token")
                    if attempt == 0:
                        refresh_token()
                    else:
                        login()
                else:
                    raise
        return fn(*args, **kwargs)
    return wrapper

# Usage:
@with_auth_retry
def place_order(symbol, side, qty):
    return get_api().placeOrder({...})
```

## Startup Checklist

```python
def startup_checks():
    """Run at bot startup to verify everything is ready."""
    checks = []

    # 1. Auth check
    try:
        api     = get_api()
        profile = api.getProfile(api.getfeedToken())
        checks.append(("✅ Auth", profile['data']['name']))
    except Exception as e:
        checks.append(("❌ Auth FAILED", str(e)))

    # 2. Market hours check
    from core.scheduler import is_market_open
    checks.append(("🟢 Market Open" if is_market_open()
                   else "🔴 Market Closed", ""))

    # 3. Token age check
    if TOKEN_FILE.exists():
        tokens  = json.loads(TOKEN_FILE.read_text())
        age_hrs = (time.time() - tokens.get("logged_at", 0)) / 3600
        checks.append(("🔑 Token Age", f"{age_hrs:.1f}h"))

    return checks
```

## systemd Environment for Secrets

Never hardcode credentials. In `mcx-bot.service`:

```ini
[Service]
EnvironmentFile=/home/ubuntu/mcx-trading-bot/.env
```

And `.env` should be chmod 600:
```bash
chmod 600 /home/ubuntu/mcx-trading-bot/.env
```
