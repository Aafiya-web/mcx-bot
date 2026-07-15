"""Historical OHLCV from Angel One getCandleData (mcx-backtest-runner skill).

Angel One serves at most a few days-to-months per request depending on the
interval, and ~400 days of history overall. Requests are therefore chunked;
per-request day limits below are the documented values — VERIFY against
current SmartAPI docs if a fetch comes back truncated.
"""

import logging
import time
from datetime import datetime, timedelta

import pandas as pd

logger = logging.getLogger(__name__)

CANDLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]

# Max days per getCandleData request, by interval (Angel One docs).
MAX_DAYS_PER_REQUEST = {
    "ONE_MINUTE": 30,
    "THREE_MINUTE": 60,
    "FIVE_MINUTE": 100,
    "TEN_MINUTE": 100,
    "FIFTEEN_MINUTE": 200,
    "THIRTY_MINUTE": 200,
    "ONE_HOUR": 400,
    "ONE_DAY": 2000,
}
_REQUEST_GAP_SECS = 0.4  # stay far under Angel's ~3 req/s historical limit
_RATE_LIMIT_RETRIES = 3
_RATE_LIMIT_WAIT_SECS = 5.0


def _is_rate_limit(exc: Exception) -> bool:
    """Angel's throttle response ('Access denied because of exceeding
    access rate') surfaces as a smartapi DataException whose text carries
    the phrase. Observed live 2026-07-15 during afternoon scans."""
    return "exceeding access rate" in str(exc).lower()


def _get_candles_with_backoff(api, params: dict) -> dict:
    for attempt in range(1, _RATE_LIMIT_RETRIES + 1):
        try:
            return api.getCandleData(params)
        except Exception as exc:
            if not _is_rate_limit(exc) or attempt == _RATE_LIMIT_RETRIES:
                raise
            wait = _RATE_LIMIT_WAIT_SECS * attempt
            logger.warning("Angel rate limit hit — backing off %.0fs "
                           "(attempt %d/%d)", wait, attempt,
                           _RATE_LIMIT_RETRIES)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _chunks(start: datetime, end: datetime,
            interval: str) -> list[tuple[datetime, datetime]]:
    """Split [start, end] into windows the API will accept."""
    span = timedelta(days=MAX_DAYS_PER_REQUEST.get(interval, 30))
    out: list[tuple[datetime, datetime]] = []
    lo = start
    while lo < end:
        hi = min(lo + span, end)
        out.append((lo, hi))
        lo = hi
    return out


def _to_frame(raw: list) -> pd.DataFrame:
    df = pd.DataFrame(raw, columns=CANDLE_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("timestamp").sort_index()
    return df.astype(float)


def fetch_ohlcv(api, symbol_token: str, interval: str = "FIFTEEN_MINUTE",
                days: int = 30, exchange: str = "MCX") -> pd.DataFrame:
    """Fetch `days` of candles ending now, chunked and stitched.

    `api` is an authenticated SmartConnect (broker.auto_login.get_api()).
    Returns a timestamp-indexed OHLCV DataFrame (may be empty off-session).
    """
    end = datetime.now()
    start = end - timedelta(days=days)
    frames: list[pd.DataFrame] = []

    for lo, hi in _chunks(start, end, interval):
        params = {
            "exchange": exchange,
            "symboltoken": symbol_token,
            "interval": interval,
            "fromdate": lo.strftime("%Y-%m-%d %H:%M"),
            "todate": hi.strftime("%Y-%m-%d %H:%M"),
        }
        data = _get_candles_with_backoff(api, params)
        rows = (data or {}).get("data") or []
        if rows:
            frames.append(_to_frame(rows))
        time.sleep(_REQUEST_GAP_SECS)

    if not frames:
        logger.warning("No candles for token %s (%s, %sd)",
                       symbol_token, interval, days)
        return pd.DataFrame(columns=CANDLE_COLUMNS[1:])

    df = pd.concat(frames)
    return df[~df.index.duplicated(keep="last")].sort_index()
