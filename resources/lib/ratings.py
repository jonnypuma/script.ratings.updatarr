"""Turn fetched scores into the ratings object Kodi's JSON-RPC call expects.

Kodi rejects a rating that is not greater than 0 and not greater than 10.
The TMDb rating name used by the movie scraper is "themoviedb"; older libraries
also have "tmdb". Unique IDs use "tmdb", which is a different field.
"""

from __future__ import annotations

TMDB_ALIASES = {"tmdb", "themoviedb"}
PRIORITY = ("imdb", "themoviedb", "trakt", "tomatometerallcritics", "metacritic")


def as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def normalize_existing(existing) -> dict[str, dict]:
    if not isinstance(existing, dict):
        return {}
    cleaned = {}
    for key, value in existing.items():
        if not isinstance(value, dict):
            continue
        try:
            rating = float(value.get("rating") or 0)
            votes = int(float(str(value.get("votes") or 0).replace(",", "")))
        except (TypeError, ValueError):
            continue
        cleaned[str(key)] = {
            "rating": rating,
            "votes": max(0, votes),
            "default": as_bool(value.get("default")),
        }
    return cleaned


def current_default(existing: dict) -> str | None:
    for key, value in existing.items():
        if value.get("default"):
            return key
    return None


def storage_keys_for(source: str, existing: dict) -> list[str]:
    if source == "themoviedb":
        found = [key for key in existing if key.lower() in TMDB_ALIASES]
        return found or ["themoviedb"]
    for key in existing:
        if key.lower() == source:
            return [key]
    return [source]


def _match_source(source: str, available: list[str]) -> str | None:
    if source == "themoviedb":
        for candidate in ("themoviedb", "tmdb"):
            for key in available:
                if key.lower() == candidate:
                    return key
        return None
    for key in available:
        if key.lower() == source:
            return key
    return None


def select_default_key(existing: dict, refreshed: dict, preferred: str) -> str | None:
    """Pick the rating Kodi should display.

    A preferred source is used only when this item actually has that score.
    Otherwise the current default stays. A new item with no default uses
    IMDb, then TMDb, then the other known sources.
    """
    available = list(dict.fromkeys([*existing.keys(), *refreshed.keys()]))
    if preferred and preferred != "keep":
        chosen = _match_source(preferred, available)
        if chosen:
            return chosen
    current = current_default(existing)
    if current:
        return current
    for source in PRIORITY:
        chosen = _match_source(source, available)
        if chosen:
            return chosen
    return None


def build_rating_update(
    existing,
    fetched,
    preferred: str,
    minimum_change: float = 0.2,
    missing_only: bool = False,
) -> dict | None:
    """Return the ratings to send, or None when nothing should be written.

    ``minimum_change`` of 0 writes every fetched score, including scores that
    already match the library. Any larger value writes a score only when it is
    new or it has moved by more than that amount. ``missing_only`` writes a
    score only when that source is not already stored.
    """
    if not fetched:
        return None
    try:
        threshold = float(minimum_change)
    except (TypeError, ValueError):
        threshold = 0.2
    if threshold < 0:
        threshold = 0.0
    existing_n = normalize_existing(existing)
    refreshed: dict[str, dict] = {}
    changed: set[str] = set()

    for source, incoming in fetched.items():
        if not isinstance(incoming, dict):
            continue
        try:
            rating = float(round(float(incoming.get("rating")), 3))
            votes = int(incoming.get("votes") or 0)
        except (TypeError, ValueError):
            continue
        if rating <= 0 or rating > 10:
            continue
        for key in storage_keys_for(source, existing_n):
            old = existing_n.get(key)
            use_votes = votes if votes > 0 else (old["votes"] if old else 0)
            refreshed[key] = {"rating": rating, "votes": use_votes}
            if old is None:
                moved = True
            else:
                moved = threshold <= 0 or abs(round(old["rating"], 3) - rating) > threshold
            missing = old is None or old["rating"] <= 0
            if missing or (not missing_only and moved):
                changed.add(key)

    if not refreshed:
        return None

    default_key = select_default_key(existing_n, refreshed, preferred)
    current = current_default(existing_n)
    default_changed = bool(default_key) and default_key != current
    stored_default = (
        missing_only
        and default_key in existing_n
        and existing_n[default_key]["rating"] > 0
    )
    if stored_default:
        default_changed = False
    if not changed and not default_changed:
        return None

    keys_to_send = set(changed)
    if default_changed and default_key:
        keys_to_send.add(default_key)

    payload = {}
    for key in keys_to_send:
        if key in refreshed:
            value = refreshed[key]
        elif key in existing_n:
            value = {"rating": existing_n[key]["rating"], "votes": existing_n[key]["votes"]}
        else:
            continue
        if value["rating"] <= 0 or value["rating"] > 10:
            continue
        entry = {"rating": float(value["rating"]), "votes": int(value["votes"])}
        if default_key and key == default_key:
            entry["default"] = True
        payload[key] = entry
    return payload or None
