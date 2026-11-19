"""Ratings Updatarr tests. They run without Kodi."""

from __future__ import annotations

import gzip
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from resources.lib.config import Config, selected_types
from resources.lib.history import remember
from resources.lib.httpclient import HttpStatus, JsonClient
from resources.lib.imdbdata import DatasetCancelled, ImdbData, build_index
from resources.lib.ids import imdb_id, new_uniqueids, tmdb_id
from resources.lib.library import JsonRpcError, LibraryClient, parse_rpc_payload
from resources.lib.media import LibraryItem, details_params
from resources.lib.params import is_truthy, parse_media_types, parse_params
from resources.lib.providers import AuthError, SourceClient, parse_mdblist, parse_omdb, parse_tmdb_rating, parse_trakt
from resources.lib.ratings import build_rating_update
from resources.lib.schedule import daily_due, is_due, parse_times
from resources.lib.updater import update_items, update_types


def movie(**kwargs) -> LibraryItem:
    payload = {"mediatype": "movie", "dbid": 1, "title": "Example"}
    payload.update(kwargs)
    return LibraryItem(**payload)


class RatingsTests(unittest.TestCase):
    def test_scales_rotten_tomatoes_and_metacritic_onto_ten(self):
        parsed = parse_omdb(
            {
                "Response": "True",
                "imdbRating": "8.2",
                "imdbVotes": "1,234,567",
                "Metascore": "74",
                "Ratings": [
                    {"Source": "Internet Movie Database", "Value": "8.2/10"},
                    {"Source": "Rotten Tomatoes", "Value": "85%"},
                    {"Source": "Metacritic", "Value": "74/100"},
                ],
            }
        )
        self.assertEqual(parsed["imdb"], {"rating": 8.2, "votes": 1234567})
        self.assertEqual(parsed["tomatometerallcritics"]["rating"], 8.5)
        self.assertEqual(parsed["metacritic"]["rating"], 7.4)

    def test_omdb_invalid_key_is_an_auth_failure(self):
        with self.assertRaises(AuthError):
            parse_omdb({"Response": "False", "Error": "Invalid API key!"})

    def test_tmdb_ignores_an_unrated_title(self):
        self.assertIsNone(parse_tmdb_rating({"vote_average": 0, "vote_count": 0}))
        self.assertEqual(parse_tmdb_rating({"vote_average": 7.25, "vote_count": 12})["votes"], 12)

    def test_trakt_requires_votes(self):
        self.assertIsNone(parse_trakt({"rating": 8, "votes": 0}))
        self.assertEqual(parse_trakt({"rating": 8.25, "votes": 4})["rating"], 8.25)

    def test_updates_existing_tmdb_alias_and_keeps_zero_vote_history(self):
        payload = build_rating_update(
            {
                "tmdb": {"rating": 6.0, "votes": 40, "default": True},
                "tomatometerallcritics": {"rating": 7.0, "votes": 12, "default": False},
            },
            {
                "themoviedb": {"rating": 6.4, "votes": 41},
                "tomatometerallcritics": {"rating": 8.5, "votes": 0},
            },
            "keep",
        )
        self.assertEqual(payload["tmdb"]["rating"], 6.4)
        self.assertEqual(payload["tmdb"]["votes"], 41)
        self.assertTrue(payload["tmdb"]["default"])
        self.assertNotIn("themoviedb", payload)
        self.assertEqual(payload["tomatometerallcritics"]["votes"], 12)

    def test_new_tmdb_rating_uses_the_scraper_name(self):
        payload = build_rating_update({}, {"themoviedb": {"rating": 7.1, "votes": 9}}, "keep")
        self.assertEqual(payload["themoviedb"]["rating"], 7.1)
        self.assertTrue(payload["themoviedb"]["default"])

    def test_preferred_source_becomes_default_only_when_present(self):
        payload = build_rating_update(
            {
                "imdb": {"rating": 8.0, "votes": 10, "default": True},
                "themoviedb": {"rating": 7.0, "votes": 10, "default": False},
            },
            {"themoviedb": {"rating": 7.5, "votes": 11}},
            "trakt",
        )
        self.assertNotIn("default", payload["themoviedb"])
        switched = build_rating_update(
            {
                "imdb": {"rating": 8.0, "votes": 10, "default": False},
                "themoviedb": {"rating": 7.0, "votes": 10, "default": True},
            },
            {"imdb": {"rating": 8.0, "votes": 10}, "themoviedb": {"rating": 7.3, "votes": 11}},
            "imdb",
        )
        self.assertTrue(switched["imdb"]["default"])
        self.assertNotIn("default", switched["themoviedb"])

    def test_unchanged_rating_is_not_written(self):
        self.assertIsNone(
            build_rating_update(
                {"imdb": {"rating": 8.0, "votes": 10, "default": True}},
                {"imdb": {"rating": 8.0, "votes": 10}},
                "keep",
            )
        )

    def test_small_change_and_vote_change_stay_below_the_default_threshold(self):
        existing = {"imdb": {"rating": 8.0, "votes": 10, "default": True}}
        self.assertIsNone(build_rating_update(existing, {"imdb": {"rating": 8.1, "votes": 10}}, "keep"))
        self.assertIsNone(build_rating_update(existing, {"imdb": {"rating": 8.2, "votes": 10}}, "keep", 0.2))
        self.assertIsNone(build_rating_update(existing, {"imdb": {"rating": 8.0, "votes": 99}}, "keep"))

    def test_change_above_the_threshold_is_written(self):
        payload = build_rating_update(
            {"imdb": {"rating": 8.0, "votes": 10, "default": True}},
            {"imdb": {"rating": 8.3, "votes": 12}},
            "keep",
        )
        self.assertEqual(payload["imdb"]["rating"], 8.3)
        self.assertEqual(payload["imdb"]["votes"], 12)

    def test_zero_threshold_rewrites_an_unchanged_rating(self):
        payload = build_rating_update(
            {"imdb": {"rating": 8.0, "votes": 10, "default": True}},
            {"imdb": {"rating": 8.0, "votes": 10}},
            "keep",
            0,
        )
        self.assertEqual(payload["imdb"]["rating"], 8.0)

    def test_missing_only_keeps_a_stored_rating_and_writes_a_new_source(self):
        existing = {"imdb": {"rating": 8.0, "votes": 10, "default": True}}
        self.assertIsNone(build_rating_update(existing, {"imdb": {"rating": 9.0, "votes": 20}}, "keep", 0, True))
        payload = build_rating_update(
            existing,
            {"imdb": {"rating": 9.0, "votes": 20}, "trakt": {"rating": 7.5, "votes": 4}},
            "imdb",
            0.2,
            True,
        )
        self.assertNotIn("imdb", payload)
        self.assertEqual(payload["trakt"]["rating"], 7.5)
        self.assertNotIn("default", payload["trakt"])

    def test_missing_only_sets_the_default_when_the_new_source_is_preferred(self):
        payload = build_rating_update(
            {"themoviedb": {"rating": 7.0, "votes": 10, "default": True}},
            {"imdb": {"rating": 8.4, "votes": 5}},
            "imdb",
            0.2,
            True,
        )
        self.assertEqual(set(payload), {"imdb"})
        self.assertTrue(payload["imdb"]["default"])

    def test_missing_only_treats_the_tmdb_alias_as_already_stored(self):
        self.assertIsNone(
            build_rating_update(
                {"tmdb": {"rating": 7.0, "votes": 2, "default": True}},
                {"themoviedb": {"rating": 8.0, "votes": 3}},
                "keep",
                0,
                True,
            )
        )

    def test_rejects_scores_kodi_cannot_store(self):
        self.assertIsNone(build_rating_update({}, {"imdb": {"rating": 85, "votes": 10}}, "keep"))

    def test_movie_numeric_imdbnumber_can_be_a_tmdb_id(self):
        self.assertEqual(tmdb_id({}, "550", allow_numeric_fallback=True), "550")
        self.assertEqual(tmdb_id({}, "12345"), "")
        self.assertEqual(imdb_id({}, "tt0137523"), "tt0137523")

    def test_existing_ids_are_not_replaced(self):
        self.assertEqual(new_uniqueids({"imdb": "tt0000001", "tmdb": "10"}, {"imdb": "tt9999999", "tvdb": "20"}), {"tvdb": "20"})

    def test_details_params_do_not_touch_the_personal_rating(self):
        params = details_params(movie(), {"imdb": {"rating": 8.0, "votes": 1, "default": True}}, {"imdb": "tt0137523"})
        self.assertEqual(params["movieid"], 1)
        self.assertNotIn("userrating", params)
        self.assertIn("ratings", params)


class ParseTests(unittest.TestCase):
    def test_runscript_arguments(self):
        params = parse_params(["plugin://script.ratings.updatarr", "content=movies+episodes&background=true", "dbid=4"])
        self.assertEqual(parse_media_types(params["content"]), ["movie", "episode"])
        self.assertEqual(parse_media_types("all"), ["movie", "tvshow", "episode"])
        self.assertTrue(is_truthy(params["background"]))
        self.assertEqual(params["dbid"], "4")

    def test_schedule_is_due_after_the_interval(self):
        now = datetime(2026, 11, 11, tzinfo=timezone.utc)
        self.assertTrue(is_due(None, 24, now))
        self.assertFalse(is_due(now - timedelta(hours=2), 24, now))
        self.assertTrue(is_due(now - timedelta(hours=24), 24, now))

    def test_daily_schedule_waits_for_a_clock_time(self):
        times = parse_times("03:00, 15:30")
        self.assertEqual(times, [(3, 0), (15, 30)])
        self.assertEqual(parse_times("nope"), [(3, 0)])
        morning = datetime(2026, 11, 11, 2, 0, tzinfo=timezone.utc)
        after_first = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)
        after_second = datetime(2026, 11, 11, 16, 0, tzinfo=timezone.utc)
        self.assertFalse(daily_due(None, times, morning))
        self.assertTrue(daily_due(None, times, after_first))
        ran_at_three = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)
        self.assertFalse(daily_due(ran_at_three, times, after_first))
        self.assertTrue(daily_due(ran_at_three, times, after_second))
        yesterday = datetime(2026, 11, 10, 16, 0, tzinfo=timezone.utc)
        self.assertTrue(daily_due(yesterday, times, after_first))

    def test_media_type_switches_limit_a_run(self):
        self.assertEqual(selected_types(["movie", "tvshow", "episode"], ("movie", "episode")), ["movie", "episode"])
        self.assertEqual(selected_types(["tvshow"], ("movie",)), [])

    def test_tmdb_key_warning_is_skipped_when_tmdbhelper_can_answer(self):
        config = Config(use_tmdb=True, tmdb_key="", use_tmdbhelper=True)
        self.assertEqual(config.missing_keys(), ["tmdb"])
        self.assertEqual(config.missing_keys(tmdbhelper_available=True), [])
        self.assertEqual(Config(use_tmdb=True, tmdb_key="", use_tmdbhelper=False).missing_keys(tmdbhelper_available=True), ["tmdb"])

    def test_imdb_folder_stays_in_addon_data_until_a_path_is_set(self):
        self.assertEqual(Config().imdb_folder("/profile"), os.path.join("/profile", "imdb"))
        self.assertEqual(Config(imdb_path="   ").imdb_folder("/profile"), os.path.join("/profile", "imdb"))
        self.assertEqual(Config(imdb_path="D:/ratings/imdb/").imdb_folder("/profile"), os.path.normpath("D:/ratings/imdb"))


class LibraryTests(unittest.TestCase):
    def test_paginates_until_the_reported_total(self):
        from resources.lib import library as library_module

        calls = []

        def rpc(method, params):
            calls.append(params["limits"]["start"])
            start = params["limits"]["start"]
            row = {"movieid": start + 1, "title": f"Title {start}", "uniqueid": {}, "ratings": {}}
            return {"movies": [row], "limits": {"total": 2}}

        original = library_module.PAGE_SIZE
        library_module.PAGE_SIZE = 1
        try:
            items = LibraryClient(rpc).list_items("movie")
        finally:
            library_module.PAGE_SIZE = original
        self.assertEqual([item.dbid for item in items], [1, 2])
        self.assertEqual(calls, [0, 1])

    def test_real_page_size_stops_on_a_short_page(self):
        def rpc(method, params):
            self.assertEqual(method, "VideoLibrary.GetMovies")
            return {"movies": [{"movieid": 7, "title": "Only", "ratings": {}, "uniqueid": {}}], "limits": {"total": 1}}

        items = LibraryClient(rpc).list_items("movie")
        self.assertEqual(items[0].title, "Only")
        self.assertEqual(items[0].dbid, 7)

    def test_apply_sends_only_changed_rating_fields(self):
        sent = {}

        def rpc(method, params):
            sent["method"] = method
            sent["params"] = params
            return {}

        LibraryClient(rpc).apply(
            movie(dbid=9),
            {"imdb": {"rating": 8.0, "votes": 3, "default": True}},
            {},
        )
        self.assertEqual(sent["method"], "VideoLibrary.SetMovieDetails")
        self.assertEqual(sent["params"]["movieid"], 9)
        self.assertNotIn("userrating", sent["params"])

    def test_rpc_errors_are_reported_without_the_raw_payload(self):
        with self.assertRaises(JsonRpcError):
            parse_rpc_payload(json.dumps({"error": {"message": "Invalid params."}}))


class SourceTests(unittest.TestCase):
    def test_movie_ratings_and_discovered_ids(self):
        transport = MapTransport(
            [
                (
                    "/movie/550",
                    {
                        "vote_average": 8.4,
                        "vote_count": 100,
                        "imdb_id": "tt0137523",
                        "external_ids": {"imdb_id": "tt0137523"},
                    },
                ),
                (
                    "omdbapi.com",
                    {
                        "Response": "True",
                        "imdbRating": "8.8",
                        "imdbVotes": "2,000",
                        "Ratings": [{"Source": "Rotten Tomatoes", "Value": "90%"}],
                    },
                ),
            ]
        )
        config = Config(tmdb_key="tmdb-key", omdb_key="omdb-key", use_tmdb=True, use_omdb=True)
        result = SourceClient(config, transport).ratings_for(movie(imdbnumber="550"))
        self.assertEqual(result.ratings["themoviedb"]["rating"], 8.4)
        self.assertEqual(result.ratings["imdb"]["rating"], 8.8)
        self.assertEqual(result.ratings["tomatometerallcritics"]["rating"], 9.0)
        self.assertEqual(result.discovered_ids["tmdb"], "550")
        self.assertEqual(result.discovered_ids["imdb"], "tt0137523")
        self.assertTrue(any("api_key=tmdb-key" in url for url in transport.urls))

    def test_tv_numeric_imdbnumber_is_not_treated_as_tmdb(self):
        transport = MapTransport([])
        item = LibraryItem(mediatype="tvshow", dbid=3, title="Show", imdbnumber="12345")
        result = SourceClient(Config(tmdb_key="k", use_tmdb=True), transport).ratings_for(item)
        self.assertEqual(result.ratings, {})
        self.assertEqual(transport.urls, [])

    def test_episode_uses_the_episode_imdb_id(self):
        transport = MapTransport(
            [
                (
                    "/tv/100/season/1/episode/2",
                    {
                        "id": 555,
                        "vote_average": 8.0,
                        "vote_count": 20,
                        "external_ids": {"imdb_id": "tt100200"},
                    },
                ),
                (
                    "i=tt100200",
                    {"Response": "True", "imdbRating": "9.1", "imdbVotes": "50"},
                ),
            ]
        )
        show = LibraryItem(mediatype="tvshow", dbid=4, title="Show", uniqueids={"tmdb": "100", "imdb": "ttSHOW"})
        episode = LibraryItem(
            mediatype="episode",
            dbid=8,
            title="Pilot",
            showtitle="Show",
            season=1,
            episode=2,
            tvshowid=4,
        )
        config = Config(tmdb_key="k", omdb_key="o", use_tmdb=True, use_omdb=True)
        result = SourceClient(config, transport).ratings_for(episode, show)
        self.assertEqual(result.ratings["imdb"]["rating"], 9.1)
        self.assertEqual(result.discovered_ids["tmdb"], "555")
        self.assertFalse(any("ttSHOW" in url for url in transport.urls))
        self.assertTrue(any("i=tt100200" in url for url in transport.urls))

    def test_not_found_does_not_hide_another_source(self):
        transport = MapTransport(
            [
                ("/movie/9", HttpStatus(404, "missing")),
                ("omdbapi.com", {"Response": "True", "imdbRating": "7.0", "imdbVotes": "15"}),
            ]
        )
        item = movie(uniqueids={"tmdb": "9", "imdb": "tt15"})
        config = Config(tmdb_key="k", omdb_key="o", use_tmdb=True, use_omdb=True)
        result = SourceClient(config, transport).ratings_for(item)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.ratings["imdb"]["rating"], 7.0)

    def test_rejected_key_stops_the_lookup(self):
        transport = MapTransport([("/movie/9", HttpStatus(401, "denied"))])
        item = movie(uniqueids={"tmdb": "9"})
        with self.assertRaises(AuthError) as caught:
            SourceClient(Config(tmdb_key="bad", use_tmdb=True), transport).ratings_for(item)
        self.assertEqual(caught.exception.provider, "tmdb")


class UpdateTests(unittest.TestCase):
    def test_writes_a_changed_movie_and_skips_a_title_with_no_id(self):
        library = FakeLibrary(
            [
                movie(dbid=1, title="Known", uniqueids={"tmdb": "10"}, ratings={"themoviedb": {"rating": 5, "votes": 5, "default": True}}),
                movie(dbid=2, title="Unknown"),
            ]
        )
        source = ScriptedSource(
            lambda item, show: Fetch(ratings={"themoviedb": {"rating": 6, "votes": 6}}, requested=True)
            if item.dbid == 1
            else Fetch()
        )
        pauses = []
        stats = update_types(
            ["movie"],
            library,
            source,
            Config(tmdb_key="k", delay_seconds=0.25),
            pause=lambda seconds: pauses.append(seconds) or False,
        )
        self.assertEqual(stats.updated, 1)
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(library.applied[0][1]["themoviedb"]["rating"], 6)
        self.assertEqual(pauses, [0.25])

    def test_episode_update_uses_the_parent_show(self):
        show = LibraryItem(mediatype="tvshow", dbid=4, title="Show", uniqueids={"tmdb": "100"})
        episode = LibraryItem(mediatype="episode", dbid=8, title="Cold Open", showtitle="Show", season=1, episode=2, tvshowid=4)
        seen = {}

        def lookup(item, parent):
            seen["show"] = parent.dbid if parent else None
            return Fetch(ratings={"themoviedb": {"rating": 8, "votes": 3}})

        stats = update_types(["episode"], FakeLibrary([show, episode]), ScriptedSource(lookup), Config(tmdb_key="k", delay_seconds=0))
        self.assertEqual(seen["show"], 4)
        self.assertEqual(stats.updated, 1)

    def test_auth_failure_stops_before_the_next_item(self):
        library = FakeLibrary([movie(dbid=1, uniqueids={"tmdb": "1"}), movie(dbid=2, uniqueids={"tmdb": "2"})])

        def lookup(item, show):
            if item.dbid == 1:
                raise AuthError("tmdb")
            return Fetch(ratings={"themoviedb": {"rating": 8, "votes": 1}})

        stats = update_items(library.items, {}, library, ScriptedSource(lookup), Config(tmdb_key="k"))
        self.assertEqual(stats.stopped, "tmdb")
        self.assertEqual(stats.updated, 0)
        self.assertEqual(library.applied, [])

    def test_cancel_keeps_the_item_already_written(self):
        library = FakeLibrary([movie(dbid=1, uniqueids={"imdb": "tt1"}), movie(dbid=2, uniqueids={"imdb": "tt2"})])
        progress = CancelAfter(1)
        stats = update_items(
            library.items,
            {},
            library,
            ScriptedSource(lambda item, show: Fetch(ratings={"imdb": {"rating": 7, "votes": 2}})),
            Config(omdb_key="o", use_tmdb=False, use_omdb=True, delay_seconds=0),
            progress=progress,
        )
        self.assertTrue(stats.cancelled)
        self.assertEqual(stats.updated, 1)

    def test_save_ids_can_be_turned_off(self):
        library = FakeLibrary([movie(uniqueids={})])
        stats = update_items(
            library.items,
            {},
            library,
            ScriptedSource(lambda item, show: Fetch(ratings={"imdb": {"rating": 7, "votes": 2}}, discovered_ids={"imdb": "tt1"})),
            Config(omdb_key="o", use_tmdb=False, use_omdb=True, save_ids=False, delay_seconds=0),
        )
        self.assertEqual(stats.updated, 1)
        self.assertEqual(library.applied[0][2], {})

    def test_finished_update_can_show_its_summary(self):
        import sys
        import types

        for name in ("xbmc", "xbmcaddon", "xbmcgui", "xbmcvfs"):
            sys.modules.setdefault(name, types.ModuleType(name))
        from resources.lib import entry

        shown = {}
        stats = types.SimpleNamespace(
            stopped="",
            cancelled=False,
            total_seen=3,
            updated=1,
            unchanged=2,
            skipped=0,
            failed=0,
        )
        entry.kodi.text = lambda string_id, **kwargs: str(string_id)
        entry.kodi.log = lambda message: None
        entry.kodi.append_run = lambda record: None
        entry.kodi.show_summary = lambda heading, message: shown.update(message=message)
        entry._announce(stats, ["movie"], False, False)
        self.assertEqual(shown["message"], "32167")


class HttpTests(unittest.TestCase):
    def test_retries_a_rate_limit_once(self):
        calls = {"count": 0}

        def opener(request, timeout):
            calls["count"] += 1
            if calls["count"] == 1:
                header = Message()
                header["Retry-After"] = "1"
                raise urllib.error.HTTPError("https://api.test/item", 429, "busy", header, io.BytesIO(b"busy"))
            return FakeResponse(b'{"vote_average": 7.5, "vote_count": 4}')

        slept = []
        payload = JsonClient(opener=opener, sleeper=slept.append).get_json("https://api.test/item")
        self.assertEqual(payload["vote_count"], 4)
        self.assertEqual(slept, [1.0])
        self.assertEqual(calls["count"], 2)


class ImdbDataTests(unittest.TestCase):
    def test_builds_ratings_and_episode_lookups(self):
        with tempfile.TemporaryDirectory() as folder:
            ratings = os.path.join(folder, "ratings.tsv.gz")
            episodes = os.path.join(folder, "episodes.tsv.gz")
            _gzip(ratings, "tconst\taverageRating\tnumVotes\ntt0000001\t8.1\t100\ntt0000002\t7.0\t5\n")
            _gzip(episodes, "tconst\tparentTconst\tseasonNumber\tepisodeNumber\ntt0000002\ttt0000001\t1\t2\n")
            counts = build_index(ratings, episodes, os.path.join(folder, "imdb.sqlite"))
            data = ImdbData(folder)
            try:
                self.assertEqual(counts["ratings"], 2)
                self.assertEqual(data.rating("tt0000001"), {"rating": 8.1, "votes": 100})
                found = data.episode("tt1", 1, 2)
                self.assertEqual(found["imdb"], "tt0000002")
                self.assertEqual(found["rating"]["rating"], 7.0)
                self.assertTrue(data.ready())
                self.assertTrue(data.due(30))
            finally:
                data._close()

    def test_download_is_replaced_by_an_unchanged_check_and_imports_stay(self):
        with tempfile.TemporaryDirectory() as folder:
            bodies = {
                "title.ratings.tsv.gz": _gzbytes("tconst\taverageRating\tnumVotes\ntt0000010\t6.5\t8\n"),
                "title.episode.tsv.gz": _gzbytes(
                    "tconst\tparentTconst\tseasonNumber\tepisodeNumber\ntt0000011\ttt0000010\t0\t1\n"
                ),
            }
            calls = []

            def opener(request, timeout):
                name = request.full_url.rsplit("/", 1)[-1]
                calls.append(request.get_method() + " " + name)
                length = str(len(bodies[name]))
                headers = {"ETag": "v1", "Last-Modified": "Wed", "Content-Length": length}
                body = b"" if request.get_method() == "HEAD" else bodies[name]
                return _Body(body, headers)

            data = ImdbData(folder, opener=opener)
            try:
                first = data.refresh(30, report=lambda _message: None)
                self.assertTrue(first["changed"])
                self.assertEqual(data.rating("tt10")["rating"], 6.5)
                self.assertFalse(any(name.endswith(".gz") for name in os.listdir(folder)))
                calls.clear()
                second = data.refresh(30, report=lambda _message: None, force=True)
                self.assertFalse(second["changed"])
                self.assertTrue(all(call.startswith("HEAD") for call in calls))

                imported = os.path.join(folder, "import")
                os.makedirs(imported)
                kept = os.path.join(imported, "title.ratings.tsv.gz")
                _gzip(kept, "tconst\taverageRating\tnumVotes\ntt0000020\t9.0\t1\n")
                _gzip(
                    os.path.join(imported, "title.episode.tsv.gz"),
                    "tconst\tparentTconst\tseasonNumber\tepisodeNumber\n",
                )
                self.assertTrue(data.due(30))
                data.refresh(30, report=lambda _message: None)
                self.assertTrue(os.path.isfile(kept))
                self.assertEqual(data.rating("tt20")["rating"], 9.0)
            finally:
                data._close()

    def test_cancel_keeps_the_previous_index(self):
        with tempfile.TemporaryDirectory() as folder:
            ratings = os.path.join(folder, "ratings.tsv.gz")
            episodes = os.path.join(folder, "episodes.tsv.gz")
            _gzip(ratings, "tconst\taverageRating\tnumVotes\ntt0000001\t8.0\t1\n")
            _gzip(episodes, "tconst\tparentTconst\tseasonNumber\tepisodeNumber\n")
            build_index(ratings, episodes, os.path.join(folder, "imdb.sqlite"))

            def opener(request, timeout):
                return _Body(b"partial", {"Content-Length": "10"})

            data = ImdbData(folder, opener=opener)
            try:
                with self.assertRaises(DatasetCancelled):
                    data.refresh(30, should_stop=lambda: True)
                self.assertEqual(data.rating("tt1")["rating"], 8.0)
                self.assertFalse(data.due(30))
            finally:
                data._close()


class HistoryTests(unittest.TestCase):
    def test_keeps_the_ten_newest_runs(self):
        runs = remember([], {"summary": "first"})
        for number in range(12):
            runs = remember(runs, {"summary": str(number)})
        self.assertEqual(len(runs), 10)
        self.assertEqual(runs[0]["summary"], "11")
        self.assertEqual(runs[-1]["summary"], "2")


class LocalImdbTests(unittest.TestCase):
    def test_dataset_score_wins_over_omdb_imdb(self):
        transport = MapTransport(
            [
                (
                    "omdbapi.com",
                    {
                        "Response": "True",
                        "imdbRating": "1.0",
                        "imdbVotes": "10",
                        "Ratings": [{"Source": "Rotten Tomatoes", "Value": "80%"}],
                    },
                )
            ]
        )

        class Index:
            def rating(self, imdb):
                return {"rating": 9.1, "votes": 3}

        item = movie(uniqueids={"imdb": "tt15"})
        config = Config(omdb_key="o", use_tmdb=False, use_omdb=True, use_imdb_data=True)
        result = SourceClient(config, transport, Index()).ratings_for(item)
        self.assertEqual(result.ratings["imdb"]["rating"], 9.1)
        self.assertEqual(result.ratings["tomatometerallcritics"]["rating"], 8.0)


class HelperRatingTests(unittest.TestCase):
    def test_cached_tmdb_score_skips_the_provider_call(self):
        from resources.lib.helper_ratings import HelperResult

        class Helper:
            def lookup(self, **kwargs):
                return HelperResult(
                    ratings={"themoviedb": {"rating": 7.5, "votes": 20}},
                    ids={"imdb": "tt15"},
                )

        transport = MapTransport([])
        item = movie(uniqueids={"tmdb": "9"})
        config = Config(tmdb_key="k", use_tmdb=True, use_omdb=False, use_trakt=False, use_imdb_data=False)
        result = SourceClient(config, transport, helper=Helper()).ratings_for(item)
        self.assertEqual(transport.urls, [])
        self.assertEqual(result.ratings["themoviedb"]["rating"], 7.5)
        self.assertEqual(result.discovered_ids["imdb"], "tt15")

    def test_helper_scores_are_scaled_onto_ten(self):
        from resources.lib.helper_ratings import ratings_from_mdblist, ratings_from_omdb_record

        mdblist = ratings_from_mdblist(
            {
                "ratings": [
                    {"source": "imdb", "value": 8.4, "votes": 10},
                    {"source": "tomatoes", "value": 90, "votes": 12},
                    {"source": "metacritic", "value": 80},
                ]
            }
        )
        self.assertEqual(mdblist["imdb"]["rating"], 8.4)
        self.assertEqual(mdblist["tomatometerallcritics"]["rating"], 9.0)
        self.assertEqual(mdblist["metacritic"]["rating"], 8.0)
        omdb = ratings_from_omdb_record({"imdbRating": "7.0", "imdbVotes": "15", "tomatoMeter": "80"})
        self.assertEqual(omdb["tomatometerallcritics"]["rating"], 8.0)


class LimitTests(unittest.TestCase):
    def test_tmdb_waits_once_the_second_is_full(self):
        from resources.lib.limits import Limiter

        now = {"at": 1000.0}
        limiter = Limiter(clock=lambda: now["at"])
        for _ in range(20):
            self.assertEqual(limiter.wait_for("tmdb"), 0)
            limiter.observe("tmdb")
        self.assertGreater(limiter.wait_for("tmdb"), 0)
        now["at"] += 1
        self.assertEqual(limiter.wait_for("tmdb"), 0)

    def test_trakt_header_holds_the_next_call(self):
        from resources.lib.limits import Limiter

        now = {"at": 1_000.0}
        limiter = Limiter(clock=lambda: now["at"])
        limiter.observe("trakt", {"x-ratelimit": '{"remaining": 0, "until": "1970-01-01T00:20:00Z"}'})
        self.assertGreater(limiter.wait_for("trakt"), 0)

    def test_omdb_daily_allowance_stops_further_calls(self):
        from resources.lib.limits import DailyQuota, Limiter, PacedTransport

        now = {"at": 1_700_000_000.0}
        limiter = Limiter(omdb_daily=1, clock=lambda: now["at"])
        calls = []

        class Inner:
            def exchange(self, url, headers=None):
                calls.append(url)

                class Result:
                    payload = {"Response": "True"}
                    headers = {}

                return Result()

        transport = PacedTransport(Inner(), limiter, lambda _seconds: False)
        transport.get_json("https://www.omdbapi.com/?i=tt1")
        with self.assertRaises(DailyQuota):
            transport.get_json("https://www.omdbapi.com/?i=tt2")
        self.assertEqual(len(calls), 1)

    def test_retry_after_is_waited_out(self):
        calls = {"count": 0}

        def opener(request, timeout):
            calls["count"] += 1
            if calls["count"] < 3:
                header = Message()
                header["Retry-After"] = "2"
                raise urllib.error.HTTPError("https://api.trakt.tv/movies/tt1/ratings", 429, "busy", header, io.BytesIO(b"busy"))
            return FakeResponse(b'{"rating": 8, "votes": 4}')

        slept = []
        payload = JsonClient(opener=opener, sleeper=slept.append).get_json("https://api.trakt.tv/movies/tt1/ratings")
        self.assertEqual(payload["votes"], 4)
        self.assertEqual(slept, [2.0, 2.0])


class PackageTests(unittest.TestCase):
    def test_addon_installs_on_omega_and_lists_its_files(self):
        addon = (ROOT / "addon.xml").read_text(encoding="utf-8")
        self.assertIn('id="script.ratings.updatarr"', addon)
        self.assertIn('addon="xbmc.python" version="3.0.1"', addon)
        self.assertNotIn('<import addon="xbmc.addon"', addon)
        self.assertIn("kodi.context.item", addon)
        self.assertIn("context_item.py", addon)
        self.assertIn("service.py", addon)
        settings = (ROOT / "resources/settings.xml").read_text(encoding="utf-8")
        self.assertIn("action=refreshimdb", settings)
        self.assertIn('id="omdb_key"', settings)
        self.assertIn('id="trakt_key"', settings)
        self.assertIn('id="mdblist_key"', settings)
        self.assertIn('id="recent_days"', settings)
        self.assertIn('id="update_on_scan"', settings)
        self.assertIn('id="pause_during_playback"', settings)
        for name in ("default.py", "context_item.py", "service.py", "resources/settings.xml", "icon.png", "fanart.png"):
            self.assertTrue((ROOT / name).is_file(), name)

    def test_every_referenced_string_exists(self):
        po = (ROOT / "resources/language/resource.language.en_gb/strings.po").read_text(encoding="utf-8")
        defined = set(__import__("re").findall(r'msgctxt "#(\d+)"', po))
        blob = "\n".join(path.read_text(encoding="utf-8") for path in ROOT.rglob("*") if path.suffix in {".py", ".xml"})
        used = set(__import__("re").findall(r"\b(32\d{3})\b", blob))
        self.assertFalse(used - defined, sorted(used - defined))


class ImprovementsAndFixesTests(unittest.TestCase):
    def test_any_active_respects_tmdbhelper_availability(self):
        cfg = Config(use_imdb_data=False, use_tmdb=True, tmdb_key="", use_tmdbhelper=True)
        self.assertFalse(cfg.any_active(tmdbhelper_available=False))
        self.assertTrue(cfg.any_active(tmdbhelper_available=True))
        all_off = Config(use_imdb_data=False, use_tmdb=False, use_omdb=False, use_trakt=False, use_mdblist=False, use_tmdbhelper=True)
        self.assertFalse(all_off.any_active(tmdbhelper_available=True))

    def test_apply_helper_respects_disabled_use_tmdb(self):
        from resources.lib.helper_ratings import HelperResult

        class Helper:
            def lookup(self, **kwargs):
                return HelperResult(
                    ratings={
                        "themoviedb": {"rating": 7.5, "votes": 20},
                        "imdb": {"rating": 8.2, "votes": 50},
                    },
                    ids={"imdb": "tt15"},
                )

        item = movie(uniqueids={"tmdb": "9"})
        cfg = Config(use_tmdb=False, use_imdb_data=True, use_tmdbhelper=True)
        result = SourceClient(cfg, MapTransport([]), helper=Helper()).ratings_for(item)
        self.assertNotIn("themoviedb", result.ratings)
        self.assertEqual(result.ratings["imdb"]["rating"], 8.2)
        self.assertEqual(result.discovered_ids["imdb"], "tt15")

    def test_helper_bridge_reads_cached_omdb_mdblist_and_properties(self):
        from resources.lib.helper_kodi import HelperBridge

        class FakeApi:
            append_to_response = "external_ids"

            def get_request_lc(self, *args, **kwargs):
                return {
                    "vote_average": 7.8,
                    "vote_count": 200,
                    "external_ids": {"imdb_id": "tt0123456"},
                    "omdb": {"Response": "True", "tomatoMeter": "92", "Metascore": "81"},
                    "ratings": [
                        {"source": "tomatoesaudience", "value": 88, "votes": 500},
                        {"source": "letterboxd", "value": 4.1, "votes": 1000},
                    ],
                    "infoproperties": {"trakt_rating": "8.0", "trakt_votes": "450"},
                }

        res = HelperBridge(FakeApi(), lambda _m: None).lookup(kind="movie", tmdb="550")
        self.assertEqual(res.ratings["themoviedb"]["rating"], 7.8)
        self.assertEqual(res.ratings["tomatometerallcritics"]["rating"], 9.2)
        self.assertEqual(res.ratings["metacritic"]["rating"], 8.1)
        self.assertEqual(res.ratings["tomatometerallaudience"]["rating"], 8.8)
        self.assertEqual(res.ratings["letterboxd"]["rating"], 8.2)
        self.assertEqual(res.ratings["trakt"]["rating"], 8.0)
        self.assertEqual(res.ids["imdb"], "tt0123456")

    def test_episode_resolves_parent_show_imdb_via_tmdb_and_saves_show_ids(self):
        class FakeIndex:
            def episode(self, show_imdb, season, number):
                if show_imdb == "tt999":
                    return {"imdb": "tt1001", "rating": {"rating": 8.9, "votes": 120}}
                return None

            def rating(self, imdb):
                return None

        transport = MapTransport(
            [
                (
                    "/tv/100?",
                    {"id": 100, "external_ids": {"imdb_id": "tt999", "tvdb_id": 777}},
                ),
                (
                    "/tv/100/season/1/episode/2",
                    {"id": 555, "vote_average": 8.5, "vote_count": 40, "external_ids": {}},
                ),
            ]
        )
        show = LibraryItem(mediatype="tvshow", dbid=4, title="Show", uniqueids={"tmdb": "100"})
        episode = LibraryItem(
            mediatype="episode",
            dbid=8,
            title="Ep 2",
            showtitle="Show",
            season=1,
            episode=2,
            tvshowid=4,
        )
        library = FakeLibrary([show, episode])
        config = Config(tmdb_key="k", use_tmdb=True, use_imdb_data=True, delay_seconds=0)
        sources = SourceClient(config, transport, imdb=FakeIndex())
        stats = update_items([episode], {4: show}, library, sources, config)
        self.assertEqual(stats.updated, 1)
        # First applied call saves discovered IDs on the parent show, second updates the episode
        show_apply = [call for call in library.applied if call[0].dbid == 4]
        ep_apply = [call for call in library.applied if call[0].dbid == 8]
        self.assertEqual(len(show_apply), 1)
        self.assertEqual(show_apply[0][2], {"imdb": "tt999", "tvdb": "777"})
        self.assertEqual(ep_apply[0][1]["imdb"]["rating"], 8.9)
        self.assertEqual(ep_apply[0][2]["imdb"], "tt1001")

    def test_mdblist_parsing_lookup_and_daily_quota(self):
        from resources.lib.limits import DailyQuota, Limiter, PacedTransport

        ratings, ids = parse_mdblist(
            {
                "imdbid": "tt0137523",
                "tmdbid": 550,
                "tvdbid": None,
                "ratings": [
                    {"source": "imdb", "value": 8.8, "votes": 2300000},
                    {"source": "tomatoes", "value": 79, "votes": 180},
                    {"source": "tomatoesaudience", "value": 96, "votes": 100000},
                    {"source": "metacritic", "value": 67},
                    {"source": "letterboxd", "value": 4.3, "score": 86, "votes": 900000},
                    {"source": "mal", "value": 8.5, "votes": 50000},
                    {"source": "trakt", "value": 8.6, "votes": 40000},
                ],
            }
        )
        self.assertEqual(ids, {"imdb": "tt0137523", "tmdb": "550"})
        self.assertEqual(ratings["tomatometerallcritics"]["rating"], 7.9)
        self.assertEqual(ratings["tomatometerallaudience"]["rating"], 9.6)
        self.assertEqual(ratings["letterboxd"]["rating"], 8.6)
        self.assertEqual(ratings["mal"]["rating"], 8.5)
        self.assertEqual(ratings["trakt"]["rating"], 8.6)

        with self.assertRaises(AuthError):
            parse_mdblist({"response": False, "error": "Invalid API key"})

        now = {"at": 1_700_000_000.0}
        limiter = Limiter(mdblist_daily=1, clock=lambda: now["at"])
        limiter.observe("mdblist")
        with self.assertRaises(DailyQuota) as caught:
            limiter.wait_for("mdblist")
        self.assertEqual(caught.exception.provider, "mdblist")

        # Integration with SourceClient: MDbList fills RT/Metacritic/Trakt so OMDb/Trakt are skipped
        transport = MapTransport(
            [
                (
                    "api.mdblist.com",
                    {
                        "imdbid": "tt1",
                        "tmdbid": 550,
                        "ratings": [
                            {"source": "tomatoes", "value": 80, "votes": 10},
                            {"source": "tomatoesaudience", "value": 90, "votes": 20},
                            {"source": "metacritic", "value": 75, "votes": 5},
                            {"source": "trakt", "value": 8.1, "votes": 100},
                        ],
                    },
                )
            ]
        )
        cfg = Config(use_tmdb=False, use_imdb_data=False, use_mdblist=True, mdblist_key="m", use_omdb=True, omdb_key="o", use_trakt=True, trakt_key="t")
        res = SourceClient(cfg, transport).ratings_for(movie(uniqueids={"imdb": "tt1"}))
        self.assertEqual(len(transport.urls), 1)
        self.assertIn("api.mdblist.com", transport.urls[0])
        self.assertEqual(res.ratings["tomatometerallaudience"]["rating"], 9.0)

    def test_recent_days_filter_and_quota_prioritization(self):
        now = datetime(2026, 11, 19, 12, 0, 0)
        old_show = LibraryItem(mediatype="tvshow", dbid=10, title="Old Show", uniqueids={"imdb": "ttOLD"}, dateadded="2025-01-01 00:00:00")
        recent_ep = LibraryItem(mediatype="episode", dbid=20, title="New Ep", tvshowid=10, season=1, episode=1, dateadded="2026-11-18 10:00:00")
        old_ep = LibraryItem(mediatype="episode", dbid=21, title="Old Ep", tvshowid=10, season=1, episode=2, dateadded="2025-01-01 00:00:00")
        seen_parents = []

        class EpSource:
            def ratings_for(self, item, show=None):
                seen_parents.append((item.dbid, show.dbid if show else None))
                return Fetch(ratings={"imdb": {"rating": 8.0, "votes": 10}})

        cfg = Config(recent_days=7, delay_seconds=0, use_imdb_data=True)
        stats = update_types(["episode"], FakeLibrary([old_show, recent_ep, old_ep]), EpSource(), cfg, now=now)
        self.assertEqual(stats.updated, 1)
        # Old parent show was still passed to recent_ep even though recent_days filtered episodes
        self.assertEqual(seen_parents, [(20, 10)])

        # Quota prioritization places items missing OMDb ratings ahead of items that already have them
        already = movie(
            dbid=1,
            uniqueids={"imdb": "tt1"},
            ratings={
                "tomatometerallcritics": {"rating": 8.0, "votes": 1, "default": False},
                "metacritic": {"rating": 7.0, "votes": 1, "default": False},
            },
        )
        missing_rt = movie(dbid=2, uniqueids={"imdb": "tt2"}, ratings={"imdb": {"rating": 7.5, "votes": 10, "default": True}})
        order = []

        class TrackSource:
            def ratings_for(self, item, show=None):
                order.append(item.dbid)
                return Fetch()

        cfg_omdb = Config(use_omdb=True, omdb_key="o", use_tmdb=False, use_imdb_data=False, delay_seconds=0)
        update_types(["movie"], FakeLibrary([already, missing_rt]), TrackSource(), cfg_omdb)
        self.assertEqual(order, [2, 1])

    def test_library_episodes_for_show_and_get_season(self):
        calls = []

        def rpc(method, params):
            calls.append((method, params))
            if method == "VideoLibrary.GetSeasonDetails":
                return {"seasondetails": {"season": 2, "tvshowid": 4, "showtitle": "Show"}}
            return {"episodes": [{"episodeid": 9, "title": "S2E1", "season": 2, "episode": 1, "tvshowid": 4}], "limits": {"total": 1}}

        client = LibraryClient(rpc)
        details = client.get_season(15)
        self.assertEqual(details["season"], 2)
        eps = client.list_episodes_for_show(4, season=2)
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0].dbid, 9)
        self.assertEqual(calls[1][1]["tvshowid"], 4)
        self.assertEqual(calls[1][1]["season"], 2)

    def test_failures_persisted_and_cancelled_schedule_skips_last_run(self):
        import types

        for name in ("xbmc", "xbmcaddon", "xbmcgui", "xbmcvfs"):
            sys.modules.setdefault(name, types.ModuleType(name))
        from resources.lib import entry

        stored = []
        shown_text = {}
        stats = types.SimpleNamespace(
            stopped="",
            cancelled=False,
            total_seen=2,
            updated=1,
            unchanged=0,
            skipped=0,
            failed=1,
            failures=["Movie A: HTTP 500"],
        )
        with (
            mock.patch.object(entry.kodi, "text", side_effect=lambda sid, **kw: str(sid)),
            mock.patch.object(entry.kodi, "log"),
            mock.patch.object(entry.kodi, "append_run", side_effect=stored.append),
            mock.patch.object(entry.kodi, "show_summary"),
        ):
            entry._announce(stats, ["movie"], False, False)
        self.assertEqual(stored[0]["failures"], ["Movie A: HTTP 500"])

        with (
            mock.patch.object(entry.kodi, "read_runs", return_value=stored),
            mock.patch.object(entry.kodi, "text", return_value="Recent runs"),
            mock.patch.object(entry.kodi, "show_text", side_effect=lambda h, b: shown_text.update(body=b)),
        ):
            entry.show_runs()
        self.assertIn("Movie A: HTTP 500", shown_text["body"])

        # Cancelled scheduled run does not advance last_run
        cancelled_stats = types.SimpleNamespace(
            stopped="",
            cancelled=True,
            total_seen=1,
            updated=0,
            unchanged=0,
            skipped=0,
            failed=0,
            failures=[],
        )
        with (
            mock.patch.object(entry.kodi, "load_config", return_value=Config(use_imdb_data=True)),
            mock.patch.object(entry.kodi, "try_lock", return_value=True),
            mock.patch.object(entry.kodi, "unlock"),
            mock.patch.object(entry.kodi, "KodiMonitor", return_value=mock.MagicMock()),
            mock.patch.object(entry.kodi, "open_progress", return_value=mock.MagicMock()),
            mock.patch.object(entry.kodi, "KodiLibrary", return_value=FakeLibrary([])),
            mock.patch.object(entry.kodi, "write_limits"),
            mock.patch.object(entry.kodi, "write_last_run") as write_last,
            mock.patch.object(entry.kodi, "append_run"),
            mock.patch.object(entry.kodi, "notify"),
            mock.patch.object(entry.kodi, "log"),
            mock.patch.object(entry.kodi, "text", return_value="msg"),
            mock.patch.object(entry, "_sources", return_value=(mock.MagicMock(), mock.MagicMock(state={}))),
            mock.patch.object(entry, "update_types", return_value=cancelled_stats),
        ):
            entry.run_many(["movie"], True, scheduled=True)
            write_last.assert_not_called()

    def test_imdb_refresh_reports_download_and_index_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            bodies = {
                "title.ratings.tsv.gz": _gzbytes("tconst\taverageRating\tnumVotes\ntt0000010\t6.5\t8\n"),
                "title.episode.tsv.gz": _gzbytes(
                    "tconst\tparentTconst\tseasonNumber\tepisodeNumber\ntt0000011\ttt0000010\t1\t1\n"
                ),
            }

            def opener(request, timeout):
                name = request.full_url.rsplit("/", 1)[-1]
                headers = {"ETag": "v1", "Last-Modified": "Wed", "Content-Length": str(len(bodies[name]))}
                body = b"" if request.get_method() == "HEAD" else bodies[name]
                return _Body(body, headers)

            ticks = []
            data = ImdbData(folder, opener=opener)
            try:
                data.refresh(30, progress=lambda pct, msg: ticks.append((pct, msg)))
            finally:
                data._close()
            self.assertTrue(any(pct >= 25 for pct, _ in ticks))
            self.assertTrue(any(pct >= 50 for pct, _ in ticks))


class Fetch:
    def __init__(self, ratings=None, discovered_ids=None, errors=None, requested=False):
        self.ratings = ratings or {}
        self.discovered_ids = discovered_ids or {}
        self.errors = errors or []
        self.requested = requested


class ScriptedSource:
    def __init__(self, lookup):
        self.lookup = lookup

    def ratings_for(self, item, show=None):
        return self.lookup(item, show)


class FakeLibrary:
    def __init__(self, items):
        self.items = items
        self.applied = []

    def list_items(self, mediatype):
        return [item for item in self.items if item.mediatype == mediatype]

    def get_item(self, mediatype, dbid):
        for item in self.items:
            if item.mediatype == mediatype and item.dbid == dbid:
                return item
        return None

    def apply(self, item, ratings, uniqueids):
        self.applied.append((item, ratings, uniqueids))


class MapTransport:
    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    def get_json(self, url, headers=None):
        self.urls.append(url)
        for needle, payload in self.routes:
            if needle in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise HttpStatus(404, "missing fixture")


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class CancelAfter:
    def __init__(self, after):
        self.after = after
        self.updates = 0

    def cancelled(self):
        return self.updates >= self.after

    def update(self, percent, message):
        self.updates += 1

    def close(self):
        return None


def _gzip(path: str, text: str) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(text)


def _gzbytes(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"))


class _Body:
    def __init__(self, body: bytes, headers: dict):
        self._body = body
        self.headers = headers
        self.status = 200

    def read(self, size=-1):
        if size is None or size < 0:
            chunk, self._body = self._body, b""
            return chunk
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


if __name__ == "__main__":
    unittest.main()
