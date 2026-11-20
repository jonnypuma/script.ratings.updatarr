"""Kodi video types that store a named rating."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MediaSpec:
    name: str
    label_string: int
    item_string: int
    all_string: int
    list_method: str
    list_key: str
    details_method: str
    details_key: str
    id_field: str
    set_method: str
    properties: tuple[str, ...]
    source_kind: str
    plural_label: str


MEDIA: tuple[MediaSpec, ...] = (
    MediaSpec(
        name="movie",
        label_string=32100,
        item_string=32150,
        all_string=32151,
        list_method="VideoLibrary.GetMovies",
        list_key="movies",
        details_method="VideoLibrary.GetMovieDetails",
        details_key="moviedetails",
        id_field="movieid",
        set_method="VideoLibrary.SetMovieDetails",
        properties=("title", "uniqueid", "ratings", "imdbnumber", "dateadded", "file"),
        source_kind="movie",
        plural_label="Movies",
    ),
    MediaSpec(
        name="tvshow",
        label_string=32101,
        item_string=32152,
        all_string=32153,
        list_method="VideoLibrary.GetTVShows",
        list_key="tvshows",
        details_method="VideoLibrary.GetTVShowDetails",
        details_key="tvshowdetails",
        id_field="tvshowid",
        set_method="VideoLibrary.SetTVShowDetails",
        properties=("title", "uniqueid", "ratings", "imdbnumber", "dateadded", "file"),
        source_kind="tv",
        plural_label="TV shows",
    ),
    MediaSpec(
        name="episode",
        label_string=32102,
        item_string=32154,
        all_string=32155,
        list_method="VideoLibrary.GetEpisodes",
        list_key="episodes",
        details_method="VideoLibrary.GetEpisodeDetails",
        details_key="episodedetails",
        id_field="episodeid",
        set_method="VideoLibrary.SetEpisodeDetails",
        properties=("title", "showtitle", "season", "episode", "tvshowid", "uniqueid", "ratings", "dateadded", "file"),
        source_kind="episode",
        plural_label="Episodes",
    ),
)

SPECS: dict[str, MediaSpec] = {spec.name: spec for spec in MEDIA}
ALL_TYPES: tuple[str, ...] = tuple(spec.name for spec in MEDIA)

ALIASES = {
    "movie": "movie",
    "movies": "movie",
    "tvshow": "tvshow",
    "tvshows": "tvshow",
    "show": "tvshow",
    "shows": "tvshow",
    "episode": "episode",
    "episodes": "episode",
    "all": "all",
    "video": "all",
    "everything": "all",
}


@dataclass
class LibraryItem:
    mediatype: str
    dbid: int
    title: str = ""
    uniqueids: dict | None = None
    ratings: dict | None = None
    imdbnumber: str = ""
    season: int | None = None
    episode: int | None = None
    showtitle: str = ""
    tvshowid: int | None = None
    dateadded: str = ""
    file: str = ""

    def __post_init__(self) -> None:
        self.uniqueids = dict(self.uniqueids or {})
        self.ratings = dict(self.ratings or {})
        self.imdbnumber = str(self.imdbnumber or "")
        self.dateadded = str(self.dateadded or "")
        self.file = str(self.file or "")

    def label(self) -> str:
        if self.mediatype == "episode":
            parts = []
            if self.showtitle:
                parts.append(self.showtitle)
            if self.season is not None and self.episode is not None:
                parts.append(f"S{self.season:02d}E{self.episode:02d}")
            if self.title:
                parts.append(self.title)
            return " - ".join(parts) or str(self.dbid)
        return self.title or str(self.dbid)


def item_from_json(mediatype: str, payload: dict) -> LibraryItem:
    spec = SPECS[mediatype]
    season = payload.get("season")
    episode = payload.get("episode")
    tvshowid = payload.get("tvshowid")
    ratings = payload.get("ratings")
    uniqueids = payload.get("uniqueid")
    return LibraryItem(
        mediatype=mediatype,
        dbid=int(payload.get(spec.id_field) or 0),
        title=str(payload.get("title") or payload.get("label") or ""),
        uniqueids=uniqueids if isinstance(uniqueids, dict) else {},
        ratings=ratings if isinstance(ratings, dict) else {},
        imdbnumber=str(payload.get("imdbnumber") or ""),
        season=_optional_int(season),
        episode=_optional_int(episode),
        showtitle=str(payload.get("showtitle") or ""),
        tvshowid=int(tvshowid) if _optional_int(tvshowid) else None,
        dateadded=str(payload.get("dateadded") or ""),
        file=str(payload.get("file") or ""),
    )


def details_params(item: LibraryItem, ratings: dict | None, uniqueids: dict | None) -> dict:
    """JSON-RPC params for a ratings write. Personal user ratings are not included."""
    spec = SPECS[item.mediatype]
    params: dict = {spec.id_field: int(item.dbid)}
    if ratings:
        params["ratings"] = ratings
    if uniqueids:
        params["uniqueid"] = uniqueids
    return params


def _optional_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
