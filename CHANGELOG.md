# Changelog

All notable changes to **Ratings Updatarr** (`script.ratings.updatarr`) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.3.2] - 2026-11-21

### Added
- **Configurable Database Backend:** Added a **Database** settings tab (`db_mode`: `jsonrpc`, `auto`, `manual`) supporting direct **MariaDB / MySQL** (bundled pure-Python `pymysql`) and **SQLite** connections with configurable server/host, port, username, password, database name (with automatic schema version resolution), or `advancedsettings.xml` auto-detection + database name override.
- **Database Connection Test:** Added **Test database connection** (`action=testdb`) in Database settings and added the active Video Library target to **Status & test keys**.

---

## [1.3.1] - 2026-11-20

### Added
- **Status & Key Tester:** Added **Status & test keys** (`action=status`) showing local IMDb index stats/size, daily UTC quotas, TMDbHelper status, and live API key checks for enabled online providers.
- **Dry-Run Preview, Vote Threshold & Change Log:** Added **Preview changes only (dry run)** (`dry_run`), `vote_change_percent` vote-growth refresh threshold, and per-item rating diff logs stored in `runs.json` and shown in **Recent runs**.
- **Optional `.nfo` Writeback:** Added **Update existing .nfo files** (`write_nfo`) to update `<ratings>` and missing `<uniqueid>` tags in existing sidecar `.nfo` files.
- **Menu & RunScript Shortcuts:** Added **Recently added** to the main menu and `episodes=true` RunScript flag for `tvshow`.

### Changed
- **TMDb Season Batching & HTTP Keep-Alive:** Fetches `/tv/{id}/season/{s}` once per season when episode IMDb IDs are known locally, adds `http.client` keep-alive connection pooling and transient `URLError` retries in `JsonClient`, and updates `USER_AGENT` to `1.3`.
- **Smaller IMDb SQLite Index:** Skips unrated episodes during `build_index()`, shrinking `imdb.sqlite` by ~75% and speeding up indexing.

---

## [1.3.0] - 2026-11-19

### Added
- **MDbList Provider & New Rating Types:** Added `use_mdblist`, `mdblist_key`, and `mdblist_daily_limit` settings with rate limiting and UTC daily quota tracking, plus support for Rotten Tomatoes Audience (`tomatometerallaudience`), Letterboxd (`letterboxd`, scaled 0–5 to 0–10), and MyAnimeList (`mal`) as storable and default ratings.
- **TV Show & Season Context Menu Actions:** Added **This TV show and its episodes** (`run_show`) on TV shows and enabled the context menu on seasons for **This season's episodes** (`run_season` and `mediatype=season`).
- **Recently Added Filter, Auto-Scan & Playback Pause:** Added **Only items added within (days)** (`recent_days`), **Update newly added items after a library scan** (`update_on_scan`), and **Wait while video is playing** (`pause_during_playback`).

### Changed
- **Faster IMDb SQLite Indexing & Progress Percentages:** Reports download and row-insert percentages in `ImdbData.refresh()` / `build_index()`, uses memory temp store and exclusive locking, and creates the episode lookup index after bulk inserting rows.
- **Quota Prioritization:** Prioritizes library items missing OMDb or MDbList ratings so large libraries advance across daily quota limits.

### Fixed
- **Parent Show IMDb Resolution:** Resolves missing parent TV show IMDb IDs via TMDbHelper, TMDb, or Trakt when updating episodes and saves discovered IDs back to the show.
- **TMDbHelper Cache Filtering & `any_active()`:** `Config.any_active()` checks whether TMDbHelper is actually installed and enabled alongside a source toggle, `_apply_helper()` respects disabled source toggles, and `HelperBridge` reads cached OMDb, MDbList, and info properties.
- **Run Failure Persistence & Cancelled Schedule Tracking:** Saves up to 20 per-item failure lines into `runs.json` and displays them in **Recent runs**; cancelled scheduled runs no longer overwrite `last_run`.
- **SQLite Read/Write Handle Ordering & Artwork Paths:** Closes cached read-only handles at the start of `_write_meta()`; moved `icon.png` and `fanart.png` to the add-on root and updated `addon.xml` `<assets>`.

---

## [1.2.9] - 2026-11-16

### Fixed
- Installs cleanly on Kodi Omega 21.2 (removed `xbmc.addon` version import restriction).

---

## [1.2.8] - 2026-11-14

### Changed
- Uses TMDbHelper's cached TMDb score when no TMDb API key is set.

---

## [1.2.7] - 2026-11-14

### Added
- Shows a summary dialog after a finished foreground update.

---

## [1.2.6] - 2026-11-13

### Fixed
- OMDb requests use the documented `apikey` and IMDb ID (`i`) lookup parameters.

---

## [1.2.5] - 2026-11-13

### Changed
- Documented OMDb API key requirement and 1,000 requests/day free allowance.

---

## [1.2.4] - 2026-11-12

### Added
- Added **Only update missing ratings** (`update_missing_only`) option to write only ratings an item does not already have.

---

## [1.2.3] - 2026-11-12

### Added
- Added **Refresh IMDb data now** action in add-on settings and menu in addition to the automatic refresh schedule.

---

## [1.2.2] - 2026-11-11

### Added
- Added **IMDb data folder** setting to choose a custom local folder for IMDb dataset downloads and the SQLite index.

---

## [1.2.1] - 2026-11-11

### Changed
- Made **Local IMDb database** an explicit provider toggle alongside the other providers.
- Applied media-type switches consistently to **All video** and scheduled runs.

---

## [1.2.0] - 2026-11-11

### Added
- Reuses TMDb scores and IDs already cached by TMDbHelper.
- Enforces provider rate limits and daily allowances for TMDb, Trakt, and OMDb.

---

## [1.1.0] - 2026-11-11

### Added
- Local IMDb dataset support (`title.ratings.tsv.gz` and `title.episode.tsv.gz`), refreshed every 30 days by default.
- Daily clock-time and hourly interval scheduling, background runs, run summary, 10-run log, and **Minimum rating change** threshold.

---

## [1.0.0] - 2026-11-11

### Added
- Initial release supporting movies, TV shows, episodes, context menu actions, and optional scheduled updates.
