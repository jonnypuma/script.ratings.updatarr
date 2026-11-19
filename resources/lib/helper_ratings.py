"""Turn TMDbHelper's cached details into the ratings this addon stores.

TMDbHelper keeps TMDb, OMDb, and MDbList answers in its own cache. Reading that
cache avoids asking the same service again. Scores above 10 are percentages and
are scaled onto Kodi's 0–10 range.
"""

from __future__ import annotations

from dataclasses import dataclass, field


NAMES = {
    "tmdb": "themoviedb",
    "imdb": "imdb",
    "trakt": "trakt",
    "tomatoes": "tomatometerallcritics",
    "rotten": "tomatometerallcritics",
    "tomatoesaudience": "tomatometerallaudience",
    "popcorn": "tomatometerallaudience",
    "audiencescore": "tomatometerallaudience",
    "metacritic": "metacritic",
    "letterboxd": "letterboxd",
    "mal": "mal",
    "myanimelist": "mal",
}
PERCENT = {"tomatoes", "rotten", "tomatoesaudience", "popcorn", "audiencescore", "metacritic"}
PROPERTY_NAMES = {
    "tmdb_rating": ("themoviedb", "tmdb_votes", 10),
    "imdb_rating": ("imdb", "imdb_votes", 10),
    "trakt_rating": ("trakt", "trakt_votes", 10),
    "rottentomatoes_rating": ("tomatometerallcritics", "", 100),
    "rottentomatoes_audiencerating": ("tomatometerallaudience", "", 100),
    "tomatoesaudience_rating": ("tomatometerallaudience", "", 100),
    "metacritic_rating": ("metacritic", "", 100),
    "letterboxd_rating": ("letterboxd", "letterboxd_votes", 10),
    "mal_rating": ("mal", "mal_votes", 10),
}


@dataclass
class HelperResult:
    ratings: dict = field(default_factory=dict)
    ids: dict = field(default_factory=dict)
    requested: bool = False


def ratings_from_tmdb_payload(payload) -> dict:
    from resources.lib.providers import parse_tmdb_rating

    parsed = parse_tmdb_rating(payload)
    return {"themoviedb": parsed} if parsed else {}


def ratings_from_omdb_record(record) -> dict:
    from resources.lib.providers import parse_omdb

    if not isinstance(record, dict) or not record:
        return {}
    body = dict(record)
    body.setdefault("Response", "True")
    if "Metascore" not in body and body.get("metascore") not in (None, ""):
        body["Metascore"] = body.get("metascore")
    try:
        parsed = parse_omdb(body)
    except Exception:
        parsed = {}
    tomatoes = _number(record.get("tomatoMeter"))
    if tomatoes and "tomatometerallcritics" not in parsed:
        scaled = _on_ten(tomatoes, 100)
        if scaled:
            parsed["tomatometerallcritics"] = {"rating": scaled, "votes": 0}
    return parsed


def ratings_from_mdblist(details) -> dict:
    if not isinstance(details, dict):
        return {}
    parsed = {}
    for entry in details.get("ratings") or []:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source") or "").lower()
        name = NAMES.get(source)
        if not name:
            continue
        value = _number(entry.get("value"))
        if source == "letterboxd":
            if value is not None and 0 < value <= 5:
                rating = _on_ten(value * 2.0, 10)
            else:
                score_val = _number(entry.get("score"))
                rating = _on_ten(score_val, 100) if score_val is not None else _on_ten(value, 10)
        else:
            if value is None:
                continue
            scale = 100 if source in PERCENT or value > 10 else 10
            rating = _on_ten(value, scale)
        if rating:
            votes = _votes(entry.get("votes"))
            parsed[name] = {"rating": rating, "votes": votes}
    return parsed


def ratings_from_properties(properties) -> dict:
    if not isinstance(properties, dict):
        return {}
    parsed = {}
    for key, (name, votes_key, scale) in PROPERTY_NAMES.items():
        rating = _on_ten(_number(properties.get(key)), scale)
        if not rating:
            continue
        parsed[name] = {"rating": rating, "votes": _votes(properties.get(votes_key)) if votes_key else 0}
    return parsed


def _number(value) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text.upper() == "N/A":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _votes(value) -> int:
    number = _number(value)
    if number is None or number <= 0:
        return 0
    return int(number)


def _on_ten(value: float | None, scale: int) -> float | None:
    if value is None or value <= 0:
        return None
    rating = value / 10.0 if scale == 100 or value > 10 else value
    if rating <= 0 or rating > 10:
        return None
    return float(round(rating, 3))
