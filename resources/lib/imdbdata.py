"""Local IMDb ratings from IMDb's own dataset files.

IMDb publishes title.ratings and title.episode as gzip TSV files for personal,
non-commercial use. This module downloads those two files for this Kodi install,
builds a small SQLite index, and answers lookups from it. The downloaded files
are removed after the index is built. A file the user placed in the import
folder is read and left untouched.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path


BASE_URL = "https://datasets.imdbws.com/"
RATINGS = "title.ratings.tsv.gz"
EPISODES = "title.episode.tsv.gz"
FILES = (RATINGS, EPISODES)
MAX_BYTES = {RATINGS: 150 * 2**20, EPISODES: 600 * 2**20}
MIN_BYTES = 20
FREE_BYTES = 800 * 2**20
SCHEMA = 1
RETRY_AFTER_FAILURE = 24 * 3600
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class DatasetError(Exception):
    """The IMDb files could not be downloaded or indexed. The previous index stays."""


class DatasetCancelled(Exception):
    """The user or Kodi stopped the download before the new index replaced the old one."""


def build_index(ratings_path: str, episodes_path: str, target: str, should_stop=None, progress=None) -> dict[str, int]:
    """Create a new index from the two dataset files. ``target`` is replaced only on success."""
    temporary = target + ".new"
    _remove(temporary)
    connection = sqlite3.connect(temporary)
    try:
        connection.executescript(
            "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-200000; "
            "PRAGMA temp_store=MEMORY; PRAGMA locking_mode=EXCLUSIVE;"
            "CREATE TABLE ratings (t INTEGER PRIMARY KEY, r INTEGER NOT NULL, v INTEGER NOT NULL);"
            "CREATE TABLE episodes ("
            "parent INTEGER NOT NULL, season INTEGER NOT NULL, number INTEGER NOT NULL, t INTEGER NOT NULL);"
            "CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);"
        )
        connection.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA),))

        def on_ratings_batch(done: int) -> None:
            if progress:
                pct = min(58, 50 + int(done * 8 / 1_800_000))
                progress(pct, f"Indexing IMDb ratings ({done:,})")

        def on_episodes_batch(done: int) -> None:
            if progress:
                pct = min(95, 58 + int(done * 37 / 1_500_000))
                progress(pct, f"Indexing IMDb episodes ({done:,})")

        rated_ids: set[int] = set()

        def tracked_ratings():
            for row in _rating_rows(ratings_path):
                rated_ids.add(row[0])
                yield row

        ratings_count = _insert(
            connection,
            "INSERT OR REPLACE INTO ratings VALUES (?, ?, ?)",
            tracked_ratings(),
            should_stop,
            on_batch=on_ratings_batch,
        )
        episodes_count = _insert(
            connection,
            "INSERT INTO episodes VALUES (?, ?, ?, ?)",
            _episode_rows(episodes_path, rated_ids),
            should_stop,
            on_batch=on_episodes_batch,
        )
        rated_ids.clear()
        if progress:
            progress(96, "Building IMDb episode index")
        connection.execute("CREATE INDEX idx_episodes_parent_season_num ON episodes (parent, season, number)")
        connection.commit()
        counts = {"ratings": ratings_count, "episodes": episodes_count}
    except DatasetCancelled:
        connection.close()
        _remove(temporary)
        raise
    except Exception:
        connection.close()
        _remove(temporary)
        raise
    else:
        connection.close()
    os.replace(temporary, target)
    return counts


class ImdbData:
    def __init__(self, folder: str, user_agent: str = "", opener=None) -> None:
        self.folder = folder
        self.user_agent = (user_agent or "").strip() or BROWSER_USER_AGENT
        self.opener = opener or urllib.request.urlopen
        self._connection = None
        self._key = None

    def rating(self, imdb_id: str) -> dict | None:
        number = _number(imdb_id)
        connection = self._db()
        if number is None or connection is None:
            return None
        row = connection.execute("SELECT r, v FROM ratings WHERE t = ?", (number,)).fetchone()
        if not row:
            return None
        return {"rating": row[0] / 10, "votes": row[1]}

    def episode(self, show_imdb: str, season: int, number: int) -> dict | None:
        parent = _number(show_imdb)
        connection = self._db()
        if parent is None or connection is None or season is None or number is None:
            return None
        try:
            row = connection.execute(
                "SELECT t FROM episodes WHERE parent = ? AND season = ? AND number = ?",
                (parent, int(season), int(number)),
            ).fetchone()
        except (sqlite3.Error, TypeError, ValueError):
            return None
        if not row:
            return None
        imdb = f"tt{row[0]:07d}"
        return {"imdb": imdb, "rating": self.rating(imdb)}

    def ready(self) -> bool:
        return self._db() is not None

    def status(self, days: int, now: float | None = None) -> dict:
        meta = self._meta()
        state = self._state()
        try:
            counts = json.loads(meta.get("counts") or "{}")
        except (TypeError, ValueError):
            counts = {}
        try:
            size_bytes = os.path.getsize(self.index_path)
        except OSError:
            size_bytes = 0
        return {
            "ready": self.ready(),
            "built_at": int(meta.get("built_at") or 0),
            "checked_at": int(meta.get("checked_at") or 0),
            "due": self.due(days, now=now),
            "size_bytes": size_bytes,
            "ratings": int(counts.get("ratings") or 0),
            "episodes": int(counts.get("episodes") or 0),
            "error": str(state.get("error") or ""),
        }

    def due(self, days: int, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        state = self._state()
        if state.get("error") and moment - float(state.get("attempt") or 0) < RETRY_AFTER_FAILURE:
            return False
        marker = self._marker() or 0
        for path in self._fresh_imports(days).values():
            try:
                if os.stat(path).st_mtime > marker:
                    return True
            except OSError:
                continue
        if marker == 0:
            return True
        return moment - marker >= int(days) * 86400

    def refresh(self, days: int = 30, report=None, should_stop=None, force: bool = False, progress=None) -> dict:
        report = report or (lambda _message: None)
        os.makedirs(self.folder, exist_ok=True)
        self._close()
        self._remember(attempt=time.time(), error="")
        sources = self._fresh_imports(days)
        downloaded: list[str] = []
        stamps: dict[str, str] = {}
        try:
            if len(sources) < len(FILES):
                if should_stop and should_stop():
                    raise DatasetCancelled()
                report("Checking IMDb for new rating files")
                if progress:
                    progress(0, "Checking IMDb for new rating files")
                remote = self._remote_stamps()
                previous = self._meta().get("stamps")
                if remote and not sources and previous == json.dumps(remote, sort_keys=True) and self.ready():
                    self._touch()
                    return {"changed": False, "reason": "current", "forced": bool(force)}
                free = shutil.disk_usage(self.folder).free
                if free < FREE_BYTES:
                    raise DatasetError("Less than 800 MB is free, which the IMDb download and index need")
                for index, name in enumerate(FILES):
                    if name in sources:
                        continue
                    path, stamp = self._download(
                        name,
                        report,
                        should_stop,
                        progress=progress,
                        base_pct=index * 25,
                        span_pct=25,
                    )
                    sources[name] = path
                    downloaded.append(path)
                    stamps[name] = stamp or remote.get(name, "")
            for name, path in sources.items():
                stamps.setdefault(name, _file_stamp(path))
            report("Indexing IMDb ratings")
            if progress:
                progress(50, "Indexing IMDb ratings")
            self._close()
            counts = build_index(sources[RATINGS], sources[EPISODES], self.index_path, should_stop, progress=progress)
            self._write_meta(
                schema=SCHEMA,
                built_at=int(time.time()),
                checked_at=int(time.time()),
                stamps=json.dumps(stamps, sort_keys=True),
                counts=json.dumps(counts),
            )
            self._remember(error="")
            return {"changed": True, "reason": "updated", "counts": counts}
        except DatasetCancelled:
            self._remember(error="cancelled")
            raise
        except DatasetError as exc:
            self._remember(error=str(exc))
            raise
        except Exception as exc:
            message = f"The IMDb data could not be built ({exc.__class__.__name__})"
            self._remember(error=message)
            raise DatasetError(message) from exc
        finally:
            for path in downloaded:
                _remove(path)
            self._close()

    @property
    def index_path(self) -> str:
        return os.path.join(self.folder, "imdb.sqlite")

    @property
    def import_dir(self) -> str:
        return os.path.join(self.folder, "import")

    def __del__(self) -> None:
        try:
            self._close()
        except Exception:
            return

    def _download(self, name: str, report, should_stop, progress=None, base_pct: int = 0, span_pct: int = 25) -> tuple[str, str]:
        target = os.path.join(self.folder, name)
        partial = target + ".part"
        cap = MAX_BYTES[name]
        request = urllib.request.Request(
            BASE_URL + name,
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity"},
        )
        try:
            with self.opener(request, timeout=60) as response:
                status = getattr(response, "status", None) or response.getcode()
                if int(status) != 200:
                    raise DatasetError(f"IMDb answered {status} for {name}")
                headers = response.headers
                expected = int(headers.get("Content-Length") or 0)
                if expected > cap:
                    raise DatasetError(f"{name} is far larger than expected; nothing was saved")
                written = 0
                with open(partial, "wb") as handle:
                    while True:
                        if should_stop and should_stop():
                            raise DatasetCancelled()
                        chunk = response.read(1 << 20)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > cap:
                            raise DatasetError(f"{name} is far larger than expected; the download was stopped")
                        handle.write(chunk)
                        msg = f"Downloading {name} ({written // 2**20} MB)"
                        report(msg)
                        if progress:
                            pct = base_pct + (int(written * span_pct / expected) if expected > 0 else 0)
                            progress(min(base_pct + span_pct, pct), msg)
                stamp = _header_stamp(headers)
            if expected and written != expected:
                raise DatasetError(f"The download of {name} was cut short")
            os.replace(partial, target)
            return target, stamp
        except DatasetCancelled:
            raise
        except DatasetError:
            raise
        except urllib.error.URLError as exc:
            raise DatasetError(f"Could not download {name} from IMDb") from exc
        finally:
            _remove(partial)

    def _remote_stamps(self) -> dict[str, str]:
        found = {}
        for name in FILES:
            request = urllib.request.Request(
                BASE_URL + name,
                method="HEAD",
                headers={"User-Agent": self.user_agent},
            )
            try:
                with self.opener(request, timeout=20) as response:
                    status = getattr(response, "status", None) or response.getcode()
                    if int(status) != 200:
                        return {}
                    stamp = _header_stamp(response.headers)
            except (urllib.error.URLError, OSError, ValueError):
                return {}
            if not stamp:
                return {}
            found[name] = stamp
        return found

    def _fresh_imports(self, days: int) -> dict[str, str]:
        found, limit = {}, time.time() - int(days) * 86400
        for name in FILES:
            path = os.path.join(self.import_dir, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if stat.st_size >= MIN_BYTES and stat.st_mtime >= limit:
                found[name] = path
        return found

    def _db(self):
        path = self.index_path
        try:
            stat = os.stat(path)
        except OSError:
            return None
        key = (stat.st_mtime_ns, stat.st_size)
        if self._connection is not None and self._key == key:
            return self._connection
        self._close()
        try:
            connection = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=5)
            row = connection.execute("SELECT v FROM meta WHERE k = 'schema'").fetchone()
            if not row or str(row[0]) != str(SCHEMA):
                connection.close()
                return None
        except sqlite3.Error:
            return None
        self._connection, self._key = connection, key
        return connection

    def _close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except sqlite3.Error:
                pass
        self._connection = None
        self._key = None

    def _meta(self) -> dict[str, str]:
        connection = self._db()
        if connection is None:
            return {}
        try:
            return {key: value for key, value in connection.execute("SELECT k, v FROM meta")}
        except sqlite3.Error:
            return {}

    def _marker(self) -> int | None:
        meta = self._meta()
        marker = max(int(meta.get("built_at") or 0), int(meta.get("checked_at") or 0))
        return marker or None

    def _touch(self) -> None:
        self._write_meta(checked_at=int(time.time()))

    def _write_meta(self, **values) -> None:
        self._close()
        connection = sqlite3.connect(self.index_path, timeout=10)
        try:
            connection.executemany(
                "INSERT OR REPLACE INTO meta VALUES (?, ?)",
                [(key, str(value)) for key, value in values.items()],
            )
            connection.commit()
        finally:
            connection.close()

    def _state(self) -> dict:
        try:
            with open(os.path.join(self.folder, "state.json"), encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _remember(self, **changes) -> None:
        data = {**self._state(), **changes}
        try:
            os.makedirs(self.folder, exist_ok=True)
            with open(os.path.join(self.folder, "state.json"), "w", encoding="utf-8") as handle:
                json.dump(data, handle)
        except OSError:
            return


def _rating_rows(path: str):
    lines = _lines(path)
    columns = _columns(next(lines), ("tconst", "averageRating", "numVotes"), RATINGS)
    for line in lines:
        parts = line.rstrip("\r\n").split("\t")
        try:
            number = _number(parts[columns[0]])
            average = round(float(parts[columns[1]]) * 10)
            votes = int(parts[columns[2]])
        except (ValueError, IndexError):
            continue
        if number is not None and average > 0:
            yield (number, average, votes)


def _episode_rows(path: str, rated_ids: set[int] | None = None):
    lines = _lines(path)
    columns = _columns(next(lines), ("tconst", "parentTconst", "seasonNumber", "episodeNumber"), EPISODES)
    for line in lines:
        parts = line.rstrip("\r\n").split("\t")
        try:
            number, parent = _number(parts[columns[0]]), _number(parts[columns[1]])
            season = int(parts[columns[2]]) if parts[columns[2]].isdigit() else None
            episode = int(parts[columns[3]]) if parts[columns[3]].isdigit() else None
        except IndexError:
            continue
        if None not in (number, parent, season, episode):
            if rated_ids is not None and number not in rated_ids:
                continue
            yield (parent, season, episode, number)


def _lines(path: str):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        yield handle.readline()
        yield from handle


def _columns(header: str, wanted: tuple[str, ...], name: str) -> list[int]:
    names = header.rstrip("\r\n").split("\t")
    try:
        return [names.index(column) for column in wanted]
    except ValueError as exc:
        raise DatasetError(f"IMDb changed the layout of {name}") from exc


def _insert(connection, sql: str, rows, should_stop, batch: int = 20000, on_batch=None) -> int:
    total, chunk, batches = 0, [], 0
    for row in rows:
        chunk.append(row)
        if len(chunk) >= batch:
            if should_stop and should_stop():
                raise DatasetCancelled()
            connection.executemany(sql, chunk)
            total += len(chunk)
            chunk = []
            batches += 1
            if on_batch and batches % 5 == 0:
                on_batch(total)
    if chunk:
        connection.executemany(sql, chunk)
        total += len(chunk)
        if on_batch:
            on_batch(total)
    return total


def _number(imdb_id) -> int | None:
    text = str(imdb_id or "").strip().lower()
    if text.startswith("tt"):
        text = text[2:]
    if text.isascii() and text.isdigit() and text:
        return int(text)
    return None


def _header_stamp(headers) -> str:
    etag = headers.get("ETag") or ""
    modified = headers.get("Last-Modified") or ""
    if not etag and not modified:
        return ""
    return f"{etag}|{modified}|{headers.get('Content-Length') or ''}"


def _file_stamp(path: str) -> str:
    stat = os.stat(path)
    return f"file|{int(stat.st_mtime)}|{stat.st_size}"


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        return
