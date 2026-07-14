"""Angel One SmartAPI session lifecycle (mcx-angel-auth skill).

TOTP auto-login, cached-token reuse, refresh, and an auth-retry decorator.
SmartApi is imported lazily so paper mode (and the test suite) never needs
the smartapi-python package or credentials installed.

Current library facts (verified July 2026, smartapi-python 1.4.8):
    pip install smartapi-python pyotp
    from SmartApi import SmartConnect          # note the capitalisation
    api.generateSession(client_id, password, pyotp.TOTP(key).now())

SEBI: sessions auto-close daily; schedule_auto_login() re-logs at 08:50 IST.
"""

import json
import logging
import time
from functools import wraps

from config import settings

logger = logging.getLogger(__name__)

TOKEN_FILE = settings.PROJECT_ROOT / ".tokens.json"  # gitignored
TOKEN_REUSE_HOURS = 20  # JWT lives ~24h; reuse while younger than this

_api = None


def _smart_connect():
    """Lazy import so the dependency is only needed when actually logging in."""
    try:
        from SmartApi import SmartConnect
    except ImportError as e:
        raise settings.ConfigError(
            "smartapi-python is not installed — run: "
            "pip install smartapi-python pyotp"
        ) from e
    # smartapi logs entire instrument dumps (thousands of lines) as single
    # INFO records via logzero, flooding the journal on every searchScrip.
    # logzero's handler holds the real stderr, so redirect_stderr can't
    # catch it — silence it at the source, keep ERROR-level records.
    try:
        import logging as _logging

        import logzero
        logzero.loglevel(_logging.ERROR)
    except Exception:
        pass
    return SmartConnect


def _require_creds() -> None:
    missing = [n for n in ("ANGEL_API_KEY", "ANGEL_CLIENT_ID",
                           "ANGEL_PASSWORD", "ANGEL_TOTP_KEY")
               if not getattr(settings, n)]
    if missing:
        raise settings.ConfigError(
            "Angel One login needs these in .env: " + ", ".join(missing)
        )


def get_api():
    """The one entry point: returns an authenticated SmartConnect."""
    global _api
    if _api is None:
        _api = _load_or_login()
    return _api


def login():
    """Fresh TOTP login; saves tokens to disk. Retries 3x."""
    global _api
    _require_creds()
    import pyotp
    from notifications.telegram import send_message

    SmartConnect = _smart_connect()
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            api = SmartConnect(api_key=settings.ANGEL_API_KEY)
            totp = pyotp.TOTP(settings.ANGEL_TOTP_KEY).now()
            data = api.generateSession(settings.ANGEL_CLIENT_ID,
                                       settings.ANGEL_PASSWORD, totp)
            if not data.get("status"):
                raise ValueError(f"Login failed: {data.get('message')}")

            TOKEN_FILE.write_text(json.dumps({
                "jwt": data["data"]["jwtToken"],
                "refresh": data["data"]["refreshToken"],
                "feed_token": api.getfeedToken(),
                "logged_at": time.time(),
            }))
            _api = api
            send_message("✅ Angel One login successful")
            logger.info("Angel One login successful")
            return api
        except Exception as e:
            last_exc = e
            logger.error("Login attempt %d/3 failed: %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(5)

    send_message(f"🚨 Angel One login FAILED after 3 attempts: {last_exc}")
    raise last_exc


def _load_or_login():
    """Reuse the cached JWT when fresh enough, else log in."""
    if TOKEN_FILE.exists():
        try:
            tokens = json.loads(TOKEN_FILE.read_text())
            age_hrs = (time.time() - tokens.get("logged_at", 0)) / 3600
            if age_hrs < TOKEN_REUSE_HOURS:
                SmartConnect = _smart_connect()
                api = SmartConnect(api_key=settings.ANGEL_API_KEY)
                api.setAccessToken(tokens["jwt"])
                # A cached token must PROVE itself before we trust it —
                # smartapi accepts setAccessToken() silently and only
                # fails (AG8001) at the first real call, which crashed
                # startup when a stale .tokens.json survived a restart.
                profile = api.getProfile(tokens.get("refresh", ""))
                if not (profile or {}).get("status"):
                    raise ValueError(f"cached token rejected: "
                                     f"{(profile or {}).get('message')}")
                logger.info("Reusing cached token (%.1fh old, validated)",
                            age_hrs)
                return api
        except settings.ConfigError:
            raise
        except Exception as e:
            logger.warning("Cached token unusable (%s) — fresh login", e)
    return login()


def refresh_token() -> bool:
    """Extend the session via the refresh token; full re-login on failure."""
    global _api
    try:
        tokens = json.loads(TOKEN_FILE.read_text())
        api = get_api()
        data = api.generateToken(tokens["refresh"])
        if data.get("status"):
            tokens["jwt"] = data["data"]["jwtToken"]
            TOKEN_FILE.write_text(json.dumps(tokens))
            api.setAccessToken(tokens["jwt"])
            logger.info("Token refreshed")
            return True
    except Exception as e:
        logger.error("Token refresh failed: %s", e)
        login()
    return False


def with_auth_retry(fn):
    """Retry a broker call once with a refreshed token, then a full login.

    Angel error AG8001 = invalid/expired token (see mcx-angel-auth skill's
    error table).
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        for attempt in range(2):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                if _is_auth_error(e):
                    logger.warning("Auth error — recovering (attempt %d)",
                                   attempt + 1)
                    refresh_token() if attempt == 0 else login()
                else:
                    raise
        return fn(*args, **kwargs)
    return wrapper


def _is_auth_error(exc: Exception) -> bool:
    """Recognise an expired/invalid Angel session in ALL its costumes.

    Learned live (2026-07-13, the day the bot went blind): the error text
    is 'Invalid Token' (capital T — a case-sensitive match missed it),
    and smartapi often surfaces auth failures as KeyError('status')
    because it indexes the error response as if it succeeded.
    """
    err = str(exc).lower()
    if "ag8001" in err or "invalid token" in err or "ag8002" in err:
        return True
    return isinstance(exc, KeyError) and "status" in err
