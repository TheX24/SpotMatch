"""Spotify matching core shared by the desktop app and the web service."""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable, TypeVar

from spotify_scraper import AsyncSpotifyClient, RateLimit, SpotifyScraperError
from spotify_scraper.api import parse_entities
from spotify_scraper.http.ratelimit import PARTNER_API_HOST


TRACK_ID_RE = re.compile(r"(?:spotify:track:|open\.spotify\.com/track/)?([A-Za-z0-9]{22})")

PRESETS = {
    "Quick": dict(search_pages=1, search_repeats=1, releases=0, albums=0, playlists=0, playlist_tracks=0, title_floor=58, variants=False),
    "Balanced": dict(search_pages=1, search_repeats=2, releases=50, albums=0, playlists=0, playlist_tracks=0, title_floor=58, variants=False),
    "Deep": dict(search_pages=1, search_repeats=3, releases=200, albums=0, playlists=0, playlist_tracks=0, title_floor=58, variants=False),
    "Exhaustive": dict(search_pages=20, search_repeats=3, releases=200, albums=150, playlists=60, playlist_tracks=500, title_floor=58, variants=True),
}


def extract_track_id(value: str) -> str:
    match = TRACK_ID_RE.search(value.strip())
    if not match:
        raise ValueError("Paste a 22-character Spotify track ID or track URL.")
    return match.group(1)


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = value.casefold().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", value))


def similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize(left), normalize(right)).ratio()


def format_duration(milliseconds: int | None) -> str:
    seconds = round((milliseconds or 0) / 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"


@dataclass(frozen=True)
class Match:
    track_id: str
    title: str
    artists: str
    album: str
    duration_ms: int
    score: int
    duration_delta_ms: int


@dataclass(frozen=True)
class SearchOptions:
    depth: str = "Balanced"
    minimum_score: int = 60
    maximum_duration_seconds: int = 10
    exact_title: bool = False
    search_pages: int = 1
    search_repeats: int = 2
    releases_per_artist: int = 50
    global_albums: int = 0
    global_playlists: int = 0
    tracks_per_playlist: int = 100
    minimum_title_similarity: int = 58
    variant_queries: bool = False


# Deep search pagination without discography or playlist scans: ~80 requests per
# lookup, which measured at ~97% of Exhaustive's high-confidence matches.
WEB_SEARCH_OPTIONS = SearchOptions(depth="Web", search_pages=20, search_repeats=3, releases_per_artist=0)


def score_candidate(source, candidate, album_name: str | None = None) -> Match:
    source_artists = ", ".join(artist.name for artist in source.artists)
    candidate_artists = ", ".join(artist.name for artist in candidate.artists)
    title_score = similarity(source.name, candidate.name)
    artist_score = similarity(source_artists, candidate_artists)
    delta = abs((source.duration_ms or 0) - (candidate.duration_ms or 0))
    duration_score = max(0.0, 1.0 - delta / 15_000)
    score = round(100 * (title_score * 0.55 + artist_score * 0.30 + duration_score * 0.15))
    return Match(
        track_id=candidate.id,
        title=candidate.name,
        artists=candidate_artists,
        album=album_name or (candidate.album.name if candidate.album else "Unknown album"),
        duration_ms=candidate.duration_ms or 0,
        score=score,
        duration_delta_ms=delta,
    )


ProgressCallback = Callable[[str, int, int], None]
_T = TypeVar("_T")
MAX_CONCURRENCY = 20
ENTITY_CONCURRENCY = 5
PARTNER_API_RATE_LIMIT = RateLimit(per_second=128, burst=20)
OPEN_SPOTIFY_HOST = "open.spotify.com"
OPEN_SPOTIFY_RATE_LIMIT = RateLimit(per_second=16, burst=5)


async def _bounded_gather(
    operations: Sequence[Callable[[], Awaitable[_T]]],
    on_complete: Callable[[int], None] | None = None,
    *,
    limit: int = MAX_CONCURRENCY,
) -> list[_T]:
    semaphore = asyncio.Semaphore(limit)
    completed = 0

    async def run(operation: Callable[[], Awaitable[_T]]) -> _T:
        nonlocal completed
        async with semaphore:
            result = await operation()
        completed += 1
        if on_complete:
            on_complete(completed)
        return result

    return list(await asyncio.gather(*(run(operation) for operation in operations)))


async def paginated_track_search_async(
    client: AsyncSpotifyClient,
    query: str,
    maximum: int = 1000,
    progress: ProgressCallback | None = None,
) -> list[object]:
    """Page Spotify's private web search beyond SpotifyScraper's first page."""
    offset = 0
    page_size = 50
    seen: set[str] = set()
    tracks: list[object] = []
    token = await client._tokens.token()
    while offset < maximum:
        if progress:
            progress("Paginating Spotify results", offset // page_size + 1, max(1, (maximum + page_size - 1) // page_size))
        data = await client._search_request(
            query,
            token,
            {"limit": page_size, "offset": offset},
            None,
        )
        union = data.get("searchV2")
        results = parse_entities.parse_search_results(union, query=query)
        new_tracks = [track for track in results.tracks if track.id not in seen]
        if not results.tracks:
            break
        for track in new_tracks:
            seen.add(track.id)
            tracks.append(track)
        offset += page_size
        if offset >= min(results.total, maximum):
            break
    return tracks


async def _find_matches_async(
    raw_id: str,
    options: SearchOptions | None = None,
    progress: ProgressCallback | None = None,
    proxy: str | None = None,
) -> tuple[object, list[Match], str]:
    options = options or SearchOptions()
    track_id = extract_track_id(raw_id)
    async with AsyncSpotifyClient(
        proxy=proxy,
        max_concurrency=MAX_CONCURRENCY,
        host_rate_limits={
            PARTNER_API_HOST: PARTNER_API_RATE_LIMIT,
            OPEN_SPOTIFY_HOST: OPEN_SPOTIFY_RATE_LIMIT,
        },
    ) as client:
        if progress:
            progress("Reading source track", 0, 1)
        source = await client.get_track(track_id)
        if progress:
            progress("Reading source track", 1, 1)
        artist_names = " ".join(artist.name for artist in source.artists)
        primary_artist = source.artists[0].name if source.artists else ""
        album_name = source.album.name if source.album else ""
        base_queries = (
            source.name,
            f"{source.name} {primary_artist}",
            f"{source.name} {artist_names}",
            f"{source.name} {album_name}",
        )
        variant_terms = ("cover", "remix", "instrumental", "live", "remaster", "karaoke", "sped up", "slowed")
        queries = dict.fromkeys(
            query.strip()
            for query in base_queries + (tuple(f"{source.name} {term}" for term in variant_terms) if options.variant_queries else ())
            if query.strip()
        )

        candidates: dict[str, Match] = {}
        discovered_album_ids: list[str] = []
        discovered_playlist_ids: list[str] = []
        seen_discovered_albums: set[str] = set()
        seen_discovered_playlists: set[str] = set()
        search_total = max(1, options.search_repeats * len(queries))
        search_specs: list[tuple[int, int, str]] = []
        for pass_index in range(options.search_repeats):
            for query_index, query in enumerate(queries):
                search_specs.append((pass_index, query_index, query))
        extra_types = (("album",) if options.global_albums else ()) + (("playlist",) if options.global_playlists else ())
        entity_types = ("track",) + extra_types

        async def run_search(query: str):
            try:
                return await client.search(query, types=entity_types, limit=50)
            except SpotifyScraperError:
                return None

        operations = [lambda query=query: run_search(query) for _pass, _index, query in search_specs]

        def search_complete(current: int) -> None:
            if progress:
                pass_index, query_index, _query = search_specs[current - 1]
                progress(
                    f"Search pass {pass_index + 1}/{options.search_repeats} · query {query_index + 1}/{len(queries)}",
                    current,
                    search_total,
                )

        search_results = await _bounded_gather(operations, search_complete)
        for results in search_results:
            if results is None:
                continue
            for candidate in results.tracks:
                if candidate.id != track_id:
                    match = score_candidate(source, candidate)
                    previous = candidates.get(candidate.id)
                    if previous is None or match.score > previous.score:
                        candidates[candidate.id] = match
            for album in results.albums:
                if album.id not in seen_discovered_albums:
                    seen_discovered_albums.add(album.id)
                    discovered_album_ids.append(album.id)
            for playlist in results.playlists:
                if playlist.id not in seen_discovered_playlists:
                    seen_discovered_playlists.add(playlist.id)
                    discovered_playlist_ids.append(playlist.id)

        paginated_hits = 0
        if options.search_pages > 1:
            # Spotify stops at 1,000 results per query, so each pass pages a different
            # wording: measured on Spicy Lyrics uploads, this reaches far more
            # releases of very common songs than repeating one query.
            paginated_queries = list(dict.fromkeys(
                query.strip()
                for query in (f"{source.name} {primary_artist}", f"{primary_artist} {source.name}", source.name)
                if query.strip()
            ))
            pagination_total = options.search_repeats * options.search_pages
            pagination_completed = 0
            pagination_reported: set[tuple[int, int]] = set()

            async def run_paginated_pass(pass_index: int):
                def page_progress(_message: str, current: int, _total: int) -> None:
                    nonlocal pagination_completed
                    progress_key = (pass_index, current)
                    if progress_key in pagination_reported:
                        return
                    pagination_reported.add(progress_key)
                    pagination_completed += 1
                    if progress:
                        progress(
                            f"Paginated pass {pass_index + 1}/{options.search_repeats} · page {current}/{options.search_pages}",
                            pagination_completed,
                            pagination_total,
                        )

                try:
                    return await paginated_track_search_async(
                        client,
                        paginated_queries[pass_index % len(paginated_queries)],
                        options.search_pages * 50,
                        page_progress,
                    )
                except SpotifyScraperError:
                    return None

            paginated_passes = await _bounded_gather(
                [lambda pass_index=pass_index: run_paginated_pass(pass_index) for pass_index in range(options.search_repeats)]
            )
            for pass_index, pass_tracks in enumerate(paginated_passes):
                if pass_tracks is None:
                    paginated_passes[pass_index] = await run_paginated_pass(pass_index)
            for pass_tracks in paginated_passes:
                if pass_tracks is None:
                    continue
                for candidate in pass_tracks:
                    paginated_hits += 1
                    if candidate.id == track_id:
                        continue
                    match = score_candidate(source, candidate)
                    previous = candidates.get(candidate.id)
                    if previous is None or match.score > previous.score:
                        candidates[candidate.id] = match

        release_limit = options.releases_per_artist
        release_ids: list[str] = []
        seen_releases: set[str] = set()
        release_artists = tuple(source.artists if release_limit else ())
        async def read_discography(artist):
            if not artist.id:
                return ()
            return await client.get_discography(artist.id, max_releases=release_limit)

        def discography_complete(current: int) -> None:
            if progress:
                progress("Reading credited-artist discographies", current, len(release_artists))

        discographies = await _bounded_gather(
            [lambda artist=artist: read_discography(artist) for artist in release_artists],
            discography_complete,
        )
        for releases in discographies:
            for release in releases:
                if release.id not in seen_releases:
                    seen_releases.add(release.id)
                    release_ids.append(release.id)

        all_album_ids = list(dict.fromkeys(release_ids + discovered_album_ids[: options.global_albums]))
        albums_scanned = 0
        async def read_album(album_id: str):
            try:
                return await client.get_album(album_id)
            except SpotifyScraperError:
                return None

        def album_complete(current: int) -> None:
            if progress:
                progress("Scanning albums", current, len(all_album_ids))

        albums = await _bounded_gather(
            [lambda album_id=album_id: read_album(album_id) for album_id in all_album_ids],
            album_complete,
            limit=ENTITY_CONCURRENCY,
        )
        for album in albums:
            if album is None:
                continue
            albums_scanned += 1
            for candidate in album.tracks:
                if candidate.id == track_id:
                    continue
                if similarity(source.name, candidate.name) < options.minimum_title_similarity / 100:
                    continue
                match = score_candidate(source, candidate, album.name)
                previous = candidates.get(candidate.id)
                if previous is None or match.score > previous.score:
                    candidates[candidate.id] = match

        playlists_scanned = 0
        if options.global_playlists:
            # Playlist expansion catches compilation/Various Artists catalog IDs that
            # artist discographies and Spotify's top track results often omit.
            playlist_ids = discovered_playlist_ids[: options.global_playlists]
            async def read_playlist(playlist_id: str):
                try:
                    return await client.get_playlist(playlist_id, max_tracks=options.tracks_per_playlist)
                except SpotifyScraperError:
                    return None

            def playlist_complete(current: int) -> None:
                if progress:
                    progress("Scanning playlists", current, len(playlist_ids))

            playlists = await _bounded_gather(
                [lambda playlist_id=playlist_id: read_playlist(playlist_id) for playlist_id in playlist_ids],
                playlist_complete,
                limit=ENTITY_CONCURRENCY,
            )
            for playlist in playlists:
                if playlist is None:
                    continue
                playlists_scanned += 1
                for playlist_item in playlist.tracks:
                    candidate = playlist_item.track
                    if candidate.id == track_id or similarity(source.name, candidate.name) < options.minimum_title_similarity / 100:
                        continue
                    match = score_candidate(source, candidate)
                    previous = candidates.get(candidate.id)
                    if previous is None or match.score > previous.score:
                        candidates[candidate.id] = match

    matches = [
        match
        for match in candidates.values()
        if match.score >= options.minimum_score
        and match.duration_delta_ms <= options.maximum_duration_seconds * 1000
        and (not options.exact_title or normalize(match.title) == normalize(source.name))
    ]
    matches.sort(key=lambda item: (-item.score, item.duration_delta_ms, item.track_id))
    coverage = f"{albums_scanned} albums"
    if options.search_pages > 1 or options.global_playlists or options.variant_queries:
        coverage += f", {playlists_scanned} playlists, {options.search_repeats} search passes, {paginated_hits} paginated tracks checked"
    return source, matches[:1000], coverage


def find_matches(
    raw_id: str,
    options: SearchOptions | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[object, list[Match], str]:
    return asyncio.run(_find_matches_async(raw_id, options, progress))
