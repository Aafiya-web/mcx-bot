"""Technical indicators — self-contained pandas/numpy implementations.

Deliberately NOT pandas_ta (unmaintained, breaks with modern numpy — a bad
dependency for a 1GB VM that must run unattended). Each function matches the
textbook definition the skill files assume; Wilder smoothing is used where
the original indicator specifies it (ATR, ADX).

All functions take a timestamp-indexed OHLCV DataFrame with columns
open/high/low/close/volume and return Series/DataFrames aligned to it.
"""

import numpy as np
import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def _wilder(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1 / length, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"],
         (df["high"] - prev_close).abs(),
         (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    return _wilder(true_range(df), length)


def adx(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    """Returns DataFrame with columns adx, dmp (+DI), dmn (-DI)."""
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0),
                        index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0),
                         index=df.index)
    atr_ = atr(df, length)
    plus_di = 100 * _wilder(plus_dm, length) / atr_
    minus_di = 100 * _wilder(minus_dm, length) / atr_
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return pd.DataFrame({"adx": _wilder(dx, length),
                         "dmp": plus_di, "dmn": minus_di})


def supertrend(df: pd.DataFrame, period: int = 10,
               mult: float = 3.0) -> pd.DataFrame:
    """Returns DataFrame with columns direction (1 up / -1 down) and line
    (the trailing supertrend level). Signal = direction flip (skill:
    mcx-signal-analyzer)."""
    atr_ = atr(df, period).to_numpy()
    hl2 = ((df["high"] + df["low"]) / 2).to_numpy()
    close = df["close"].to_numpy()
    n = len(df)

    upper = hl2 + mult * atr_
    lower = hl2 - mult * atr_
    f_upper = upper.copy()
    f_lower = lower.copy()
    direction = np.ones(n, dtype=int)
    line = np.full(n, np.nan)

    for i in range(1, n):
        f_upper[i] = (upper[i]
                      if upper[i] < f_upper[i - 1] or close[i - 1] > f_upper[i - 1]
                      else f_upper[i - 1])
        f_lower[i] = (lower[i]
                      if lower[i] > f_lower[i - 1] or close[i - 1] < f_lower[i - 1]
                      else f_lower[i - 1])
        if direction[i - 1] == 1:
            direction[i] = -1 if close[i] < f_lower[i] else 1
        else:
            direction[i] = 1 if close[i] > f_upper[i] else -1
        line[i] = f_lower[i] if direction[i] == 1 else f_upper[i]

    return pd.DataFrame({"direction": direction, "line": line},
                        index=df.index)


def bollinger(series: pd.Series, length: int = 20,
              std: float = 2.0) -> pd.DataFrame:
    """Returns mid/upper/lower/width_pct (band width as % of the mid)."""
    mid = series.rolling(length).mean()
    sd = series.rolling(length).std(ddof=0)
    upper = mid + std * sd
    lower = mid - std * sd
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower,
                         "width_pct": (upper - lower) / mid * 100})


def donchian(df: pd.DataFrame, length: int = 20) -> pd.DataFrame:
    """Channel of the PRIOR length bars (shifted so the current bar can
    break out of it — using the unshifted channel can never signal)."""
    return pd.DataFrame({
        "upper": df["high"].rolling(length).max().shift(1),
        "lower": df["low"].rolling(length).min().shift(1),
    })


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP, reset each calendar day."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"]
    day = df.index.date
    return pv.groupby(day).cumsum() / df["volume"].groupby(day).cumsum()


def volume_confirmed(df: pd.DataFrame, mult: float = 1.5,
                     lookback: int = 20) -> bool:
    """Breakout volume rule: last bar's volume > mult x prior average."""
    if len(df) < lookback + 1:
        return False
    avg = df["volume"].iloc[-(lookback + 1):-1].mean()
    return bool(df["volume"].iloc[-1] > mult * avg)
