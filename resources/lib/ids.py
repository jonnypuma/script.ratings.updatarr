"""Read and fill Kodi unique IDs without replacing IDs the library already has."""

from __future__ import annotations


def unique_map(raw) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for key, value in raw.items():
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in {"none", "null"}:
            cleaned[str(key)] = text
    return cleaned


def lookup(uniqueids: dict, *names: str) -> str:
    lowered = {str(key).lower(): value for key, value in unique_map(uniqueids).items()}
    for name in names:
        found = lowered.get(name.lower(), "")
        if found:
            return found
    return ""


def is_imdb(value: str) -> bool:
    text = str(value or "").strip()
    return len(text) > 2 and text.startswith("tt") and text[2:].isdigit()


def is_numeric_id(value: str) -> bool:
    text = str(value or "").strip()
    return text.isdigit() and 0 < len(text) <= 12


def imdb_id(uniqueids: dict, imdbnumber: str = "") -> str:
    found = lookup(uniqueids, "imdb")
    if is_imdb(found):
        return found
    if is_imdb(imdbnumber):
        return str(imdbnumber).strip()
    return ""


def tmdb_id(uniqueids: dict, imdbnumber: str = "", *, allow_numeric_fallback: bool = False) -> str:
    found = lookup(uniqueids, "tmdb", "themoviedb")
    if is_numeric_id(found):
        return str(int(found))
    if allow_numeric_fallback and is_numeric_id(imdbnumber):
        return str(int(str(imdbnumber).strip()))
    return ""


def tvdb_id(uniqueids: dict) -> str:
    found = lookup(uniqueids, "tvdb", "thetvdb")
    if is_numeric_id(found):
        return str(int(found))
    return ""


def new_uniqueids(existing, discovered: dict) -> dict[str, str]:
    """IDs worth saving. An ID already stored under any known alias is kept as-is."""
    current = unique_map(existing)
    have = {key.lower() for key in current}
    groups = {
        "tmdb": {"tmdb", "themoviedb"},
        "imdb": {"imdb"},
        "tvdb": {"tvdb", "thetvdb"},
    }
    fresh: dict[str, str] = {}
    for key, value in (discovered or {}).items():
        if value is None:
            continue
        text = str(value).strip()
        names = groups.get(key, {key.lower()})
        if have.intersection(names):
            continue
        if key == "imdb" and not is_imdb(text):
            continue
        if key in {"tmdb", "tvdb"} and not is_numeric_id(text):
            continue
        fresh[key] = str(int(text)) if key in {"tmdb", "tvdb"} else text
    return fresh
