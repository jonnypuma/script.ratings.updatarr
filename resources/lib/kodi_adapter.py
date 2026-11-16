"""Kodi GUI, JSON-RPC, and settings. Kept apart from the rating logic so tests can run without Kodi."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from resources.lib.config import Config
from resources.lib.library import LibraryClient, parse_rpc_payload
from resources.lib.updater import NullProgress


ADDON_ID = "script.ratings.updatarr"
LOCK_PROPERTY = "script.ratings.updatarr.running"


class KodiMonitor:
    def __init__(self) -> None:
        self._monitor = xbmc.Monitor()

    def wait(self, seconds: float) -> bool:
        return self._monitor.waitForAbort(max(0.0, float(seconds)))

    def abort_requested(self) -> bool:
        return self._monitor.abortRequested()

    def pause(self, seconds: float) -> bool:
        if seconds <= 0:
            return self.abort_requested()
        return self.wait(seconds)


def addon():
    return xbmcaddon.Addon(ADDON_ID)


def addon_installed(addon_id: str) -> bool:
    try:
        xbmcaddon.Addon(addon_id)
        return True
    except Exception:
        return False


def text(string_id: int, **kwargs) -> str:
    raw = addon().getLocalizedString(string_id) or ""
    if kwargs:
        return raw.format(**kwargs)
    return raw


def log(message: str, level: int | None = None) -> None:
    xbmc.log(f"[{ADDON_ID}] {message}", xbmc.LOGINFO if level is None else level)


def notify(message: str, kind: str = "info") -> None:
    icons = {
        "info": xbmcgui.NOTIFICATION_INFO,
        "warning": xbmcgui.NOTIFICATION_WARNING,
        "error": xbmcgui.NOTIFICATION_ERROR,
    }
    xbmcgui.Dialog().notification(addon().getAddonInfo("name"), message, icons.get(kind, icons["info"]), 6500)


def choose(heading: str, options: list[str]) -> int:
    return xbmcgui.Dialog().select(heading, options)


def open_settings() -> None:
    xbmc.executebuiltin(f"Addon.OpenSettings({ADDON_ID})")


def try_lock() -> bool:
    window = xbmcgui.Window(10000)
    if window.getProperty(LOCK_PROPERTY) == "1":
        return False
    window.setProperty(LOCK_PROPERTY, "1")
    return True


def unlock() -> None:
    xbmcgui.Window(10000).clearProperty(LOCK_PROPERTY)


def selected_library_item() -> tuple[str, int]:
    dbtype = ""
    dbid = -1
    listitem = getattr(sys, "listitem", None)
    if listitem is not None:
        try:
            info = listitem.getVideoInfoTag()
            dbtype = info.getMediaType() or ""
            dbid = int(info.getDbId())
        except Exception:
            log("Could not read the selected item from the context menu", xbmc.LOGDEBUG)
    if not dbtype:
        dbtype = xbmc.getInfoLabel("ListItem.DBType") or ""
    if dbid < 1:
        raw = xbmc.getInfoLabel("ListItem.DBID") or ""
        dbid = int(raw) if raw.isdigit() else -1
    return dbtype.lower(), dbid


def load_config() -> Config:
    current = addon()
    types = []
    if _bool(current, "schedule_movies", True):
        types.append("movie")
    if _bool(current, "schedule_tvshows", True):
        types.append("tvshow")
    if _bool(current, "schedule_episodes", True):
        types.append("episode")
    return Config(
        tmdb_key=_string(current, "tmdb_key"),
        omdb_key=_string(current, "omdb_key"),
        trakt_key=_string(current, "trakt_key"),
        use_tmdb=_bool(current, "use_tmdb", True),
        use_omdb=_bool(current, "use_omdb", False),
        use_trakt=_bool(current, "use_trakt", False),
        preferred_default=_string(current, "default_rating") or "keep",
        delay_seconds=_int(current, "delay_ms", 250) / 1000.0,
        save_ids=_bool(current, "save_ids", True),
        use_imdb_data=_bool(current, "use_imdb_data", True),
        imdb_refresh_days=_int(current, "imdb_refresh_days", 30),
        imdb_user_agent=_string(current, "imdb_user_agent"),
        imdb_path=_folder(current, "imdb_path"),
        minimum_change=_number(current, "minimum_change", 0.2),
        update_missing_only=_bool(current, "update_missing_only", False),
        use_tmdbhelper=_bool(current, "use_tmdbhelper", True),
        omdb_daily_limit=_int(current, "omdb_daily_limit", 1000),
        schedule_enabled=_bool(current, "schedule_enabled", False),
        schedule_mode=_string(current, "schedule_mode") or "daily",
        schedule_times=_string(current, "schedule_times") or "03:00",
        schedule_hours=_int(current, "schedule_hours", 168),
        schedule_background=_bool(current, "schedule_background", True),
        manual_background=_bool(current, "manual_background", False),
        schedule_types=tuple(types),
        startup_delay=_int(current, "startup_delay", 120),
    )


class KodiLibrary(LibraryClient):
    def __init__(self) -> None:
        super().__init__(execute)


def execute(method: str, params: dict | None = None) -> dict:
    raw = xbmc.executeJSONRPC(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}))
    return parse_rpc_payload(raw)


class ForegroundProgress:
    def __init__(self, heading: str, monitor: KodiMonitor) -> None:
        self.heading = heading
        self.monitor = monitor
        self.dialog = xbmcgui.DialogProgress()
        self.dialog.create(heading, "")

    def cancelled(self) -> bool:
        return self.dialog.iscanceled() or self.monitor.abort_requested()

    def update(self, percent: int, message: str) -> None:
        self.dialog.update(_percent(percent), message)

    def close(self) -> None:
        self.dialog.close()


class BackgroundProgress:
    def __init__(self, heading: str, monitor: KodiMonitor) -> None:
        self.heading = heading
        self.monitor = monitor
        self.dialog = xbmcgui.DialogProgressBG()
        self.dialog.create(heading, "")

    def cancelled(self) -> bool:
        return self.monitor.abort_requested()

    def update(self, percent: int, message: str) -> None:
        self.dialog.update(_percent(percent), self.heading, message)

    def close(self) -> None:
        self.dialog.close()


def open_progress(heading: str, background: bool, monitor: KodiMonitor):
    try:
        if background:
            return BackgroundProgress(heading, monitor)
        return ForegroundProgress(heading, monitor)
    except Exception as exc:
        log(f"Progress dialog unavailable: {exc}", xbmc.LOGWARNING)
        return NullProgress()


def profile_dir() -> str:
    path = xbmcvfs.translatePath(f"special://profile/addon_data/{ADDON_ID}/")
    os.makedirs(path, exist_ok=True)
    return path


def read_last_run() -> datetime | None:
    path = os.path.join(profile_dir(), "state.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return datetime.fromisoformat(data["last_run"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_last_run(when: datetime | None = None) -> None:
    moment = when or datetime.now(timezone.utc)
    path = os.path.join(profile_dir(), "state.json")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"last_run": moment.isoformat()}, handle)
    except OSError as exc:
        log(f"Could not store the last run time: {exc}", xbmc.LOGWARNING)


def read_limits() -> dict:
    path = os.path.join(profile_dir(), "limits.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_limits(state: dict) -> None:
    path = os.path.join(profile_dir(), "limits.json")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"omdb_day": state.get("omdb_day", ""), "omdb_used": int(state.get("omdb_used") or 0)}, handle)
    except (OSError, TypeError, ValueError) as exc:
        log(f"Could not store the provider allowance: {exc}", xbmc.LOGWARNING)


def read_runs() -> list:
    path = os.path.join(profile_dir(), "runs.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def append_run(record: dict) -> None:
    from resources.lib.history import remember

    path = os.path.join(profile_dir(), "runs.json")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(remember(read_runs(), record), handle)
    except OSError as exc:
        log(f"Could not store the run result: {exc}", xbmc.LOGWARNING)


def show_summary(heading: str, message: str) -> None:
    try:
        xbmcgui.Dialog().ok(heading, message)
    except Exception as exc:
        log(f"Could not show the summary: {exc}", xbmc.LOGWARNING)
        notify(message)


def show_text(heading: str, body: str) -> None:
    try:
        xbmcgui.Dialog().textviewer(heading, body)
    except Exception as exc:
        log(f"Could not show the run log: {exc}", xbmc.LOGWARNING)


def _percent(value: int) -> int:
    return max(0, min(100, int(value)))


def _folder(current, setting_id: str) -> str:
    raw = _string(current, setting_id)
    if not raw:
        return ""
    try:
        translated = (xbmcvfs.translatePath(raw) or "").strip()
    except Exception:
        translated = ""
    chosen = translated or raw
    if "://" in chosen:
        log("IMDb data folder must be a local path; the addon data folder will be used", xbmc.LOGWARNING)
        return ""
    return os.path.normpath(chosen)


def _string(current, setting_id: str) -> str:
    try:
        return current.getSettingString(setting_id).strip()
    except Exception:
        return ""


def _bool(current, setting_id: str, default: bool) -> bool:
    try:
        return bool(current.getSettingBool(setting_id))
    except Exception:
        return default


def _int(current, setting_id: str, default: int) -> int:
    try:
        return int(current.getSettingInt(setting_id))
    except Exception:
        return default


def _number(current, setting_id: str, default: float) -> float:
    try:
        return float(current.getSettingNumber(setting_id))
    except Exception:
        text = _string(current, setting_id)
        try:
            return float(text) if text else default
        except ValueError:
            return default
