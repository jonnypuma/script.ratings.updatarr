"""Stay inside the request limits published by each ratings provider.

TMDb asks callers to stay under about 40 requests a second per IP and to back off
on HTTP 429. This addon keeps a lower ceiling so TMDbHelper can use the same
address at the same time. Trakt allows 500 GET requests every 5 minutes and
sends the reset time in X-Ratelimit. A free OMDb key allows 1,000 requests a day.
MDbList asks for about one request a second.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


TMDB_LIMIT = 20
TMDB_WINDOW = 1.0
TRAKT_LIMIT = 450
TRAKT_WINDOW = 300.0
OMDB_LIMIT = 5
OMDB_WINDOW = 1.0
MDBLIST_LIMIT = 1
MDBLIST_WINDOW = 1.0
MAX_HOLD = 300.0

WINDOWS = {
    "tmdb": (TMDB_LIMIT, TMDB_WINDOW),
    "trakt": (TRAKT_LIMIT, TRAKT_WINDOW),
    "omdb": (OMDB_LIMIT, OMDB_WINDOW),
    "mdblist": (MDBLIST_LIMIT, MDBLIST_WINDOW),
}


class DailyQuota(Exception):
    """The local daily allowance for this provider is already used."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(provider)


class RunCancelled(Exception):
    """The user or Kodi stopped the run while it was waiting for a provider."""


class Limiter:
    def __init__(self, omdb_daily: int = 1000, state: dict | None = None, clock=None, mdblist_daily: int = 1000) -> None:
        self.omdb_daily = max(0, int(omdb_daily))
        self.mdblist_daily = max(0, int(mdblist_daily))
        self._daily_limits = {"omdb": self.omdb_daily, "mdblist": self.mdblist_daily}
        self.state = state if isinstance(state, dict) else {}
        self.clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        self._hits: dict[str, list[float]] = {name: [] for name in WINDOWS}
        self._not_before: dict[str, float] = {}

    def wait_for(self, provider: str, now: float | None = None) -> float:
        """Seconds to wait before the next request. Zero means send it now."""
        if provider not in WINDOWS:
            return 0.0
        moment = self.clock() if now is None else now
        daily = self._daily_limits.get(provider, 0)
        if provider in self._daily_limits:
            self._roll_day(provider, moment)
            if daily and int(self.state.get(f"{provider}_used") or 0) >= daily:
                raise DailyQuota(provider)
        hold = float(self._not_before.get(provider) or 0) - moment
        limit, window = WINDOWS[provider]
        hits = [stamp for stamp in self._hits[provider] if moment - stamp < window]
        self._hits[provider] = hits
        window_wait = 0.0
        if len(hits) >= limit:
            window_wait = window - (moment - hits[0])
        return min(MAX_HOLD, max(0.0, hold, window_wait))

    def observe(self, provider: str, headers=None, now: float | None = None) -> None:
        if provider not in WINDOWS:
            return
        moment = self.clock() if now is None else now
        self._hits[provider].append(moment)
        if provider in self._daily_limits:
            self._roll_day(provider, moment)
            used_key = f"{provider}_used"
            self.state[used_key] = int(self.state.get(used_key) or 0) + 1
        self._read_reset(provider, headers, moment)

    def exhaust(self, provider: str, now: float | None = None) -> None:
        """The provider itself said the allowance is gone."""
        daily = self._daily_limits.get(provider, 0)
        if daily:
            moment = self.clock() if now is None else now
            self._roll_day(provider, moment)
            self.state[f"{provider}_used"] = daily

    def _roll_day(self, provider: str, moment: float) -> None:
        day = datetime.fromtimestamp(moment, timezone.utc).date().isoformat()
        day_key = f"{provider}_day"
        if self.state.get(day_key) != day:
            self.state[day_key] = day
            self.state[f"{provider}_used"] = 0

    def _read_reset(self, provider: str, headers, moment: float) -> None:
        raw = _header(headers, "x-ratelimit")
        if not raw:
            return
        try:
            payload = json.loads(raw)
            remaining = int(payload.get("remaining"))
            until = _epoch(payload.get("until"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        if remaining <= 0 and until and until > moment:
            self._not_before[provider] = until


class PacedTransport:
    """Wait for the provider limit, then perform one request and record it."""

    def __init__(self, inner, limiter: Limiter, sleeper) -> None:
        self.inner = inner
        self.limiter = limiter
        self.sleeper = sleeper

    def get_json(self, url: str, headers: dict | None = None):
        provider = classify(url)
        delay = self.limiter.wait_for(provider)
        if delay > 0 and self._sleep(delay):
            raise RunCancelled()
        try:
            if hasattr(self.inner, "exchange"):
                result = self.inner.exchange(url, headers)
                payload, response_headers = result.payload, result.headers
            else:
                payload, response_headers = self.inner.get_json(url, headers), {}
        except Exception as exc:
            self.limiter.observe(provider)
            if getattr(exc, "status", None) == 429 and provider in {"omdb", "mdblist"}:
                self.limiter.exhaust(provider)
            raise
        self.limiter.observe(provider, response_headers)
        return payload

    def _sleep(self, seconds: float) -> bool:
        try:
            return bool(self.sleeper(seconds))
        except RunCancelled:
            raise


def classify(url: str) -> str:
    host = str(url or "").lower()
    if "themoviedb.org" in host or "api.tmdb.org" in host:
        return "tmdb"
    if "trakt.tv" in host:
        return "trakt"
    if "omdbapi.com" in host:
        return "omdb"
    if "mdblist.com" in host:
        return "mdblist"
    return ""


def _header(headers, name: str) -> str:
    if not headers:
        return ""
    try:
        value = headers.get(name)
        if not value:
            value = headers.get(name.title())
        return str(value or "")
    except Exception:
        return ""


def _epoch(value) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()
