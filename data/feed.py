"""Market data feeds behind one interface: LiveFeed (Angel One) and MockFeed
(synthetic, offline). Everything downstream — indicators, strategies, the
engine — takes a Feed and cannot tell which one it got, so the whole system
runs and tests offline, after hours, and without credentials.

Feed contract:
    get_ltp(symbol) -> float
    get_candles(symbol, interval, lookback) -> OHLCV DataFrame (ts-indexed)
"""

import logging
from datetime import datetime, time as dtime, timedelta

import numpy as np
import pandas as pd

from config import settings

logger = logging.getLogger(__name__)

SESSION_OPEN = dtime(9, 0)
SESSION_CLOSE = dtime(23, 30)

_BASE_PRICES = {
    "CRUDEOIL": 6000.0, "CRUDEOILM": 6000.0,
    "NATURALGAS": 250.0, "NATURALGASM": 250.0,
    "GOLD": 72000.0, "GOLDM": 72000.0,
    "SILVER": 90000.0, "SILVERM": 90000.0,
    "COPPER": 850.0,
}


class Feed:
    def get_ltp(self, symbol: str) -> float:
        raise NotImplementedError

    def get_candles(self, symbol: str, interval: str = "FIFTEEN_MINUTE",
                    lookback: int = 200) -> pd.DataFrame:
        raise NotImplementedError


# --------------------------------------------------------------- mock feed


def _session_index(n_bars: int, end: datetime | None = None) -> pd.DatetimeIndex:
    """Last n_bars 15-minute bars inside MCX session hours (Mon-Fri,
    09:00-23:15 IST bar opens)."""
    end = end or datetime.now().replace(minute=0, second=0, microsecond=0)
    raw = pd.date_range(end=end, periods=n_bars * 4, freq="15min")
    keep = raw[
        (raw.weekday < 5)
        & (raw.time >= SESSION_OPEN)
        & (raw.time < SESSION_CLOSE)
    ]
    return keep[-n_bars:]


def _synth_ohlcv(base_price: float, n_bars: int, seed: int) -> pd.DataFrame:
    """Random-walk OHLCV with alternating trend/range regimes and volume
    spikes on big bars, so regime detection and breakout logic see realistic
    structure instead of pure noise."""
    rng = np.random.default_rng(seed)
    bar_vol = base_price * 0.0012  # per-bar sigma ~0.12%

    drifts: list[float] = []
    direction = 1
    remaining = 0
    trending = True
    for _ in range(n_bars):
        if remaining == 0:
            trending = not trending
            remaining = int(rng.integers(40, 90))
            direction = int(rng.choice([1, -1]))
        drifts.append(direction * bar_vol * 0.45 if trending else 0.0)
        remaining -= 1

    steps = rng.normal(0, bar_vol, n_bars) + np.array(drifts)
    close = base_price + np.cumsum(steps)
    close = np.maximum(close, base_price * 0.5)  # never near zero
    open_ = np.concatenate([[base_price], close[:-1]])
    spread = np.abs(rng.normal(0, bar_vol * 0.6, n_bars))
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread

    move = np.abs(close - open_)
    base_volume = rng.uniform(800, 1200, n_bars)
    volume = base_volume * (1 + 3 * move / (bar_vol + 1e-9) * 0.2)

    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
         "volume": volume},
        index=_session_index(n_bars),
    )


class MockFeed(Feed):
    """Deterministic synthetic feed. step() advances one 15-min bar so the
    engine can run a whole simulated session in seconds."""

    WARMUP = 250  # bars always available behind the cursor

    def __init__(self, symbols: list[str] | None = None, n_bars: int = 800,
                 seed: int = 42):
        symbols = symbols or list(_BASE_PRICES)
        self._series: dict[str, pd.DataFrame] = {
            sym: _synth_ohlcv(_BASE_PRICES.get(sym, 1000.0), n_bars,
                              seed + i)
            for i, sym in enumerate(symbols)
        }
        self._n_bars = n_bars
        self.cursor = self.WARMUP  # index of the current (latest) bar

    def step(self) -> bool:
        """Advance one bar; False when the series is exhausted."""
        if self.cursor >= self._n_bars - 1:
            return False
        self.cursor += 1
        return True

    @property
    def now(self) -> datetime:
        first = next(iter(self._series.values()))
        return first.index[self.cursor].to_pydatetime()

    def get_ltp(self, symbol: str) -> float:
        return float(self._series[symbol]["close"].iloc[self.cursor])

    def set_ltp(self, symbol: str, price: float) -> None:
        """Test hook: force the current bar's close (e.g. to breach a stop)."""
        self._series[symbol].iloc[
            self.cursor, self._series[symbol].columns.get_loc("close")
        ] = price

    def get_candles(self, symbol: str, interval: str = "FIFTEEN_MINUTE",
                    lookback: int = 200) -> pd.DataFrame:
        window = self._series[symbol].iloc[: self.cursor + 1]
        if interval == "ONE_HOUR":
            window = (
                window.resample("1h")
                .agg({"open": "first", "high": "max", "low": "min",
                      "close": "last", "volume": "sum"})
                .dropna()
            )
        elif interval != "FIFTEEN_MINUTE":
            raise ValueError(f"MockFeed does not serve {interval}")
        return window.tail(lookback)


# --------------------------------------------------------------- live feed


_INTERVAL_MINUTES = {"FIVE_MINUTE": 5, "FIFTEEN_MINUTE": 15,
                     "THIRTY_MINUTE": 30, "ONE_HOUR": 60, "ONE_DAY": 1440}


def drop_forming_bar(df: pd.DataFrame, now, interval: str) -> pd.DataFrame:
    """Remove the still-forming last candle from a broker response.

    Angel's getCandleData includes the CURRENT partial bar (verified live
    2026-07-17). The engine scans at bucket start, so without this every
    strategy evaluated a seconds-old, near-empty bar as "the last bar" —
    no breakout, no volume, no flip could ever be seen, and the bot went
    a week structurally unable to signal. A bar timestamped T (its open)
    is complete only when now >= T + interval.
    """
    if df.empty:
        return df
    last_open = df.index[-1].to_pydatetime().replace(tzinfo=None)
    minutes = _INTERVAL_MINUTES.get(interval, 15)
    if now < last_open + timedelta(minutes=minutes):
        return df.iloc[:-1]
    return df


class LiveFeed(Feed):
    """Polling feed over Angel One. token_map (symbol -> symboltoken) comes
    from the contract monitor, which resolves the active contract daily."""

    def __init__(self, token_map: dict[str, str], exchange: str = "MCX"):
        self._tokens = dict(token_map)
        self._exchange = exchange

    def update_token(self, symbol: str, token: str) -> None:
        self._tokens[symbol] = token  # after rollover

    def get_ltp(self, symbol: str) -> float:
        from broker.auto_login import get_api, with_auth_retry

        @with_auth_retry
        def _do():
            return get_api().ltpData(self._exchange, symbol,
                                     self._tokens[symbol])

        data = _do()
        return float(data["data"]["ltp"])

    # Approximate bars per MCX session (09:00-23:30 IST) by interval —
    # used to translate a lookback in BARS into a fetch window in DAYS.
    # Landmine L2: a flat 30-bars/day guess starved ONE_HOUR lookbacks.
    BARS_PER_DAY = {
        "FIVE_MINUTE": 174.0,
        "FIFTEEN_MINUTE": 58.0,
        "THIRTY_MINUTE": 29.0,
        "ONE_HOUR": 14.5,
        "ONE_DAY": 1.0,
    }

    # Shared across instances: scan cycles fetch 5 symbols x 2 intervals
    # back-to-back, which tripped Angel's per-second limits (observed
    # 2026-07-17, SILVER scan errors). Every broker candle fetch is paced
    # at least CANDLE_FETCH_GAP_SECS apart, wherever it comes from.
    _last_fetch_mono: float = 0.0

    @classmethod
    def _pace(cls) -> None:
        import time as _time
        gap = settings.CANDLE_FETCH_GAP_SECS
        wait = cls._last_fetch_mono + gap - _time.monotonic()
        if wait > 0:
            _time.sleep(wait)
        cls._last_fetch_mono = _time.monotonic()

    def get_candles(self, symbol: str, interval: str = "FIFTEEN_MINUTE",
                    lookback: int = 200) -> pd.DataFrame:
        from broker.auto_login import get_api, with_auth_retry
        from data.historical import fetch_ohlcv
        from data.store import save_candles

        per_day = self.BARS_PER_DAY.get(interval, 14.5)
        # +40% margin for weekends/holidays inside the calendar window.
        days = max(3, int(lookback / per_day * 1.4) + 2)

        @with_auth_retry
        def _do():
            self._pace()
            return fetch_ohlcv(get_api(), self._tokens[symbol], interval,
                               days)

        df = drop_forming_bar(_do(), datetime.now(), interval)
        if not df.empty:
            save_candles(symbol, interval, df)
        return df.tail(lookback)
