"""Apply fetched ratings to library items."""

from __future__ import annotations

from dataclasses import dataclass, field

from resources.lib.config import Config
from resources.lib.ids import new_uniqueids
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
        groups.append((name, items))

    shows = {item.dbid: item for item in lists.get("tvshow", [])}
    if "episode" in types and "tvshow" not in lists:
        if progress.cancelled():
            stats.cancelled = True
            return stats
        progress.update(0, reading(type_label("tvshow")))
        shows = {item.dbid: item for item in library.list_items("tvshow")}

    return _walk(groups, shows, library, sources, config, progress, pause, log, type_label, item_message, stats)


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
    )


def _walk(groups, shows, library, sources, config, progress, pause, log, type_label, item_message, stats) -> Stats:
    total = sum(len(items) for _name, items in groups)
    if total == 0:
        return stats
    seen = 0
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
            try:
                requested = _update_item(item, shows, library, sources, config, stats, log)
            except AuthError as exc:
                stats.stopped = exc.provider
                log(f"Stopped: {exc.provider} rejected the API key")
                return stats
            except RunCancelled:
                stats.cancelled = True
                return stats
            if requested and seen < total and config.delay_seconds > 0:
                if pause(config.delay_seconds):
                    stats.cancelled = True
                    return stats
    return stats


def _update_item(item, shows, library, sources, config, stats, log) -> bool:
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
    )
    uniqueids = new_uniqueids(item.uniqueids, fetched.discovered_ids) if config.save_ids else {}
    if rating_payload or uniqueids:
        try:
            library.apply(item, rating_payload or {}, uniqueids)
        except Exception as exc:
            stats.failed += 1
            _remember(stats, log, f"{item.label()}: {exc}")
            return fetched.requested
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


def _remember(stats: Stats, log, message: str) -> None:
    log(message)
    if len(stats.failures) < 20:
        stats.failures.append(message)
