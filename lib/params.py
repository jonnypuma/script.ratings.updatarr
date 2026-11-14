"""RunScript argument parsing."""

from __future__ import annotations

import re
import urllib.parse

from resources.lib.media import ALIASES, ALL_TYPES


def parse_params(argv: list[str] | tuple[str, ...]) -> dict[str, str]:
    """Accept Kodi's query-string argument and extra key=value arguments."""
    params: dict[str, str] = {}
    raw = argv[1] if len(argv) > 1 else ""
    if raw.startswith("?"):
        raw = raw[1:]
    if raw and "=" in raw:
        params.update(urllib.parse.parse_qsl(raw, keep_blank_values=True))
    for arg in argv[2:]:
        if "=" in arg:
            key, value = arg.split("=", 1)
            params[key] = value
    return {str(key): str(value) for key, value in params.items()}


def parse_media_types(value: str) -> list[str]:
    """Turn 'movies+tvshows' or 'episode' into spec names. 'all' expands.

    Query strings treat '+' as a space, so both separators are accepted.
    """
    if not value or not str(value).strip():
        return []
    chosen: list[str] = []
    for token in re.split(r"[+,|\s]+", str(value).strip().lower()):
        if not token:
            continue
        mapped = ALIASES.get(token)
        if mapped == "all":
            return list(ALL_TYPES)
        if mapped and mapped not in chosen:
            chosen.append(mapped)
    return chosen


def is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
