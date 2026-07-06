"""Economic calendar feed — real scheduled-event awareness for the macro
agent and the entry blackout gate.

Provider chain: FinnhubProvider (needs FINNHUB_API_KEY; the endpoint is
premium-gated on some plans) -> StaticWeeklyProvider fallback (the original
EIA Wed/Thu table) whenever the primary is unavailable for ANY reason. The
source actually used is logged and cached, and surfaces in the morning
briefing, so a silent fallback is impossible.

Relevance filter: the regex map below (derived from the mcx-regime-detector
skill's HIGH_IMPACT_EVENTS table) IS the high-impact filter. The provider's
own impact grade is recorded but not trusted — providers grade
inconsistently, and an event we don't recognise can't gate trading anyway.

Times: providers deliver UTC; the bot's clock is IST. India has no DST, so
the fixed +05:30 offset is exact and needs no tzdata dependency.

Cache: one JSON blob per day in the existing bot_state table — one provider
call per day, refreshed on the first access after midnight or via refresh()
(run_bot fires that with the 07:30 morning briefing).
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests

from config import settings
from database import models

logger = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)
CACHE_KEY = "econ_cal_cache"
FETCH_DAYS_AHEAD = 3

_ALL5 = ["CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER"]

# (pattern, US-only, affected base instruments). First match wins.
EVENT_SYMBOL_MAP: list[tuple[re.Pattern, bool, list[str]]] = [
    (re.compile(r"fomc|interest rate decision|federal funds"), True, _ALL5),
    (re.compile(r"\bcpi\b|consumer price"), True,
     ["CRUDEOIL", "GOLD", "SILVER", "COPPER"]),
    (re.compile(r"nonfarm|non farm|payrolls"), True,
     ["CRUDEOIL", "GOLD", "SILVER"]),
    (re.compile(r"crude oil (stocks|inventor)"), False, ["CRUDEOIL"]),
    (re.compile(r"natural gas (storage|stocks)"), False, ["NATURALGAS"]),
    # OPEC dates are irregular: caught here IF the provider carries them;
    # they are NOT in the static fallback (documented in HANDOFF.md).
    (re.compile(r"\bopec\b"), False, ["CRUDEOIL", "NATURALGAS"]),
]


def classify_event(name: str, country: str = "") -> list[str]:
    """Affected base instruments for an event name; [] = irrelevant."""
    lowered = name.lower()
    for pattern, us_only, symbols in EVENT_SYMBOL_MAP:
        if pattern.search(lowered):
            if us_only and country.upper() not in ("US", "USA"):
                continue
            return list(symbols)
    return []


@dataclass
class EconEvent:
    name: str
    ts_utc: datetime          # naive UTC
    country: str
    impact: str
    symbols: list[str]
    source: str

    def ts_ist(self) -> datetime:
        return self.ts_utc + IST_OFFSET

    def to_dict(self) -> dict:
        return {"name": self.name, "ts_utc": self.ts_utc.isoformat(),
                "country": self.country, "impact": self.impact,
                "symbols": self.symbols, "source": self.source}

    @classmethod
    def from_dict(cls, d: dict) -> "EconEvent":
        return cls(d["name"], datetime.fromisoformat(d["ts_utc"]),
                   d["country"], d["impact"], d["symbols"], d["source"])


class CalendarUnavailable(RuntimeError):
    """Provider could not deliver events — callers fall back."""


class CalendarProvider:
    name = "provider"

    def fetch(self, from_date: date, to_date: date) -> list[EconEvent]:
        raise NotImplementedError


class FinnhubProvider(CalendarProvider):
    """GET /api/v1/calendar/economic?from&to&token — response
    {"economicCalendar": [{"event","country","time"(UTC),"impact",...}]}.
    Verified against the official finnhub-python client, July 2026."""

    name = "finnhub"
    URL = "https://finnhub.io/api/v1/calendar/economic"
    _session = requests.Session()

    def fetch(self, from_date: date, to_date: date) -> list[EconEvent]:
        if not settings.FINNHUB_API_KEY:
            raise CalendarUnavailable("FINNHUB_API_KEY not set")
        try:
            resp = self._session.get(
                self.URL,
                params={"from": from_date.isoformat(),
                        "to": to_date.isoformat(),
                        "token": settings.FINNHUB_API_KEY},
                timeout=10)
        except Exception as exc:
            raise CalendarUnavailable(f"request failed: {exc}") from exc
        if resp.status_code != 200:
            raise CalendarUnavailable(
                f"HTTP {resp.status_code}"
                + (" (economic calendar may be premium-gated on this plan)"
                   if resp.status_code in (401, 403) else ""))
        try:
            rows = resp.json().get("economicCalendar")
        except ValueError as exc:
            raise CalendarUnavailable(f"bad JSON: {exc}") from exc
        if rows is None:
            raise CalendarUnavailable("response missing 'economicCalendar'")

        events: list[EconEvent] = []
        for row in rows:
            symbols = classify_event(str(row.get("event", "")),
                                     str(row.get("country", "")))
            if not symbols:
                continue
            ts = self._parse_time(str(row.get("time", "")))
            if ts is None:
                continue
            events.append(EconEvent(str(row["event"]), ts,
                                    str(row.get("country", "")),
                                    str(row.get("impact", "")),
                                    symbols, self.name))
        return events

    @staticmethod
    def _parse_time(raw: str) -> datetime | None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None


# The original weekly table (was agents/analysts.py SCHEDULED_EVENTS).
# weekday 0=Mon .. 4=Fri; release time 14:30 UTC = 20:00 IST (actual EIA
# release: 10:30 ET crude Wed / gas Thu).
SCHEDULED_EVENTS: dict[int, list[tuple[str, str]]] = {
    2: [("EIA crude inventory", "CRUDEOIL")],
    3: [("EIA natural gas storage", "NATURALGAS")],
}
_STATIC_RELEASE_UTC = timedelta(hours=14, minutes=30)


class StaticWeeklyProvider(CalendarProvider):
    """Offline fallback: regenerates the fixed weekly EIA schedule. Never
    fails, knows nothing about Fed/CPI/OPEC — a floor, not a substitute."""

    name = "static-weekly"

    def fetch(self, from_date: date, to_date: date) -> list[EconEvent]:
        events: list[EconEvent] = []
        day = from_date
        while day <= to_date:
            for name, symbol in SCHEDULED_EVENTS.get(day.weekday(), []):
                ts = datetime(day.year, day.month, day.day) \
                    + _STATIC_RELEASE_UTC
                events.append(EconEvent(name, ts, "US", "high", [symbol],
                                        self.name))
            day += timedelta(days=1)
        return events


class EconomicCalendar:
    """The service the engine/agents/briefings use. Provider-agnostic."""

    def __init__(self, provider: CalendarProvider | None = None,
                 fallback: CalendarProvider | None = None, db_path=None):
        self.provider = provider or FinnhubProvider()
        self.fallback = fallback or StaticWeeklyProvider()
        self.db = db_path
        self.source: str | None = None

    def get_events(self, now: datetime | None = None) -> list[EconEvent]:
        now = now or datetime.now()
        raw = models.get_state(CACHE_KEY, "", self.db)
        if raw:
            try:
                cache = json.loads(raw)
                if cache["date"] == now.date().isoformat():
                    self.source = cache["source"]
                    return [EconEvent.from_dict(d) for d in cache["events"]]
            except Exception as exc:
                logger.warning("calendar cache unreadable (%s) — refetching",
                               exc)
        return self.refresh(now)

    def refresh(self, now: datetime | None = None) -> list[EconEvent]:
        now = now or datetime.now()
        frm = now.date()
        to = frm + timedelta(days=FETCH_DAYS_AHEAD)
        try:
            events = self.provider.fetch(frm, to)
            self.source = self.provider.name
            logger.info("Economic calendar: %d relevant event(s) from '%s'",
                        len(events), self.source)
        except CalendarUnavailable as exc:
            events = self.fallback.fetch(frm, to)
            self.source = self.fallback.name
            logger.warning(
                "Economic calendar provider '%s' unavailable (%s) — using "
                "'%s' fallback (%d event(s))", self.provider.name, exc,
                self.source, len(events))

        models.set_state(CACHE_KEY, json.dumps({
            "date": now.date().isoformat(),
            "source": self.source,
            "events": [e.to_dict() for e in events],
        }), self.db)
        return events

    def upcoming_for(self, base_symbol: str, now: datetime | None = None,
                     window_minutes: int | None = None) -> list[EconEvent]:
        """Events affecting base_symbol inside [now, now + window] —
        the entry-blackout set."""
        now = now or datetime.now()
        window = timedelta(minutes=window_minutes
                           if window_minutes is not None
                           else settings.EVENT_BLACKOUT_MINUTES)
        now_utc = now - IST_OFFSET
        return [e for e in self.get_events(now)
                if base_symbol in e.symbols
                and timedelta(0) <= (e.ts_utc - now_utc) <= window]

    def events_today(self, now: datetime | None = None) -> list[EconEvent]:
        now = now or datetime.now()
        return [e for e in self.get_events(now)
                if e.ts_ist().date() == now.date()]
