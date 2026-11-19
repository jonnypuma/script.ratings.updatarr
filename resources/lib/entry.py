"""Program, context menu, and scheduled entry points."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from resources.lib import kodi_adapter as kodi
from resources.lib.httpclient import JsonClient
from resources.lib.imdbdata import DatasetCancelled, DatasetError, ImdbData
from resources.lib.limits import Limiter, PacedTransport, RunCancelled
from resources.lib.media import ALL_TYPES, SPECS
from resources.lib.params import is_truthy, parse_media_types, parse_params
from resources.lib.providers import SourceClient
from resources.lib.schedule import daily_due, is_due, parse_times
from resources.lib.updater import update_items, update_types
from resources.lib.config import selected_types


MISSING_KEY = {
    "tmdb": 32160,
    "omdb": 32161,
    "trakt": 32162,
    "mdblist": 32164,
}
PROVIDER_LABEL = {
    "tmdb": "TMDb",
    "omdb": "OMDb",
    "trakt": "Trakt",
    "mdblist": "MDbList",
}
ZERO_COUNTS = {"checked": 0, "updated": 0, "unchanged": 0, "skipped": 0, "failed": 0}


def main(argv) -> None:
    params = parse_params(list(argv))
    action = params.get("action", "").strip().lower()
    if action in {"settings", "opensettings"}:
        kodi.open_settings()
        return
    if action in {"runs", "history"}:
        show_runs()
        return
    if action in {"imdb", "refreshimdb"}:
        refresh_imdb_now(_background_flag(params))
        return
    background = _background_flag(params)
    raw_type = params.get("mediatype") or params.get("content") or params.get("type") or params.get("dbtype") or ""
    dbid = params.get("dbid", "").strip()
    if dbid.isdigit() and raw_type.strip().lower() == "season":
        run_season(int(dbid), background)
        return
    types = parse_media_types(raw_type)
    if dbid.isdigit() and len(types) == 1:
        run_one(types[0], int(dbid), background)
        return
    if types:
        allowed = _types_for_run(types)
        if not allowed:
            return
        run_many(allowed, background, scheduled=False)
        return
    selected = kodi.choose(
        kodi.text(32106),
        [kodi.text(spec.label_string) for spec in SPECS.values()]
        + [kodi.text(32103), kodi.text(32050), kodi.text(32051), kodi.text(32104)],
    )
    options = list(SPECS) + ["all", "runs", "imdb", "settings"]
    if selected < 0 or selected >= len(options):
        return
    choice = options[selected]
    if choice == "settings":
        kodi.open_settings()
        return
    if choice == "runs":
        show_runs()
        return
    if choice == "imdb":
        refresh_imdb_now(background)
        return
    if choice == "all":
        allowed = _types_for_run(list(ALL_TYPES))
        if allowed:
            run_many(allowed, background, scheduled=False)
        return
    run_many([choice], background, scheduled=False)


def context_main() -> None:
    dbtype, dbid = kodi.selected_library_item()
    if dbtype == "season" and dbid >= 1:
        background = kodi.load_config().manual_background
        selected = kodi.choose(
            kodi.text(32105),
            [kodi.text(32158), kodi.text(SPECS["episode"].all_string), kodi.text(32156)],
        )
        if selected == 0:
            run_season(dbid, background)
        elif selected == 1:
            allowed = _types_for_run(["episode"])
            if allowed:
                run_many(allowed, background, scheduled=False)
        elif selected == 2:
            allowed = _types_for_run(list(ALL_TYPES))
            if allowed:
                run_many(allowed, background, scheduled=False)
        return
    spec = SPECS.get(dbtype)
    if spec is None or dbid < 1:
        kodi.notify(kodi.text(32172), "warning")
        return
    background = kodi.load_config().manual_background
    if spec.name == "tvshow":
        selected = kodi.choose(
            kodi.text(32105),
            [kodi.text(spec.item_string), kodi.text(32157), kodi.text(spec.all_string), kodi.text(32156)],
        )
        if selected == 0:
            run_one(spec.name, dbid, background)
        elif selected == 1:
            run_show(dbid, background)
        elif selected == 2:
            allowed = _types_for_run(["tvshow"])
            if allowed:
                run_many(allowed, background, scheduled=False)
        elif selected == 3:
            allowed = _types_for_run(list(ALL_TYPES))
            if allowed:
                run_many(allowed, background, scheduled=False)
        return
    selected = kodi.choose(
        kodi.text(32105),
        [kodi.text(spec.item_string), kodi.text(spec.all_string), kodi.text(32156)],
    )
    if selected == 0:
        run_one(spec.name, dbid, background)
    elif selected == 1:
        allowed = _types_for_run([spec.name])
        if allowed:
            run_many(allowed, background, scheduled=False)
    elif selected == 2:
        allowed = _types_for_run(list(ALL_TYPES))
        if allowed:
            run_many(allowed, background, scheduled=False)


def service_main() -> None:
    monitor = kodi.KodiMonitor()
    waited_for_startup = False
    pending_scan_update = False
    while not monitor.abort_requested():
        config = kodi.load_config()
        if monitor.pop_scan_finished():
            pending_scan_update = True
        interested = config.schedule_enabled or config.use_imdb_data or config.update_on_scan
        if not interested:
            pending_scan_update = False
            if monitor.wait(30):
                return
            continue
        if not waited_for_startup:
            waited_for_startup = True
            if config.startup_delay and monitor.wait(config.startup_delay):
                return
            config = kodi.load_config()
            if monitor.pop_scan_finished():
                pending_scan_update = True
        if config.pause_during_playback and kodi.is_playing_video():
            if monitor.wait(30):
                return
            continue
        if config.use_imdb_data:
            _refresh_imdb_if_due(config, monitor)
            if monitor.abort_requested():
                return
        if pending_scan_update:
            pending_scan_update = False
            if config.update_on_scan and config.schedule_types and config.any_active(_helper_available(config)):
                run_many(
                    list(config.schedule_types),
                    True,
                    scheduled=False,
                    recent_days_override=config.recent_days or 7,
                )
                if monitor.wait(30):
                    return
                continue
        if not config.schedule_enabled or not config.schedule_types:
            if monitor.wait(30):
                return
            continue
        if not config.any_active(_helper_available(config)):
            kodi.log("Scheduled ratings update skipped because no source is configured")
            if monitor.wait(1800):
                return
            continue
        if not _schedule_due(config):
            if monitor.wait(30):
                return
            continue
        run_many(list(config.schedule_types), config.schedule_background, scheduled=True)
        if monitor.wait(30):
            return


def _run_custom(types: list[str], background: bool, runner) -> None:
    config = kodi.load_config()
    if not _ready(config, scheduled=False):
        return
    if not kodi.try_lock():
        kodi.notify(kodi.text(32165), "warning")
        return
    monitor = kodi.KodiMonitor()
    progress = None
    limiter = None
    try:
        _warn_missing(config, scheduled=False)
        library = kodi.KodiLibrary()
        progress = kodi.open_progress(kodi.text(32105), background, monitor)
        sources, limiter = _sources(config, progress, monitor)
        stats = runner(library, sources, config, progress, monitor)
        if stats is None:
            kodi.notify(kodi.text(32166), "info")
            return
        _announce(stats, types, background, scheduled=False)
    except DatasetCancelled:
        _announce_cancelled(types, background, scheduled=False)
    except RunCancelled:
        _announce_cancelled(types, background, scheduled=False)
    except Exception as exc:
        _announce_error(exc, types, background, scheduled=False)
    finally:
        if limiter is not None:
            kodi.write_limits(limiter.state)
        if progress is not None:
            progress.close()
        kodi.unlock()


def run_one(mediatype: str, dbid: int, background: bool) -> None:
    def runner(library, sources, config, progress, monitor):
        item = library.get_item(mediatype, dbid)
        if item is None:
            return None
        shows = {}
        if item.mediatype == "episode" and item.tvshowid:
            show = library.get_item("tvshow", item.tvshowid)
            if show is not None:
                shows = {show.dbid: show}
        return update_items(
            [item],
            shows,
            library,
            sources,
            config,
            progress=progress,
            pause=monitor.pause,
            log=kodi.log,
            type_label=_type_label,
            item_message=_item_message,
        )

    _run_custom([mediatype], background, runner)


def run_show(tvshowid: int, background: bool) -> None:
    def runner(library, sources, config, progress, monitor):
        show = library.get_item("tvshow", tvshowid)
        if show is None:
            return None
        episodes = library.list_episodes_for_show(tvshowid)
        return update_items(
            [show, *episodes],
            {show.dbid: show},
            library,
            sources,
            config,
            progress=progress,
            pause=monitor.pause,
            log=kodi.log,
            type_label=_type_label,
            item_message=_item_message,
        )

    _run_custom(["tvshow", "episode"], background, runner)


def run_season(seasonid: int, background: bool) -> None:
    def runner(library, sources, config, progress, monitor):
        details = library.get_season(seasonid)
        if not details or not details.get("tvshowid") or details.get("season") is None:
            return None
        tvshowid = int(details["tvshowid"])
        season_num = int(details["season"])
        show = library.get_item("tvshow", tvshowid)
        shows = {show.dbid: show} if show is not None else {}
        episodes = library.list_episodes_for_show(tvshowid, season=season_num)
        if not episodes:
            return None
        return update_items(
            episodes,
            shows,
            library,
            sources,
            config,
            progress=progress,
            pause=monitor.pause,
            log=kodi.log,
            type_label=_type_label,
            item_message=_item_message,
        )

    _run_custom(["episode"], background, runner)


def run_many(types: list[str], background: bool, scheduled: bool, recent_days_override: int | None = None) -> None:
    config = kodi.load_config()
    if not _ready(config, scheduled):
        return
    if not kodi.try_lock():
        if scheduled:
            kodi.log("Scheduled ratings update skipped because another update is running")
        else:
            kodi.notify(kodi.text(32165), "warning")
        return
    monitor = kodi.KodiMonitor()
    progress = None
    limiter = None
    try:
        _warn_missing(config, scheduled)
        kodi.log("Updating ratings for " + ", ".join(types))
        progress = kodi.open_progress(kodi.text(32105), background, monitor)
        sources, limiter = _sources(config, progress, monitor)
        stats = update_types(
            types,
            kodi.KodiLibrary(),
            sources,
            config,
            progress=progress,
            pause=monitor.pause,
            log=kodi.log,
            type_label=_type_label,
            reading=lambda kind: kodi.text(32171, kind=kind),
            item_message=_item_message,
            recent_days_override=recent_days_override,
        )
        _announce(stats, types, background, scheduled)
        if scheduled and not stats.cancelled:
            kodi.write_last_run()
    except DatasetCancelled:
        _announce_cancelled(types, background, scheduled)
    except RunCancelled:
        _announce_cancelled(types, background, scheduled)
    except Exception as exc:
        _announce_error(exc, types, background, scheduled)
        if scheduled:
            kodi.write_last_run()
    finally:
        if limiter is not None:
            kodi.write_limits(limiter.state)
        if progress is not None:
            progress.close()
        kodi.unlock()


def refresh_imdb_now(background: bool) -> None:
    config = kodi.load_config()
    if not config.use_imdb_data:
        kodi.notify(kodi.text(32182), "warning")
        kodi.open_settings()
        return
    if not kodi.try_lock():
        kodi.notify(kodi.text(32165), "warning")
        return
    monitor = kodi.KodiMonitor()
    progress = None
    try:
        progress = kodi.open_progress(kodi.text(32051), background, monitor)
        data = _imdb_data(config)
        changed = _refresh_index(data, config, progress, monitor, force=True)
        message = kodi.text(32185, **_index_counts(data)) if changed else kodi.text(32186)
        _show_result(message, background, "info")
    except DatasetCancelled:
        _show_result(kodi.text(32168, **ZERO_COUNTS), background, "warning")
    except DatasetError as exc:
        kodi.log(str(exc))
        _show_result(str(exc), background, "error")
    finally:
        if progress is not None:
            progress.close()
        kodi.unlock()


def show_runs() -> None:
    runs = kodi.read_runs()
    if not runs:
        kodi.notify(kodi.text(32187), "info")
        return
    lines = []
    for run in runs:
        entry = f"{run.get('finished', '')}\n{run.get('summary', '')}"
        failures = run.get("failures") or []
        if failures:
            entry += "\n" + "\n".join(f"  - {err}" for err in failures)
        lines.append(entry)
    kodi.show_text(kodi.text(32050), "\n\n".join(lines))


def _refresh_imdb_if_due(config, monitor) -> None:
    data = _imdb_data(config)
    if not data.due(config.imdb_refresh_days):
        return
    if not kodi.try_lock():
        kodi.log("IMDb refresh skipped because another update is running")
        return
    progress = None
    try:
        progress = kodi.open_progress(kodi.text(32051), config.schedule_background, monitor)
        _refresh_index(data, config, progress, monitor, force=False)
    except DatasetCancelled:
        kodi.log("IMDb download cancelled")
    except DatasetError as exc:
        kodi.log(str(exc))
    finally:
        if progress is not None:
            progress.close()
        kodi.unlock()


def _sources(config, progress, monitor):
    index = None
    if config.use_imdb_data:
        data = _imdb_data(config)
        try:
            if data.due(config.imdb_refresh_days) or not data.ready():
                _refresh_index(data, config, progress, monitor, force=False)
        except DatasetError as exc:
            kodi.log(str(exc))
        if data.ready():
            index = data
    limiter = Limiter(
        omdb_daily=config.omdb_daily_limit,
        mdblist_daily=config.mdblist_daily_limit,
        state=kodi.read_limits(),
    )

    def sleeper(seconds: float) -> bool:
        if monitor is not None and monitor.pause(seconds):
            raise RunCancelled()
        return False

    helper = None
    try:
        from resources.lib.helper_kodi import open_helper

        helper = open_helper(config, kodi.log)
    except Exception as exc:
        kodi.log(f"TMDbHelper could not be used ({exc.__class__.__name__})")
    transport = PacedTransport(JsonClient(sleeper=sleeper), limiter, sleeper)
    return SourceClient(config, transport, index, helper), limiter


def _refresh_index(data: ImdbData, config, progress, monitor, force: bool) -> bool:
    def report(message: str) -> None:
        if progress is not None:
            progress.update(0, message)
        else:
            kodi.log(message)

    def should_stop() -> bool:
        if monitor is not None and monitor.abort_requested():
            return True
        return bool(progress is not None and progress.cancelled())

    if progress is not None:
        progress.update(0, kodi.text(32184))
    result = data.refresh(
        config.imdb_refresh_days,
        report=report,
        should_stop=should_stop,
        force=force,
        progress=(lambda pct, msg: progress.update(pct, msg)) if progress is not None else None,
    )
    if result.get("changed"):
        counts = result.get("counts") or {}
        kodi.log(kodi.text(32185, ratings=counts.get("ratings", 0), episodes=counts.get("episodes", 0)))
    else:
        kodi.log(kodi.text(32186))
    return bool(result.get("changed"))


def _imdb_data(config) -> ImdbData:
    return ImdbData(config.imdb_folder(kodi.profile_dir()), config.imdb_user_agent)


def _index_counts(data: ImdbData) -> dict:
    try:
        raw = data._meta().get("counts") or "{}"
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = {}
    return {"ratings": int(parsed.get("ratings") or 0), "episodes": int(parsed.get("episodes") or 0)}


def _schedule_due(config) -> bool:
    last = kodi.read_last_run()
    if config.schedule_mode == "interval":
        return is_due(last, config.schedule_hours, datetime.now(timezone.utc))
    return daily_due(last, parse_times(config.schedule_times), datetime.now().astimezone())


def _background_flag(params: dict) -> bool:
    if "background" in params:
        return is_truthy(params.get("background"))
    return kodi.load_config().manual_background


def _helper_available(config) -> bool:
    return bool(getattr(config, "use_tmdbhelper", True) and kodi.addon_installed("plugin.video.themoviedb.helper"))


def _ready(config, scheduled: bool) -> bool:
    if config.any_active(_helper_available(config)):
        return True
    if scheduled:
        return False
    kodi.notify(kodi.text(32163), "warning")
    kodi.open_settings()
    return False


def _warn_missing(config, scheduled: bool) -> None:
    missing = config.missing_keys(tmdbhelper_available=_helper_available(config))
    if not missing:
        return
    messages = [kodi.text(MISSING_KEY[key]) for key in missing if key in MISSING_KEY]
    if not messages:
        return
    if scheduled:
        for message in messages:
            kodi.log(message)
        return
    kodi.notify(messages[0], "warning")


def _announce(stats, types, background: bool, scheduled: bool) -> None:
    counts = _counts(stats)
    if stats.stopped:
        provider = PROVIDER_LABEL.get(stats.stopped, stats.stopped)
        message = kodi.text(32169, provider=provider, **counts)
        kind = "error"
    elif stats.cancelled:
        message = kodi.text(32168, **counts)
        kind = "warning"
    else:
        message = kodi.text(32167, **counts)
        kind = "info" if stats.total_seen == 0 or stats.updated or not stats.failed else "warning"
    kodi.log(message)
    failures = list(getattr(stats, "failures", ()) or ())
    _store_run(types, background, scheduled, counts, stats.cancelled, stats.stopped, message, failures)
    _show_result(message, background, kind)


def _announce_cancelled(types, background: bool, scheduled: bool) -> None:
    message = kodi.text(32168, **ZERO_COUNTS)
    kodi.log(message)
    _store_run(types, background, scheduled, ZERO_COUNTS, True, "", message)
    _show_result(message, background, "warning")


def _announce_error(exc: Exception, types, background: bool, scheduled: bool) -> None:
    message = str(exc)
    kodi.log(f"Ratings update failed: {message}")
    _store_run(types, background, scheduled, ZERO_COUNTS, False, "error", message)
    _show_result(message, background, "error")


def _show_result(message: str, background: bool, kind: str) -> None:
    if background:
        kodi.notify(message, kind)
        return
    kodi.show_summary(kodi.text(32105), message)


def _store_run(
    types,
    background: bool,
    scheduled: bool,
    counts: dict,
    cancelled: bool,
    stopped: str,
    summary: str,
    failures: list | None = None,
) -> None:
    record = {
        "finished": datetime.now(timezone.utc).isoformat(),
        "types": list(types),
        "background": bool(background),
        "scheduled": bool(scheduled),
        "checked": counts["checked"],
        "updated": counts["updated"],
        "unchanged": counts["unchanged"],
        "skipped": counts["skipped"],
        "failed": counts["failed"],
        "cancelled": bool(cancelled),
        "stopped": stopped or "",
        "summary": summary,
    }
    if failures:
        record["failures"] = list(failures)
    kodi.append_run(record)


def _counts(stats) -> dict:
    return {
        "checked": stats.total_seen,
        "updated": stats.updated,
        "unchanged": stats.unchanged,
        "skipped": stats.skipped,
        "failed": stats.failed,
    }


def _types_for_run(requested) -> list[str]:
    config = kodi.load_config()
    chosen = selected_types(requested, config.schedule_types)
    if chosen:
        return chosen
    if config.schedule_types:
        kodi.notify(kodi.text(32188), "warning")
    else:
        kodi.notify(kodi.text(32189), "warning")
    return []


def _type_label(name: str) -> str:
    return kodi.text(SPECS[name].label_string)


def _item_message(kind: str, seen: int, total: int, title: str) -> str:
    return kodi.text(32170, kind=kind, current=seen, total=total, title=title)
