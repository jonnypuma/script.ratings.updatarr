"""User configuration that does not depend on Kodi."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


ALLOWED_DEFAULTS = {
    "keep",
    "imdb",
    "themoviedb",
    "trakt",
    "tomatometerallcritics",
    "tomatometerallaudience",
    "metacritic",
    "letterboxd",
    "mal",
}


@dataclass
class Config:
    """Sources and run options for one update."""

    tmdb_key: str = ""
    omdb_key: str = ""
    trakt_key: str = ""
    mdblist_key: str = ""
    use_tmdb: bool = True
    use_omdb: bool = False
    use_trakt: bool = False
    use_mdblist: bool = False
    preferred_default: str = "keep"
    delay_seconds: float = 0.25
    save_ids: bool = True
    use_imdb_data: bool = True
    imdb_refresh_days: int = 30
    imdb_user_agent: str = ""
    imdb_path: str = ""
    minimum_change: float = 0.2
    update_missing_only: bool = False
    use_tmdbhelper: bool = True
    omdb_daily_limit: int = 1000
    mdblist_daily_limit: int = 1000
    recent_days: int = 0
    vote_change_percent: int = 0
    write_nfo: bool = False
    dry_run: bool = False
    schedule_enabled: bool = False
    schedule_mode: str = "daily"
    schedule_times: str = "03:00"
    schedule_hours: int = 168
    schedule_background: bool = True
    manual_background: bool = False
    schedule_types: tuple[str, ...] = field(default_factory=lambda: ("movie", "tvshow", "episode"))
    startup_delay: int = 120
    update_on_scan: bool = False
    pause_during_playback: bool = True

    def __post_init__(self) -> None:
        self.tmdb_key = (self.tmdb_key or "").strip()
        self.omdb_key = (self.omdb_key or "").strip()
        self.trakt_key = (self.trakt_key or "").strip()
        self.mdblist_key = (self.mdblist_key or "").strip()
        self.imdb_user_agent = (self.imdb_user_agent or "").strip()
        self.imdb_path = (self.imdb_path or "").strip()
        if self.preferred_default not in ALLOWED_DEFAULTS:
            self.preferred_default = "keep"
        if self.schedule_mode not in {"daily", "interval"}:
            self.schedule_mode = "daily"
        self.delay_seconds = max(0.0, float(self.delay_seconds))
        self.schedule_hours = max(1, int(self.schedule_hours))
        self.startup_delay = max(0, int(self.startup_delay))
        self.imdb_refresh_days = max(7, min(365, int(self.imdb_refresh_days)))
        try:
            self.minimum_change = max(0.0, float(self.minimum_change))
        except (TypeError, ValueError):
            self.minimum_change = 0.2
        try:
            self.omdb_daily_limit = max(0, int(self.omdb_daily_limit))
        except (TypeError, ValueError):
            self.omdb_daily_limit = 1000
        try:
            self.mdblist_daily_limit = max(0, int(self.mdblist_daily_limit))
        except (TypeError, ValueError):
            self.mdblist_daily_limit = 1000
        try:
            self.recent_days = max(0, int(self.recent_days))
        except (TypeError, ValueError):
            self.recent_days = 0
        try:
            self.vote_change_percent = max(0, int(self.vote_change_percent))
        except (TypeError, ValueError):
            self.vote_change_percent = 0

    @property
    def tmdb_active(self) -> bool:
        return self.use_tmdb and bool(self.tmdb_key)

    @property
    def omdb_active(self) -> bool:
        return self.use_omdb and bool(self.omdb_key)

    @property
    def trakt_active(self) -> bool:
        return self.use_trakt and bool(self.trakt_key)

    @property
    def mdblist_active(self) -> bool:
        return self.use_mdblist and bool(self.mdblist_key)

    def any_active(self, tmdbhelper_available: bool = False) -> bool:
        helper_ok = self.use_tmdbhelper and tmdbhelper_available and (
            self.use_tmdb or self.use_omdb or self.use_trakt or self.use_mdblist
        )
        return (
            self.tmdb_active
            or self.omdb_active
            or self.trakt_active
            or self.mdblist_active
            or self.use_imdb_data
            or helper_ok
        )

    def imdb_folder(self, profile: str) -> str:
        """Folder for the downloaded files, the SQLite index, and import drop-ins."""
        if not self.imdb_path:
            return os.path.join(profile, "imdb")
        return os.path.normpath(self.imdb_path)

    def missing_keys(self, tmdbhelper_available: bool = False) -> list[str]:
        """Enabled sources whose key is empty. The run can continue on the others.

        A missing TMDb key is not reported when TMDbHelper can supply its cache.
        """
        missing = []
        if self.use_tmdb and not self.tmdb_key and not (self.use_tmdbhelper and tmdbhelper_available):
            missing.append("tmdb")
        if self.use_omdb and not self.omdb_key:
            missing.append("omdb")
        if self.use_trakt and not self.trakt_key:
            missing.append("trakt")
        if self.use_mdblist and not self.mdblist_key:
            missing.append("mdblist")
        return missing


def selected_types(requested, allowed) -> list[str]:
    """Keep the requested media types that the user has switched on."""
    chosen = set(allowed or ())
    return [name for name in requested if name in chosen]
