"""Direct Kodi video database library client (MariaDB/MySQL and SQLite)."""

from __future__ import annotations

import os
import re
import sqlite3
import xml.etree.ElementTree as ET

from resources.lib.media import LibraryItem


DEFAULT_RATING_COL = {
    "movie": ("movie", "idMovie", "c05"),
    "tvshow": ("tvshow", "idShow", "c04"),
    "episode": ("episode", "idEpisode", "c03"),
}


def _import_pymysql():
    try:
        import pymysql

        return pymysql
    except ImportError:
        from resources.lib import pymysql

        return pymysql


def parse_advanced_settings(xml_text: str) -> dict | None:
    """Extract <videodatabase> connection details from Kodi's advancedsettings.xml."""
    if not xml_text or not xml_text.strip():
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    video_db = root.find("videodatabase")
    if video_db is None:
        return None
    db_type = (video_db.findtext("type") or "").strip().lower()
    if not db_type:
        return None
    port_text = (video_db.findtext("port") or "").strip()
    try:
        port = int(port_text) if port_text else 3306
    except ValueError:
        port = 3306
    return {
        "type": db_type,
        "host": (video_db.findtext("host") or "localhost").strip(),
        "port": port,
        "name": (video_db.findtext("name") or "").strip(),
        "user": (video_db.findtext("user") or "").strip(),
        "pass": video_db.findtext("pass") or "",
    }


def find_latest_sqlite_db(database_dir: str) -> str:
    """Return the highest-schema MyVideos*.db file inside ``database_dir``."""
    if not database_dir or not os.path.isdir(database_dir):
        return ""
    candidates: list[tuple[int, str]] = []
    for name in os.listdir(database_dir):
        match = re.fullmatch(r"MyVideos(\d+)\.db", name, re.IGNORECASE)
        if match:
            candidates.append((int(match.group(1)), os.path.join(database_dir, name)))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


class SQLiteBackend:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.conn: sqlite3.Connection | None = None

    def connect(self) -> SQLiteBackend:
        if not self.db_path or not os.path.isfile(self.db_path):
            raise FileNotFoundError(f"SQLite database not found: {self.db_path}")
        self.conn = sqlite3.connect(self.db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        return self

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def describe(self) -> str:
        return f"SQLite: {self.db_path}"

    def query(self, sql: str, params=None) -> list[dict]:
        assert self.conn is not None
        cur = self.conn.cursor()
        try:
            cur.execute(sql.replace("%s", "?"), tuple(params or ()))
            return [dict(row) for row in cur.fetchall()]
        finally:
            cur.close()

    def execute(self, sql: str, params=None) -> int:
        assert self.conn is not None
        cur = self.conn.cursor()
        try:
            cur.execute(sql.replace("%s", "?"), tuple(params or ()))
            return int(cur.lastrowid or 0)
        finally:
            cur.close()

    def commit(self) -> None:
        if self.conn is not None:
            self.conn.commit()

    def rollback(self) -> None:
        if self.conn is not None:
            self.conn.rollback()


class MySQLBackend:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        db_name: str,
        max_schema: int | None = None,
    ) -> None:
        self.host = host or "localhost"
        self.port = int(port or 3306)
        self.user = user
        self.password = password
        self.db_name = db_name or "MyVideos"
        self.max_schema = max_schema
        self.resolved_db_name: str = ""
        self.conn = None

    def resolve_database(self) -> str:
        pymysql = _import_pymysql()
        temp = pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            connect_timeout=10,
            charset="utf8mb4",
        )
        try:
            cur = temp.cursor()
            cur.execute("SHOW DATABASES;")
            all_dbs = [str(row[0]) for row in cur.fetchall()]
        finally:
            temp.close()

        for existing in all_dbs:
            if existing.lower() == self.db_name.lower():
                self.resolved_db_name = existing
                return existing

        base = re.sub(r"\d+$", "", self.db_name) if not any(c.isdigit() for c in self.db_name[-1:]) else self.db_name
        pattern = re.compile(r"^" + re.escape(base) + r"(\d+)$", re.IGNORECASE)
        candidates: list[tuple[int, str]] = []
        for existing in all_dbs:
            match = pattern.match(existing)
            if match:
                candidates.append((int(match.group(1)), existing))
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            if self.max_schema is not None:
                scoped = [c for c in candidates if c[0] <= self.max_schema]
                if scoped:
                    self.resolved_db_name = scoped[0][1]
                    return self.resolved_db_name
            self.resolved_db_name = candidates[0][1]
            return self.resolved_db_name

        self.resolved_db_name = self.db_name
        return self.resolved_db_name

    def connect(self) -> MySQLBackend:
        pymysql = _import_pymysql()
        if not self.resolved_db_name:
            self.resolve_database()
        self.conn = pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.resolved_db_name,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
            connect_timeout=10,
            charset="utf8mb4",
        )
        return self

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def describe(self) -> str:
        db = self.resolved_db_name or self.db_name
        return f"MySQL: {db} @ {self.host}:{self.port}"

    def query(self, sql: str, params=None) -> list[dict]:
        assert self.conn is not None
        cur = self.conn.cursor()
        try:
            cur.execute(sql.replace("?", "%s"), tuple(params or ()))
            return list(cur.fetchall())
        finally:
            cur.close()

    def execute(self, sql: str, params=None) -> int:
        assert self.conn is not None
        cur = self.conn.cursor()
        try:
            cur.execute(sql.replace("?", "%s"), tuple(params or ()))
            return int(cur.lastrowid or 0)
        finally:
            cur.close()

    def commit(self) -> None:
        if self.conn is not None:
            self.conn.commit()

    def rollback(self) -> None:
        if self.conn is not None:
            self.conn.rollback()


class DatabaseLibrary:
    """Video library reader/writer talking directly to Kodi's SQL database."""

    def __init__(self, backend) -> None:
        self.backend = backend

    def close(self) -> None:
        self.backend.close()

    def describe(self) -> str:
        return self.backend.describe()

    def list_items(self, mediatype: str) -> list[LibraryItem]:
        rows = self._select_items(mediatype)
        return self._hydrate(mediatype, rows)

    def get_item(self, mediatype: str, dbid: int) -> LibraryItem | None:
        rows = self._select_items(mediatype, dbid=int(dbid))
        if not rows:
            return None
        return self._hydrate(mediatype, rows, single_id=int(dbid))[0]

    def list_episodes_for_show(self, tvshowid: int, season: int | None = None) -> list[LibraryItem]:
        where = ["e.idShow = %s"]
        params: list = [int(tvshowid)]
        if season is not None:
            where.append("CAST(e.c12 AS INTEGER) = %s" if isinstance(self.backend, SQLiteBackend) else "CAST(e.c12 AS SIGNED) = %s")
            params.append(int(season))
        sql = self._episode_sql(" WHERE " + " AND ".join(where))
        rows = self.backend.query(sql, params)
        return self._hydrate("episode", rows)

    def get_season(self, seasonid: int) -> dict | None:
        rows = self.backend.query(
            "SELECT se.idSeason, se.idShow AS tvshowid, se.season, t.c00 AS showtitle "
            "FROM seasons se LEFT JOIN tvshow t ON t.idShow = se.idShow "
            "WHERE se.idSeason = %s",
            (int(seasonid),),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "season": int(row["season"]) if row.get("season") is not None else None,
            "tvshowid": int(row["tvshowid"]) if row.get("tvshowid") is not None else None,
            "showtitle": str(row.get("showtitle") or ""),
        }

    def apply(self, item: LibraryItem, ratings: dict, uniqueids: dict) -> None:
        table, id_col, default_col = DEFAULT_RATING_COL[item.mediatype]
        media_id = int(item.dbid)
        try:
            for rating_type, entry in (ratings or {}).items():
                if not isinstance(entry, dict):
                    continue
                score = float(entry.get("rating") or 0)
                if score <= 0 or score > 10:
                    continue
                votes = max(0, int(entry.get("votes") or 0))
                existing = self.backend.query(
                    "SELECT rating_id FROM rating WHERE media_id = %s AND media_type = %s AND rating_type = %s",
                    (media_id, item.mediatype, str(rating_type)),
                )
                if existing:
                    rating_id = int(existing[0]["rating_id"])
                    self.backend.execute(
                        "UPDATE rating SET rating = %s, votes = %s WHERE rating_id = %s",
                        (score, votes, rating_id),
                    )
                else:
                    rating_id = self.backend.execute(
                        "INSERT INTO rating (media_id, media_type, rating_type, rating, votes) VALUES (%s, %s, %s, %s, %s)",
                        (media_id, item.mediatype, str(rating_type), score, votes),
                    )
                if entry.get("default") and rating_id:
                    self.backend.execute(
                        f"UPDATE {table} SET {default_col} = %s WHERE {id_col} = %s",
                        (str(rating_id), media_id),
                    )

            for uid_type, uid_val in (uniqueids or {}).items():
                val = str(uid_val or "").strip()
                if not val:
                    continue
                existing_uid = self.backend.query(
                    "SELECT uniqueid_id FROM uniqueid WHERE media_id = %s AND media_type = %s AND type = %s",
                    (media_id, item.mediatype, str(uid_type)),
                )
                if not existing_uid:
                    self.backend.execute(
                        "INSERT INTO uniqueid (media_id, media_type, value, type) VALUES (%s, %s, %s, %s)",
                        (media_id, item.mediatype, val, str(uid_type)),
                    )
            self.backend.commit()
        except Exception:
            self.backend.rollback()
            raise

    def _select_items(self, mediatype: str, dbid: int | None = None) -> list[dict]:
        if mediatype == "movie":
            sql = (
                "SELECT m.idMovie AS dbid, m.c00 AS title, m.c05 AS default_rating_id, m.c09 AS default_uid_id, "
                "f.dateAdded AS dateadded, p.strPath AS folder, f.strFilename AS filename "
                "FROM movie m LEFT JOIN files f ON f.idFile = m.idFile LEFT JOIN path p ON p.idPath = f.idPath"
            )
            if dbid is not None:
                return self.backend.query(sql + " WHERE m.idMovie = %s", (dbid,))
            return self.backend.query(sql + " ORDER BY m.idMovie")
        if mediatype == "tvshow":
            sql = (
                "SELECT s.idShow AS dbid, s.c00 AS title, s.c04 AS default_rating_id, s.c12 AS default_uid_id, "
                "MIN(p.strPath) AS folder "
                "FROM tvshow s LEFT JOIN tvshowlinkpath lp ON lp.idShow = s.idShow LEFT JOIN path p ON p.idPath = lp.idPath"
            )
            if dbid is not None:
                return self.backend.query(
                    sql + " WHERE s.idShow = %s GROUP BY s.idShow, s.c00, s.c04, s.c12",
                    (dbid,),
                )
            return self.backend.query(sql + " GROUP BY s.idShow, s.c00, s.c04, s.c12 ORDER BY s.idShow")
        if mediatype == "episode":
            where = " WHERE e.idEpisode = %s" if dbid is not None else ""
            params = (dbid,) if dbid is not None else ()
            return self.backend.query(self._episode_sql(where), params)
        raise KeyError(mediatype)

    def _episode_sql(self, where_clause: str = "") -> str:
        return (
            "SELECT e.idEpisode AS dbid, e.c00 AS title, e.c03 AS default_rating_id, e.c20 AS default_uid_id, "
            "e.c12 AS season, e.c13 AS episode, e.idShow AS tvshowid, "
            "t.c00 AS showtitle, f.dateAdded AS dateadded, p.strPath AS folder, f.strFilename AS filename "
            "FROM episode e "
            "LEFT JOIN tvshow t ON t.idShow = e.idShow "
            "LEFT JOIN files f ON f.idFile = e.idFile "
            "LEFT JOIN path p ON p.idPath = f.idPath"
            f"{where_clause} ORDER BY e.idEpisode"
        )

    def _hydrate(self, mediatype: str, rows: list[dict], single_id: int | None = None) -> list[LibraryItem]:
        if not rows:
            return []
        if single_id is not None:
            rating_rows = self.backend.query(
                "SELECT rating_id, media_id, rating_type, rating, votes FROM rating WHERE media_type = %s AND media_id = %s",
                (mediatype, single_id),
            )
            uid_rows = self.backend.query(
                "SELECT uniqueid_id, media_id, value, type FROM uniqueid WHERE media_type = %s AND media_id = %s",
                (mediatype, single_id),
            )
        else:
            rating_rows = self.backend.query(
                "SELECT rating_id, media_id, rating_type, rating, votes FROM rating WHERE media_type = %s",
                (mediatype,),
            )
            uid_rows = self.backend.query(
                "SELECT uniqueid_id, media_id, value, type FROM uniqueid WHERE media_type = %s",
                (mediatype,),
            )

        ratings_by_id: dict[int, list[dict]] = {}
        for r in rating_rows:
            ratings_by_id.setdefault(int(r["media_id"]), []).append(r)

        uids_by_id: dict[int, list[dict]] = {}
        for u in uid_rows:
            uids_by_id.setdefault(int(u["media_id"]), []).append(u)

        items: list[LibraryItem] = []
        for row in rows:
            dbid = int(row["dbid"])
            default_rating_id = str(row.get("default_rating_id") or "").strip()
            default_uid_id = str(row.get("default_uid_id") or "").strip()

            item_ratings: dict[str, dict] = {}
            for r in ratings_by_id.get(dbid, ()):
                rtype = str(r.get("rating_type") or "").strip()
                if not rtype:
                    continue
                item_ratings[rtype] = {
                    "rating": float(r.get("rating") or 0),
                    "votes": int(r.get("votes") or 0),
                    "default": bool(default_rating_id and str(r.get("rating_id")) == default_rating_id),
                }

            item_uids: dict[str, str] = {}
            imdbnumber = ""
            for u in uids_by_id.get(dbid, ()):
                utype = str(u.get("type") or "").strip()
                uval = str(u.get("value") or "").strip()
                if utype and uval:
                    item_uids[utype] = uval
                    if default_uid_id and str(u.get("uniqueid_id")) == default_uid_id:
                        imdbnumber = uval

            folder = str(row.get("folder") or "")
            filename = str(row.get("filename") or "")
            file_path = folder if mediatype == "tvshow" else (folder + filename)
            items.append(
                LibraryItem(
                    mediatype=mediatype,
                    dbid=dbid,
                    title=str(row.get("title") or ""),
                    uniqueids=item_uids,
                    ratings=item_ratings,
                    imdbnumber=imdbnumber,
                    season=_opt_int(row.get("season")),
                    episode=_opt_int(row.get("episode")),
                    showtitle=str(row.get("showtitle") or ""),
                    tvshowid=_opt_int(row.get("tvshowid")),
                    dateadded=str(row.get("dateadded") or ""),
                    file=file_path,
                )
            )
        return items


def _opt_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
