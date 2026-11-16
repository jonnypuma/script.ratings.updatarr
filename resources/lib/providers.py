"""TMDb, OMDb, and Trakt rating lookups."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field

from resources.lib.config import Config
from resources.lib.httpclient import HttpStatus
from resources.lib.ids import imdb_id, is_imdb, is_numeric_id, tmdb_id, tvdb_id
from resources.lib.limits import DailyQuota, RunCancelled
from resources.lib.media import LibraryItem


TMDB_BASE = "https://api.themoviedb.org/3"
# https://www.omdbapi.com/: every request needs apikey. A title is looked up with i, an IMDb id.
# The same page documents HTTPS. A free key allows 1,000 requests a day.
OMDB_URL = "https://www.omdbapi.com/"
TRAKT_BASE = "https://api.trakt.tv"


class AuthError(Exception):
    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(provider)


@dataclass
class FetchResult:
    ratings: dict = field(default_factory=dict)
    discovered_ids: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    requested: bool = False


def parse_tmdb_rating(payload) -> dict | None:
    if not isinstance(payload, dict) or payload.get("success") is False:
        return None
    try:
        rating = float(payload.get("vote_average"))
        votes = int(payload.get("vote_count") or 0)
    except (TypeError, ValueError):
        return None
    if rating <= 0 or rating > 10 or votes <= 0:
        return None
    return {"rating": float(round(rating, 3)), "votes": votes}


def parse_omdb(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    if str(payload.get("Response")).lower() == "false":
        error = str(payload.get("Error") or "")
        lowered = error.lower()
        if "key" in lowered:
            raise AuthError("omdb")
        if "limit" in lowered:
            raise HttpStatus(429, error or "OMDb request limit reached")
        return {}
    ratings: dict[str, dict] = {}
    imdb_rating = _score(payload.get("imdbRating"))
    imdb_votes = _votes(payload.get("imdbVotes"))
    if imdb_rating is None:
        imdb_rating, _unused = _named_score(payload, "Internet Movie Database", scale=10)
    if imdb_rating is not None:
        ratings["imdb"] = {"rating": imdb_rating, "votes": imdb_votes}
    metacritic, _meta_votes = _named_score(payload, "Metacritic", scale=100)
    metascore = _score(payload.get("Metascore"))
    if metacritic is None and metascore is not None:
        metacritic = float(round(metascore / 10.0, 3)) if metascore > 10 else metascore
    if metacritic is not None and 0 < metacritic <= 10:
        ratings["metacritic"] = {"rating": metacritic, "votes": 0}
    tomatoes, tomato_votes = _named_score(payload, "Rotten Tomatoes", scale=100)
    if tomatoes is not None and 0 < tomatoes <= 10:
        ratings["tomatometerallcritics"] = {"rating": tomatoes, "votes": tomato_votes}
    return ratings


def parse_trakt(payload) -> dict | None:
    if not isinstance(payload, dict):
        return None
    try:
        rating = float(payload.get("rating"))
        votes = int(payload.get("votes") or 0)
    except (TypeError, ValueError):
        return None
    if rating <= 0 or rating > 10 or votes <= 0:
        return None
    return {"rating": float(round(rating, 3)), "votes": votes}


def _score(value) -> float | None:
    text = str(value or "").strip()
    if not text or text.upper() == "N/A":
        return None
    if "/" in text:
        text = text.split("/", 1)[0].strip()
    text = text.rstrip("%").strip()
    try:
        return float(text)
    except ValueError:
        return None


def _votes(value) -> int:
    text = str(value or "").strip().replace(",", "")
    if not text or text.upper() == "N/A":
        return 0
    try:
        return max(0, int(float(text)))
    except ValueError:
        return 0


def _named_score(payload: dict, source: str, scale: int) -> tuple[float | None, int]:
    for entry in payload.get("Ratings") or []:
        if not isinstance(entry, dict) or entry.get("Source") != source:
            continue
        raw = _score(entry.get("Value"))
        if raw is None or raw <= 0:
            return None, 0
        rating = raw / 10.0 if scale == 100 or raw > 10 else raw
        if rating <= 0 or rating > 10:
            return None, 0
        return float(round(rating, 3)), 0
    return None, 0


class SourceClient:
    """Fetches ratings for one library run. Responses are cached for that run."""

    def __init__(self, config: Config, transport, imdb=None, helper=None) -> None:
        self.config = config
        self.transport = transport
        self.imdb = imdb
        self.helper = helper
        self._cache: dict[tuple, object] = {}

    def ratings_for(self, item: LibraryItem, show: LibraryItem | None = None) -> FetchResult:
        if item.mediatype == "episode":
            return self._episode(item, show)
        if item.mediatype == "tvshow":
            return self._title(item, "tv")
        return self._title(item, "movie")

    def _title(self, item: LibraryItem, kind: str) -> FetchResult:
        result = FetchResult()
        allow_numeric = kind == "movie"
        tmdb, discovered = self._resolve_tmdb(item, kind, allow_numeric, result)
        result.discovered_ids.update(discovered)
        imdb = imdb_id(item.uniqueids, item.imdbnumber)
        imdb = self._apply_helper(result, kind, tmdb, imdb) or imdb
        if self.config.tmdb_active and tmdb and self._needs_tmdb(result, imdb):
            payload = self._guard(result, "tmdb", self._tmdb_details, tmdb, kind)
            if isinstance(payload, dict):
                parsed = parse_tmdb_rating(payload)
                if parsed:
                    result.ratings["themoviedb"] = parsed
                result.discovered_ids.update(_external_ids(payload, kind))
                if not tmdb_id(item.uniqueids):
                    result.discovered_ids.setdefault("tmdb", str(tmdb))
                found_imdb = result.discovered_ids.get("imdb", "")
                if found_imdb:
                    imdb = imdb or found_imdb
        local = self._local_rating(imdb)
        if local:
            result.ratings["imdb"] = local
        if self._needs_omdb(result, imdb):
            parsed = self._guard(result, "omdb", self._omdb_ratings, imdb)
            if isinstance(parsed, dict):
                if result.ratings.get("imdb"):
                    parsed.pop("imdb", None)
                result.ratings.update(parsed)
        if self.config.trakt_active and "trakt" not in result.ratings and (imdb or tmdb):
            parsed = self._guard(result, "trakt", self._trakt_title, kind, imdb, tmdb)
            if isinstance(parsed, dict):
                result.ratings["trakt"] = parsed
        return result

    def _episode(self, item: LibraryItem, show: LibraryItem | None) -> FetchResult:
        result = FetchResult()
        show_tmdb = ""
        show_imdb = ""
        if show is not None:
            show_tmdb, _show_ids = self._resolve_tmdb(show, "tv", False, result)
            show_imdb = imdb_id(show.uniqueids, show.imdbnumber)
        episode_imdb = imdb_id(item.uniqueids, "")
        episode_imdb = self._apply_helper(result, "episode", show_tmdb, episode_imdb, item.season, item.episode) or episode_imdb
        if (
            self.config.tmdb_active
            and show_tmdb
            and item.season is not None
            and item.episode is not None
            and self._needs_tmdb(result, episode_imdb)
        ):
            payload = self._guard(
                result,
                "tmdb",
                self._tmdb_episode,
                show_tmdb,
                item.season,
                item.episode,
            )
            if isinstance(payload, dict):
                parsed = parse_tmdb_rating(payload)
                if parsed:
                    result.ratings["themoviedb"] = parsed
                ext = payload.get("external_ids") if isinstance(payload.get("external_ids"), dict) else {}
                found_imdb = str((ext or {}).get("imdb_id") or "")
                if is_imdb(found_imdb):
                    episode_imdb = episode_imdb or found_imdb
                    result.discovered_ids["imdb"] = found_imdb
                episode_tmdb = payload.get("id")
                if is_numeric_id(str(episode_tmdb or "")):
                    result.discovered_ids["tmdb"] = str(int(episode_tmdb))
        found = self._local_episode(show_imdb, item.season, item.episode)
        if found:
            if found.get("imdb"):
                episode_imdb = episode_imdb or found["imdb"]
                result.discovered_ids.setdefault("imdb", found["imdb"])
            if found.get("rating"):
                result.ratings["imdb"] = found["rating"]
        if "imdb" not in result.ratings and episode_imdb:
            local = self._local_rating(episode_imdb)
            if local:
                result.ratings["imdb"] = local
        if self._needs_omdb(result, episode_imdb):
            parsed = self._guard(result, "omdb", self._omdb_ratings, episode_imdb)
            if isinstance(parsed, dict):
                if result.ratings.get("imdb"):
                    parsed.pop("imdb", None)
                result.ratings.update(parsed)
        if (
            self.config.trakt_active
            and "trakt" not in result.ratings
            and item.season is not None
            and item.episode is not None
            and (show_imdb or show_tmdb)
        ):
            parsed = self._guard(
                result,
                "trakt",
                self._trakt_episode,
                show_imdb,
                show_tmdb,
                item.season,
                item.episode,
            )
            if isinstance(parsed, dict):
                result.ratings["trakt"] = parsed
        return result

    def _apply_helper(self, result: FetchResult, kind: str, tmdb: str, imdb: str, season: int | None = None, episode: int | None = None) -> str:
        if self.helper is None or not getattr(self.config, "use_tmdbhelper", True):
            return imdb
        try:
            found = self.helper.lookup(kind=kind, tmdb=tmdb or "", imdb=imdb or "", season=season, episode=episode)
        except RunCancelled:
            raise
        except Exception:
            return imdb
        ratings = getattr(found, "ratings", found if isinstance(found, dict) else {}) or {}
        ids = getattr(found, "ids", {}) or {}
        if getattr(found, "requested", False):
            result.requested = True
        for key, value in ratings.items():
            if key not in result.ratings and isinstance(value, dict) and value.get("rating"):
                result.ratings[key] = value
        for key, value in ids.items():
            if value:
                result.discovered_ids.setdefault(key, str(value))
        return imdb or result.discovered_ids.get("imdb", "")

    def _needs_tmdb(self, result: FetchResult, imdb: str) -> bool:
        if "themoviedb" not in result.ratings:
            return True
        return not imdb

    def _needs_omdb(self, result: FetchResult, imdb: str) -> bool:
        if not self.config.omdb_active or not imdb:
            return False
        return "tomatometerallcritics" not in result.ratings or "metacritic" not in result.ratings

    def _resolve_tmdb(
        self,
        item: LibraryItem,
        kind: str,
        allow_numeric_fallback: bool,
        result: FetchResult,
    ) -> tuple[str, dict]:
        existing = tmdb_id(
            item.uniqueids,
            item.imdbnumber,
            allow_numeric_fallback=allow_numeric_fallback,
        )
        if existing:
            return existing, {}
        if not self.config.tmdb_active:
            return "", {}
        imdb = imdb_id(item.uniqueids, item.imdbnumber)
        if imdb:
            found = self._guard(result, "tmdb", self._tmdb_find, imdb, "imdb_id", kind)
            if found:
                return str(found), {"tmdb": str(found)}
        if kind == "tv":
            tvdb = tvdb_id(item.uniqueids)
            if tvdb:
                found = self._guard(result, "tmdb", self._tmdb_find, tvdb, "tvdb_id", kind)
                if found:
                    return str(found), {"tmdb": str(found)}
        return "", {}

    def _guard(self, result: FetchResult, provider: str, func, *args):
        result.requested = True
        try:
            return func(*args)
        except AuthError:
            raise
        except RunCancelled:
            raise
        except DailyQuota as exc:
            if exc.provider == "omdb":
                self.config.use_omdb = False
                result.errors.append("omdb: daily allowance is used, so OMDb is off for the rest of this run")
            else:
                result.errors.append(f"{exc.provider}: the provider limit was reached")
            return None
        except HttpStatus as exc:
            if exc.status in {401, 403}:
                raise AuthError(provider) from exc
            if exc.status == 404:
                return None
            if exc.status == 429 and provider == "omdb":
                self.config.use_omdb = False
                limiter = getattr(self.transport, "limiter", None)
                if limiter is not None:
                    limiter.exhaust("omdb")
                result.errors.append("omdb: request limit reached; OMDb is off for the rest of this run")
                return None
            if exc.status == 429:
                result.errors.append(f"{provider}: rate limit reached")
                return None
            result.errors.append(f"{provider}: {exc.message or exc}")
            return None
        except Exception as exc:
            result.errors.append(f"{provider}: {exc}")
            return None

    def _get(self, url: str, headers: dict | None = None):
        key = (url, tuple(sorted((headers or {}).items())))
        if key in self._cache:
            return self._cache[key]
        payload = self.transport.get_json(url, headers=headers)
        self._cache[key] = payload
        return payload

    def _tmdb_url(self, path: str, **extra) -> str:
        params = {"api_key": self.config.tmdb_key}
        params.update(extra)
        query = urllib.parse.urlencode(params)
        return f"{TMDB_BASE}{path}?{query}"

    def _tmdb_details(self, tmdb: str, kind: str):
        path = f"/movie/{tmdb}" if kind == "movie" else f"/tv/{tmdb}"
        return self._get(self._tmdb_url(path, append_to_response="external_ids"))

    def _tmdb_episode(self, show_tmdb: str, season: int, episode: int):
        path = f"/tv/{show_tmdb}/season/{int(season)}/episode/{int(episode)}"
        return self._get(self._tmdb_url(path, append_to_response="external_ids"))

    def _tmdb_find(self, external_id: str, external_source: str, kind: str):
        quoted = urllib.parse.quote(str(external_id), safe="")
        payload = self._get(
            self._tmdb_url(f"/find/{quoted}", external_source=external_source)
        )
        if not isinstance(payload, dict):
            return ""
        bucket = "movie_results" if kind == "movie" else "tv_results"
        results = payload.get(bucket) or []
        if not results:
            return ""
        found = results[0].get("id")
        return str(found) if found else ""

    def _omdb(self, imdb: str):
        query = urllib.parse.urlencode({"apikey": self.config.omdb_key, "i": imdb, "r": "json"})
        return self._get(f"{OMDB_URL}?{query}")

    def _omdb_ratings(self, imdb: str) -> dict:
        payload = self._omdb(imdb)
        if not isinstance(payload, dict):
            return {}
        return parse_omdb(payload)

    def _trakt_headers(self) -> dict[str, str]:
        return {
            "trakt-api-version": "2",
            "trakt-api-key": self.config.trakt_key,
            "Content-Type": "application/json",
        }

    def _trakt_title(self, kind: str, imdb: str, tmdb: str):
        slug = "movies" if kind == "movie" else "shows"
        search_type = "movie" if kind == "movie" else "show"
        key = imdb or self._trakt_key_from_tmdb(tmdb, search_type)
        if not key:
            return None
        payload = self._get(f"{TRAKT_BASE}/{slug}/{urllib.parse.quote(str(key), safe='')}/ratings", self._trakt_headers())
        return parse_trakt(payload)

    def _trakt_episode(self, show_imdb: str, show_tmdb: str, season: int, episode: int):
        key = show_imdb or self._trakt_key_from_tmdb(show_tmdb, "show")
        if not key:
            return None
        path = (
            f"{TRAKT_BASE}/shows/{urllib.parse.quote(str(key), safe='')}"
            f"/seasons/{int(season)}/episodes/{int(episode)}/ratings"
        )
        return parse_trakt(self._get(path, self._trakt_headers()))

    def _local_rating(self, imdb: str) -> dict | None:
        if not self.config.use_imdb_data or self.imdb is None or not imdb:
            return None
        try:
            score = self.imdb.rating(imdb)
        except Exception:
            return None
        return score if isinstance(score, dict) else None

    def _local_episode(self, show_imdb: str, season: int | None, episode: int | None) -> dict | None:
        if not self.config.use_imdb_data or self.imdb is None or not show_imdb:
            return None
        if season is None or episode is None:
            return None
        try:
            found = self.imdb.episode(show_imdb, season, episode)
        except Exception:
            return None
        return found if isinstance(found, dict) else None

    def _trakt_key_from_tmdb(self, tmdb: str, search_type: str) -> str:
        if not tmdb:
            return ""
        payload = self._get(
            f"{TRAKT_BASE}/search/tmdb/{urllib.parse.quote(str(tmdb), safe='')}?type={search_type}",
            self._trakt_headers(),
        )
        if not isinstance(payload, list) or not payload:
            return ""
        media = payload[0].get(search_type) or {}
        ids = media.get("ids") or {}
        if ids.get("imdb"):
            return str(ids["imdb"])
        if ids.get("trakt"):
            return str(ids["trakt"])
        return ""


def _external_ids(payload: dict, kind: str) -> dict[str, str]:
    ext = payload.get("external_ids") if isinstance(payload.get("external_ids"), dict) else {}
    imdb = payload.get("imdb_id") if kind == "movie" else None
    imdb = imdb or ext.get("imdb_id") or ""
    found = {}
    if is_imdb(str(imdb)):
        found["imdb"] = str(imdb)
    tvdb = ext.get("tvdb_id")
    if kind == "tv" and is_numeric_id(str(tvdb or "")):
        found["tvdb"] = str(int(tvdb))
    return found
