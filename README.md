# SpotMatch

Find alternate Spotify IDs for the same recording without a Spotify login or developer API key.

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

## Use SpotMatch

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

## Run from source

Requires Python 3.10 or newer:

```powershell
python -m pip install -r requirements.txt
python app.py
```

SpotMatch uses SpotifyScraper and Spotify's undocumented public web endpoints. It can break if Spotify changes those endpoints. No Spotify account, cookie, password, or token is requested or stored.

Long searches show phase-specific progress for repeated queries, pagination, artist discographies, album batches, and playlist batches.
