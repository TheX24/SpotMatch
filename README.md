# SpotMatch

Find alternate Spotify IDs for the same recording without a Spotify login or developer API key.

## Use the Windows app

1. Open `SpotMatch.exe`.
2. Paste a Spotify track ID, URI, or URL.
3. Click **Find matches**.
4. Ctrl/Shift-click to select results, then copy or export them in the Spicy Lyrics bot format: `id1,id2,id3` with no spaces.
5. Double-click a result to open it in the Spotify app when installed, with a browser fallback.

The original ID is excluded. SpotMatch runs several broad searches, removes duplicates, and returns up to 100 ranked alternatives. Results are ranked by title (55%), artists (30%), and duration (15%).

Search options:

- **Quick, Balanced, Deep, and Exhaustive are presets only.** Choosing one fills the editable limits below it.
- Open **Show advanced options** to configure search pages (50 results each, up to Spotify's 1,000-result ceiling), repeated search passes, artist releases, global albums, playlists, tracks per playlist, title-similarity floor, and variant queries independently.
- Repeated passes rerun Spotify search and merge/deduplicate the changing results, helping catch compilation IDs that Spotify ranks differently each time.
- The Exhaustive preset uses 20 pages × 3 passes, 200 artist releases, 150 global albums, 60 playlists with up to 500 tracks each, and cover/remix/live/instrumental query expansion. This can take several minutes.
- Filter by minimum match score, maximum duration difference, and exact normalized title.

Treat scores as suggestions: live, remastered, clean, explicit, sped-up, and region-specific releases can still require human review.

## Run from source

Requires Python 3.10 or newer:

```powershell
python -m pip install -r requirements.txt
python app.py
```

SpotMatch uses SpotifyScraper and Spotify's undocumented public web endpoints. It can break if Spotify changes those endpoints. No Spotify account, cookie, password, or token is requested or stored.

Long searches show phase-specific progress for repeated queries, pagination, artist discographies, album batches, and playlist batches.
