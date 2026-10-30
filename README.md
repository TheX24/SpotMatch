# SpotMatch

Find alternate Spotify IDs for the same recording without a Spotify login or developer API key.

Use it in the browser at **[spotmatch.tx24.dev](https://spotmatch.tx24.dev)**, download the desktop app, or host the web version yourself.

| Folder | What's there |
|---|---|
| `spotmatch_core.py` | The search and scoring code, shared by both apps |
| `web/` | Web page and API server, plus an optional Cloudflare WARP sidecar in `web/warp/` |
| `desktop/` | Tk desktop app, PyInstaller spec, Linux packaging, and `VERSION` for desktop releases |
| `tests/`, `scripts/` | Tests for both, release-tag validation |

## Download the desktop app

Stable `vX.Y.Z` tags publish four downloads on the matching GitHub Release:

- **Windows x64:** `SpotMatch-Windows-x64.zip`
- **Linux x86_64:** `SpotMatch-Linux-x86_64.AppImage`
- **macOS Intel:** `SpotMatch-macOS-x64.zip`
- **macOS Apple Silicon:** `SpotMatch-macOS-arm64.zip`

Extract the Windows or macOS ZIP, then open SpotMatch. The macOS builds are unsigned and unnotarized, so Gatekeeper may require you to approve the app explicitly in **System Settings → Privacy & Security**.

On Linux, mark the AppImage executable and run it:

```bash
chmod +x SpotMatch-Linux-x86_64.AppImage
./SpotMatch-Linux-x86_64.AppImage
```

If FUSE is unavailable, use AppImage's extraction fallback:

```bash
./SpotMatch-Linux-x86_64.AppImage --appimage-extract-and-run
```

SpotMatch checks for newer stable releases in the background at startup and only interrupts you when an update exists. Use **Check for updates** for a manual check that also reports when you are current or a check fails. Updates open the GitHub Release page; SpotMatch never downloads or installs them automatically.

Pushes to `dev` produce temporary test artifacts in GitHub Actions. Stable tags publish a GitHub Release after every platform build passes. Release notes are edited manually on the release page.

## Use the desktop app

1. Open SpotMatch.
2. Paste a Spotify track ID, URI, or URL.
3. Click **Find matches**.
4. Ctrl/Shift-click to select results, then copy or export them in the Spicy Lyrics bot format: `id1,id2,id3` with no spaces.
5. Double-click a result to open it in the Spotify app when installed, with a browser fallback.

The original ID is excluded. SpotMatch runs several broad searches, removes duplicates, and returns up to 100 ranked alternatives. Results are ranked by title (55%), artists (30%), and duration (15%).

Search options:

- **Quick, Balanced, Deep, and Exhaustive are presets only.** Choosing one fills the editable limits below it.
- Open **Show advanced options** to configure search pages (50 results each, up to Spotify's 1,000-result ceiling), repeated search passes, artist releases, global albums, playlists, tracks per playlist, title-similarity floor, and variant queries independently.
- Your last track input, preset, filters, and advanced search limits are restored the next time you open SpotMatch.
- Save the current filters and search limits as a named custom preset; custom presets can be replaced or deleted without changing the built-in presets.
- Repeated passes rerun Spotify search and merge/deduplicate the changing results, helping catch compilation IDs that Spotify ranks differently each time.
- The Exhaustive preset uses 20 pages × 3 passes, 200 artist releases, 150 global albums, 60 playlists with up to 500 tracks each, and cover/remix/live/instrumental query expansion. This can take several minutes.
- Filter by minimum match score, maximum duration difference, and exact normalized title.
- Use **Copy selected + source** to produce the same comma-separated format with the searched track ID first.

Treat scores as suggestions: live, remastered, clean, explicit, sped-up, and region-specific releases can still require human review.

## Run the desktop app from source

Requires Python 3.10 or newer:

```powershell
python -m pip install -r desktop/requirements.txt
python desktop/app.py
```

SpotMatch uses SpotifyScraper and Spotify's undocumented public web endpoints. It can break if Spotify changes those endpoints. No Spotify account, cookie, password, or token is requested or stored.

The desktop app shows phase-specific progress for repeated queries, pagination, artist discographies, album batches, and playlist batches.

## Web version

The web version runs one fixed deep search per track (Spotify search with deep pagination, no discography or playlist scans: about 80 Spotify requests and 40 seconds). Results are cached per track for a day, identical searches share one run, each visitor gets one search at a time, and searches beyond the concurrency limit wait in a queue.

On the page, click a row to select it, Shift-click to select a range, and Ctrl+A to select everything shown. **Copy IDs** gives the Spicy Lyrics bot format; when the list is longer than the bot's 4000-character paste box, use **Export .txt**. **Server** points the page at a different SpotMatch server, saved in that browser.

### Self-host with Docker

```bash
docker compose up -d --build
```

This starts the server on `127.0.0.1:8765` behind a Cloudflare WARP container, so Spotify sees a Cloudflare address instead of your server's. Starting the WARP container accepts [Cloudflare's WARP terms](https://www.cloudflare.com/application/terms/). To skip WARP, remove the `warp` service, `depends_on`, and `SPOTMATCH_PROXY` from `docker-compose.yml`.

Put a reverse proxy in front of it. Progress is streamed, so turn off response buffering (Caddy: `flush_interval -1`; nginx: `proxy_buffering off`).

| Setting | Default | What it does |
|---|---|---|
| `SPOTMATCH_PROXY` | none | Proxy for Spotify requests, e.g. `socks5://warp:40001` |
| `SPOTMATCH_MAX_SEARCHES` | `6` | Searches that run at once; more wait in a queue |
| `SPOTMATCH_CACHE_SECONDS` | `86400` | How long a finished result is reused |
| `SPOTMATCH_ALLOWED_ORIGINS` | `*` | Sites allowed to call the API from a browser |
| `SPOTMATCH_MAX_QUEUE` | `30` | Searches that may wait for a slot; beyond that, new ones get "busy, try later" |
| `SPOTMATCH_HOURLY_LIMIT` | `20` | New searches per visitor per hour (cached results don't count) |
| `SPOTMATCH_MAX_STREAMS` | `3` | Open result streams per visitor |
| `SPOTMATCH_SEARCH_TIMEOUT` | `180` | Seconds before a stuck search is stopped |
| `SPOTMATCH_CACHE_ENTRIES` | `300` | Finished results kept in memory |
| `SPOTMATCH_RELEASES_PER_ARTIST` | `0` | Also scan this many releases per credited artist. Finds album copies Spotify hides from search, at about one extra request per release |
| `SPOTMATCH_CLIENT_IP_HEADER` | unset | Header your reverse proxy sets to the visitor's address (e.g. `X-Real-IP`). Only set it if nothing can reach the server except through that proxy |

The compose file also caps memory, CPU, processes and log size, and the server refuses connections past 300 at once.

To serve the page somewhere else (any static host), copy `web/static/` and set `apiBase` in `web/static/config.js` to your server's URL.

### Run without Docker

```powershell
python -m pip install -r web/requirements.txt
python -m uvicorn web.server:app --port 8765
```

### API

`GET /api/match?track=<ID, URI, or link>` streams server-sent events: `queued` (with `position`), `progress` (`message`, `current`, `total`), then one `result` (`source`, `matches`, `coverage`) or `error` (`message`). `GET /api/health` reports load.
