"""Apply fetched ratings to library items."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from resources.lib.config import Config
from resources.lib.ids import imdb_id, new_uniqueids, tmdb_id, tvdb_id
from resources.lib.media import SPECS
from resources.lib.providers import AuthError, SourceClient
from resources.lib.limits import RunCancelled
from resources.lib.ratings import build_rating_update


@dataclass
class Stats:
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: bool = False
    stopped: str = ""
    failures: list = field(default_factory=list)
    changes: list = field(default_factory=list)

    @property
    def total_seen(self) -> int:
        return self.updated + self.unchanged + self.skipped + self.failed


class NullProgress:
    def cancelled(self) -> bool:
        return False

    def update(self, percent: int, message: str) -> None:
        return None

    def close(self) -> None:
        return None


def update_types(
    types,
    library,
    sources: SourceClient,
    config: Config,
    progress=None,
    pause=None,
    log=None,
    type_label=None,
    reading=None,
    item_message=None,
    recent_days_override: int | None = None,
    now: datetime | None = None,
    nfo_writer=None,
) -> Stats:
    progress = progress or NullProgress()
    pause = pause or (lambda _seconds: False)
    log = log or (lambda _message: None)
    type_label = type_label or (lambda name: SPECS[name].plural_label)
    reading = reading or (lambda kind: f"Reading {kind}")
    item_message = item_message or (
        lambda kind, seen, total, title: f"{kind} {seen}/{total} - {title}"
    )
    stats = Stats()
    lists: dict[str, list] = {}
    recent_days = int(getattr(config, "recent_days", 0) or 0) if recent_days_override is None else int(recent_days_override)

    def load(name: str):
        if name not in lists:
            if progress.cancelled():
                return []
            progress.update(0, reading(type_label(name)))
            lists[name] = library.list_items(name)
        return lists[name]

    groups = []
    for name in types:
        if name not in SPECS:
            continue
        items = load(name)
        if progress.cancelled():
            stats.cancelled = True
            return stats
        filtered = _filter_recent(items, recent_days, now=now)
        ordered = _prioritize_for_quota(filtered, config)
        groups.append((name, ordered))

    shows = {item.dbid: item for item in lists.get("tvshow", [])}
    if "episode" in types and "tvshow" not in lists:
        if progress.cancelled():
            stats.cancelled = True
            return stats
        progress.update(0, reading(type_label("tvshow")))
        shows = {item.dbid: item for item in library.list_items("tvshow")}

    return _walk(groups, shows, library, sources, config, progress, pause, log, type_label, item_message, stats, nfo_writer=nfo_writer)


def update_items(
    items,
    shows,
    library,
    sources: SourceClient,
    config: Config,
    progress=None,
    pause=None,
    log=None,
    type_label=None,
    item_message=None,
    nfo_writer=None,
) -> Stats:
    """Update an explicit item list. Used for one selected library item."""
    progress = progress or NullProgress()
    pause = pause or (lambda _seconds: False)
    log = log or (lambda _message: None)
    type_label = type_label or (lambda name: SPECS[name].plural_label)
    item_message = item_message or (
        lambda kind, seen, total, title: f"{kind} {seen}/{total} - {title}"
    )
    stats = Stats()
    return _walk(
        [(item.mediatype, [item]) for item in items],
        shows,
        library,
        sources,
        config,
        progress,
        pause,
        log,
        type_label,
        item_message,
        stats,
        nfo_writer=nfo_writer,
    )


def _walk(groups, shows, library, sources, config, progress, pause, log, type_label, item_message, stats, nfo_writer=None) -> Stats:
    total = sum(len(items) for _name, items in groups)
    if total == 0:
        return stats
    seen = 0
    saved_show_ids: set[int] = set()
    for name, items in groups:
        for item in items:
            if progress.cancelled():
                stats.cancelled = True
                return stats
            seen += 1
            progress.update(
                int(seen * 100 / total),
                item_message(type_label(name), seen, total, item.label()),
            )
            show = shows.get(item.tvshowid) if item.mediatype == "episode" else None
            before_show_ids = (
                dict(show.uniqueids)
                if show is not None and config.save_ids and not getattr(config, "dry_run", False) and show.dbid not in saved_show_ids
                else None
            )
            try:
                requested = _update_item(item, shows, library, sources, config, stats, log, nfo_writer=nfo_writer)
            except AuthError as exc:
                stats.stopped = exc.provider
                log(f"Stopped: {exc.provider} rejected the API key")
                return stats
            except RunCancelled:
                stats.cancelled = True
                return stats
            finally:
                if before_show_ids is not None and show is not None:
                    new_show_uid = new_uniqueids(before_show_ids, show.uniqueids)
                    if new_show_uid:
                        try:
                            library.apply(show, {}, new_show_uid)
                        except Exception as exc:
                            log(f"{show.label()}: could not save discovered IDs ({exc})")
                        saved_show_ids.add(show.dbid)
            if requested and seen < total and config.delay_seconds > 0:
                if pause(config.delay_seconds):
                    stats.cancelled = True
                    return stats
    return stats


def _update_item(item, shows, library, sources, config, stats, log, nfo_writer=None) -> bool:
    show = shows.get(item.tvshowid) if item.mediatype == "episode" else None
    try:
        fetched = sources.ratings_for(item, show)
    except AuthError:
        raise
    except RunCancelled:
        raise
    except Exception as exc:
        stats.failed += 1
        _remember(stats, log, f"{item.label()}: {exc}")
        return False

    for error in fetched.errors:
        log(f"{item.label()}: {error}")

    rating_payload = build_rating_update(
        item.ratings,
        fetched.ratings,
        config.preferred_default,
        getattr(config, "minimum_change", 0.2),
        getattr(config, "update_missing_only", False),
        getattr(config, "vote_change_percent", 0),
    )
    uniqueids = new_uniqueids(item.uniqueids, fetched.discovered_ids) if config.save_ids else {}
    if rating_payload or uniqueids:
        change_line = _format_change(item, rating_payload or {}, uniqueids)
        if not getattr(config, "dry_run", False):
            try:
                library.apply(item, rating_payload or {}, uniqueids)
            except Exception as exc:
                stats.failed += 1
                _remember(stats, log, f"{item.label()}: {exc}")
                return fetched.requested
            if getattr(config, "write_nfo", False) and nfo_writer is not None:
                try:
                    nfo_writer(item, rating_payload or {}, uniqueids)
                except Exception as exc:
                    log(f"{item.label()}: could not update .nfo ({exc})")
        if len(stats.changes) < 200:
            stats.changes.append(change_line)
        stats.updated += 1
        return fetched.requested
    if fetched.errors and not fetched.ratings:
        stats.failed += 1
        _remember(stats, log, f"{item.label()}: {fetched.errors[0]}")
        return fetched.requested
    if fetched.ratings:
        stats.unchanged += 1
        return fetched.requested
    stats.skipped += 1
    return fetched.requested


def _format_change(item, rating_payload: dict, uniqueids: dict) -> str:
    parts = []
    existing = item.ratings or {}
    for key, val in rating_payload.items():
        new_score = val.get("rating")
        old_entry = existing.get(key)
        if isinstance(old_entry, dict) and float(old_entry.get("rating") or 0) > 0:
            parts.append(f"{key} {float(old_entry['rating']):.1f}->{float(new_score):.1f}")
        else:
            parts.append(f"+{key} {float(new_score):.1f}")
    for key in uniqueids:
        parts.append(f"+id:{key}")
    return f"{item.label()}: {', '.join(parts)}"


def _remember(stats: Stats, log, message: str) -> None:
    log(message)
    if len(stats.failures) < 20:
        stats.failures.append(message)


def _filter_recent(items: list, days: int, now: datetime | None = None) -> list:
    if days <= 0:
        return list(items)
    reference = now or datetime.now()
    if reference.tzinfo is not None:
        reference = reference.replace(tzinfo=None)
    cutoff = reference - timedelta(days=days)
    kept = []
    for item in items:
        added = _parse_dateadded(getattr(item, "dateadded", ""))
        if added is None or added >= cutoff:
            kept.append(item)
    return kept


def _parse_dateadded(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
    except ValueError:
        return None


def _prioritize_for_quota(items: list, config: Config) -> list:
    omdb_on = getattr(config, "omdb_active", False)
    mdblist_on = getattr(config, "mdblist_active", False)
    if (not omdb_on and not mdblist_on) or getattr(config, "update_missing_only", False):
        return list(items)
    wanted: set[str] = set()
    if omdb_on:
        wanted.update({"tomatometerallcritics", "metacritic"})
    if mdblist_on:
        wanted.update({"tomatometerallcritics", "tomatometerallaudience", "metacritic", "letterboxd"})

    def sort_key(item) -> int:
        has_id = bool(
            imdb_id(item.uniqueids, item.imdbnumber)
            or tmdb_id(item.uniqueids, item.imdbnumber, allow_numeric_fallback=(item.mediatype == "movie"))
            or tvdb_id(item.uniqueids)
            or item.mediatype == "episode"
        )
        if not has_id:
            return 1
        existing_keys = {str(k).lower() for k, v in (item.ratings or {}).items() if isinstance(v, dict) and float(v.get("rating") or 0) > 0}
        return 0 if wanted - existing_keys else 1

    return sorted(items, key=sort_key)
