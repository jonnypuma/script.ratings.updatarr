"""Read ratings from TMDbHelper when that addon is installed.

TMDbHelper publishes its TMDb client for other addons. This bridge reads the
details TMDbHelper has already cached, including the score and external IDs, and
does not make a new TMDb request. A title that is not in that cache is left for
this addon's own request.
"""

from __future__ import annotations

from resources.lib.helper_ratings import (
    HelperResult,
    ratings_from_mdblist,
    ratings_from_omdb_record,
    ratings_from_properties,
    ratings_from_tmdb_payload,
)
from resources.lib.ids import is_imdb, is_numeric_id


def open_helper(config, log):
    if not getattr(config, "use_tmdbhelper", True):
        return None
    try:
        import xbmcaddon

        xbmcaddon.Addon("plugin.video.themoviedb.helper")
        from tmdbhelper_lib.api.tmdb.api import TMDb
    except Exception:
        log("TMDbHelper is not installed, so ratings come from this addon's own sources")
        return None
    try:
        return HelperBridge(TMDb(api_key=config.tmdb_key or "0"), log)
    except Exception as exc:
        log(f"TMDbHelper could not be used ({exc.__class__.__name__})")
        return None


class HelperBridge:
    def __init__(self, api, log) -> None:
        self.api = api
        self.log = log

    def lookup(self, kind: str, tmdb: str = "", imdb: str = "", season: int | None = None, episode: int | None = None) -> HelperResult:
        result = HelperResult()
        if not tmdb:
            return result
        try:
            payload = self._cached(kind, tmdb, season, episode)
        except Exception as exc:
            self.log(f"TMDbHelper lookup skipped ({exc.__class__.__name__})")
            return result
        result.ratings.update(ratings_from_tmdb_payload(payload))
        if isinstance(payload, dict):
            if "ratings" in payload:
                for key, value in ratings_from_mdblist(payload).items():
                    result.ratings.setdefault(key, value)
            if isinstance(payload.get("omdb"), dict):
                for key, value in ratings_from_omdb_record(payload["omdb"]).items():
                    result.ratings.setdefault(key, value)
            if isinstance(payload.get("infoproperties"), dict):
                for key, value in ratings_from_properties(payload["infoproperties"]).items():
                    result.ratings.setdefault(key, value)
        result.ids.update(_ids_from_tmdb(payload, kind))
        return result

    def _cached(self, kind: str, tmdb: str, season, episode):
        path = ["movie" if kind == "movie" else "tv", str(tmdb)]
        if kind == "episode" and season is not None and episode is not None:
            path += ["season", int(season), "episode", int(episode)]
        return self.api.get_request_lc(*path, append_to_response=self.api.append_to_response, cache_only=True) or None


def _ids_from_tmdb(payload, kind: str) -> dict:
    if not isinstance(payload, dict):
        return {}
    ext = payload.get("external_ids") if isinstance(payload.get("external_ids"), dict) else {}
    found = {}
    imdb = (payload.get("imdb_id") if kind == "movie" else None) or ext.get("imdb_id")
    if is_imdb(str(imdb or "")):
        found["imdb"] = str(imdb)
    tvdb = ext.get("tvdb_id")
    if kind == "tv" and is_numeric_id(str(tvdb or "")):
        found["tvdb"] = str(int(tvdb))
    return found
