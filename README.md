# Ratings Updatarr

**Project page:** [jonnypuma.github.io/#/script.ratings.updatarr](https://jonnypuma.github.io/#/script.ratings.updatarr) · **All add-ons and apps by jonnypuma:** [jonnypuma.github.io](https://jonnypuma.github.io/)

Ratings Updatarr refreshes scraped ratings in a Kodi 22 Piers library. It can update movies, TV shows, episodes, or all three. Your personal star rating is left alone. Seasons only store that personal rating, so they are not part of an update.

TMDb is the main online source. IMDb scores come from IMDb's own ratings and episode files, downloaded about every 30 days for personal, non-commercial use. OMDb adds Rotten Tomatoes critic and Metacritic scores. MDbList can supply IMDb, TMDb, Trakt, Rotten Tomatoes critic and audience, Metacritic, Letterboxd, and MyAnimeList scores in one call. Trakt adds the public Trakt score. This product uses the TMDb API but is not endorsed or certified by TMDb. The IMDb files are described at [developer.imdb.com/non-commercial-datasets](https://developer.imdb.com/non-commercial-datasets).

## Install

1. Zip this folder so the archive contains `script.ratings.updatarr/addon.xml`, or copy the folder into the Kodi `addons` directory.
2. In Kodi, use Install from zip file, or restart Kodi if you copied it in.
3. Open the addon settings. IMDb datasets are on by default. Add a TMDb API key from [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) when you also want TMDb scores.

Optional keys:

- OMDb requires an API key from [omdbapi.com/apikey.aspx](https://www.omdbapi.com/apikey.aspx). Requests go to `https://www.omdbapi.com/?apikey=[yourkey]&i=[IMDb id]`, as described at [omdbapi.com](https://www.omdbapi.com/). The free key allows 1,000 requests a day. Without a key, OMDb is skipped. Leave it off for a large episode library unless the key allows more. With IMDb datasets on, OMDb is used for Rotten Tomatoes and Metacritic.
- MDbList requires an API key from [mdblist.com/preferences](https://mdblist.com/preferences). A free key allows 1,000 requests a day. When MDbList already supplies Rotten Tomatoes critic, Metacritic, or Trakt scores, OMDb and Trakt are skipped for that item.
- Trakt client ID, from a Trakt application. Public ratings do not use the Trakt Kodi addon, and the client secret is not used.

## IMDb datasets

The addon downloads `title.ratings.tsv.gz` and `title.episode.tsv.gz` from `datasets.imdb.com`, builds a local index, and deletes the downloaded files. The first download needs about 800 MB free. It checks again after the number of days in the settings (30 by default, from 7 to 365). If IMDb says the files have not changed, they are not downloaded again. A failed download waits a day before the next automatic try. **Refresh IMDb data now** in the settings, or on the addon menu, downloads immediately and leaves that automatic check in place.

**IMDb data folder** chooses where those downloads and the SQLite index are stored. Leave it empty to use the addon data folder `imdb`. You can put your own copies of the two files in an `import` folder inside that path. Files newer than the refresh interval are used and are not deleted. Changing the folder does not move an index that was already built.

A score from this index is used instead of the IMDb score from OMDb.

## TMDbHelper

If TMDbHelper is installed, **Use TMDbHelper cache** reads a TMDb score and the IDs TMDbHelper has already stored. That works with no TMDb key of your own, and the empty-key warning is not shown. A title it has not cached is requested only when a TMDb key is set. TMDbHelper does not share its own key with other addons. OMDb and Trakt are still requested here.

## Provider limits

Requests are paced to the limits those services publish:

- TMDb: at most 20 requests a second, under the roughly 40 a second ceiling, so another addon on the same address still has room. A 429 waits for the `Retry-After` time, up to 5 minutes.
- Trakt: at most 450 GET requests every 5 minutes, under the published 500. When Trakt sends `X-Ratelimit` with no requests remaining, the next call waits until the reset time.
- OMDb: at most 5 requests a second, and no more than the daily number in the settings. A free key allows 1,000 a day. The count is remembered until the next UTC day. The run also stops when OMDb says the limit is reached.
- MDbList: at most 1 request a second, and no more than the daily number in the settings. A free key allows 1,000 a day. Items missing quota-limited ratings are checked first so large libraries progress across runs.

The pause between library items is still there as well. It does not replace these limits.

## Providers

Sources in the addon settings are the providers used for an update. Turn one off to leave it out.

- **Local IMDb database** is a provider. It is on by default and supplies the IMDb score from the files stored on this device.
- **TMDb**, **OMDb**, **MDbList**, and **Trakt** are the online providers. Each needs its own key while it is on.
- **Use TMDbHelper cache** reuses ratings and IDs TMDbHelper already stored. It does not replace the other providers.

## Run it

The addon menu can update one media type, or **All video**. All video and the schedule use the media types switched on under Library. Choosing one type from the menu still updates that type. One item opened from the context menu is always updated.

On a movie, TV show, season, or episode in the library, the context menu item **Update ratings** can refresh that item, a TV show and all of its episodes, a season's episodes, every switched-on item of its type, or all switched-on types.

A finished foreground run opens a summary with how many items were checked and how many were updated. A background run shows a notification and stores the same result. **Recent runs** keeps the last 10, including cancelled runs and item failures.

The library schedule is off until you enable it. It can run every day at one or more local times, such as `03:00, 15:30`, or after a number of hours. You can also enable automatic updates after a video library scan finishes, filter updates to items added within the last N days, and pause background updates while a video is playing. Scheduled and manual runs can each be set to the background. `background=true` or `background=false` on RunScript overrides the manual setting for that call.

## Rating changes

By default a rating is written only when the new score is more than 0.2 away from the stored score. A move from 8.0 to 8.1 is left alone. A move from 8.0 to 8.3 is written. Set **Minimum rating change** to 0 to write every fetched rating, including ratings that have not changed. A new rating is always written. **Only update missing ratings** leaves every stored score as it is and writes a source only when the item does not have it yet. Your personal star rating is never changed.

## RunScript

```
RunScript(script.ratings.updatarr)
RunScript(script.ratings.updatarr,mediatype=movies)
RunScript(script.ratings.updatarr,mediatype=tvshows)
RunScript(script.ratings.updatarr,mediatype=episodes)
RunScript(script.ratings.updatarr,mediatype=all)
RunScript(script.ratings.updatarr,mediatype=movies+episodes)
RunScript(script.ratings.updatarr,mediatype=movie,dbid=12)
RunScript(script.ratings.updatarr,mediatype=episodes,background=true)
```

`content=` is accepted as well as `mediatype=`.

## What gets written

The addon writes the `ratings` field through JSON-RPC. A TMDb score is stored as `themoviedb`, or under the existing `tmdb` name when the library already uses that. IMDb is `imdb`, Rotten Tomatoes critic is `tomatometerallcritics`, Rotten Tomatoes audience is `tomatometerallaudience`, Metacritic is `metacritic`, Letterboxd is `letterboxd`, MyAnimeList is `mal`, and Trakt is `trakt`. Scores are on a 0–10 scale, which is what Kodi stores. An IMDb score from the local dataset replaces the IMDb score returned by OMDb or MDbList for that item.

Missing IMDb, TMDb, and TVDb IDs can be filled in. An ID that is already stored is not replaced. NFO files are not rewritten; export the library if you want the new ratings on disk.

Items with no IMDb, TMDb, or TVDb ID are skipped. Episode lookups use the parent show's TMDb ID plus the season and episode number.
