from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spotify_scraper import NotFoundError

from spotmatch_core import SearchOptions, find_matches


TRACK_ID = "A" * 22
CANDIDATE_ID = "B" * 22


def track(track_id: str, name: str = "Song"):
    artist = SimpleNamespace(id="artist-id", name="Artist")
    album = SimpleNamespace(name="Album")
    return SimpleNamespace(
        id=track_id,
        name=name,
        artists=(artist,),
        album=album,
        duration_ms=180_000,
    )


def search_results(*tracks, albums=(), playlists=()):
    return SimpleNamespace(tracks=tracks, albums=albums, playlists=playlists, total=len(tracks))


class AsyncClientContext:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *_args):
        return False


class SearchResilienceTests(unittest.TestCase):
    def test_failed_standard_query_does_not_abort_later_queries(self):
        source = track(TRACK_ID)
        candidate = track(CANDIDATE_ID)
        client = SimpleNamespace(
            get_track=AsyncMock(return_value=source),
            search=AsyncMock(
                side_effect=[
                    NotFoundError("Spotify pathfinder returned a null entity union."),
                    search_results(candidate),
                    search_results(),
                ]
            ),
        )

        with patch("spotmatch_core.AsyncSpotifyClient", return_value=AsyncClientContext(client)):
            _, matches, _ = find_matches(
                TRACK_ID,
                SearchOptions(search_repeats=1, releases_per_artist=0),
            )

        self.assertEqual([match.track_id for match in matches], [CANDIDATE_ID])
        self.assertEqual(client.search.await_count, 3)

    def test_failed_paginated_pass_does_not_abort_later_passes(self):
        source = track(TRACK_ID)
        candidate = track(CANDIDATE_ID)
        client = SimpleNamespace(
            get_track=AsyncMock(return_value=source),
            search=AsyncMock(return_value=search_results()),
        )

        paginated_search = AsyncMock(
            side_effect=[
                NotFoundError("Spotify pathfinder returned a null entity union."),
                [candidate],
                [candidate],
            ]
        )

        with (
            patch("spotmatch_core.AsyncSpotifyClient", return_value=AsyncClientContext(client)),
            patch("spotmatch_core.paginated_track_search_async", paginated_search),
        ):
            _, matches, _ = find_matches(
                TRACK_ID,
                SearchOptions(
                    search_pages=2,
                    search_repeats=2,
                    releases_per_artist=0,
                ),
            )

        self.assertEqual([match.track_id for match in matches], [CANDIDATE_ID])
        self.assertEqual(paginated_search.await_count, 3)

    def test_paginated_passes_rotate_query_wordings(self):
        source = track(TRACK_ID)
        client = SimpleNamespace(
            get_track=AsyncMock(return_value=source),
            search=AsyncMock(return_value=search_results()),
        )
        paginated_search = AsyncMock(return_value=[])

        with (
            patch("spotmatch_core.AsyncSpotifyClient", return_value=AsyncClientContext(client)),
            patch("spotmatch_core.paginated_track_search_async", paginated_search),
        ):
            find_matches(TRACK_ID, SearchOptions(search_pages=2, search_repeats=3, releases_per_artist=0))

        queries = sorted(call.args[1] for call in paginated_search.await_args_list)
        self.assertEqual(queries, ["Artist Song", "Song", "Song Artist"])


class SearchConcurrencyTests(unittest.TestCase):
    def test_standard_queries_overlap_without_exceeding_twenty_operations(self):
        source = track(TRACK_ID)
        active = 0
        maximum_active = 0

        async def delayed_search(_query, **_kwargs):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return search_results()

        client = SimpleNamespace(
            get_track=AsyncMock(return_value=source),
            search=delayed_search,
        )

        with patch("spotmatch_core.AsyncSpotifyClient", return_value=AsyncClientContext(client)):
            find_matches(
                TRACK_ID,
                SearchOptions(
                    search_repeats=3,
                    releases_per_artist=0,
                    variant_queries=True,
                ),
            )

        self.assertEqual(maximum_active, 20)

    def test_spotify_hosts_use_their_proven_high_throughput_rates(self):
        source = track(TRACK_ID)
        client = SimpleNamespace(
            get_track=AsyncMock(return_value=source),
            search=AsyncMock(return_value=search_results()),
        )
        constructor_options = {}

        def client_factory(**options):
            constructor_options.update(options)
            return AsyncClientContext(client)

        with patch("spotmatch_core.AsyncSpotifyClient", side_effect=client_factory):
            find_matches(
                TRACK_ID,
                SearchOptions(search_repeats=1, releases_per_artist=0),
            )

        partner_rate = constructor_options["host_rate_limits"]["api-partner.spotify.com"]
        embed_rate = constructor_options["host_rate_limits"]["open.spotify.com"]
        self.assertEqual((partner_rate.per_second, partner_rate.burst), (128, 20))
        self.assertEqual((embed_rate.per_second, embed_rate.burst), (16, 5))

    def test_progress_is_monotonic_when_queries_finish_out_of_order(self):
        source = track(TRACK_ID)
        delays = iter((0.03, 0.01, 0.02))

        async def delayed_search(_query, **_kwargs):
            await asyncio.sleep(next(delays))
            return search_results()

        client = SimpleNamespace(
            get_track=AsyncMock(return_value=source),
            search=delayed_search,
        )
        progress: list[tuple[str, int, int]] = []

        with patch("spotmatch_core.AsyncSpotifyClient", return_value=AsyncClientContext(client)):
            find_matches(
                TRACK_ID,
                SearchOptions(search_repeats=1, releases_per_artist=0),
                lambda message, current, total: progress.append((message, current, total)),
            )

        search_updates = [current for message, current, _total in progress if message.startswith("Search pass")]
        self.assertEqual(search_updates, [1, 2, 3])

    def test_paginated_progress_never_moves_backward_across_concurrent_passes(self):
        source = track(TRACK_ID)
        client = SimpleNamespace(
            get_track=AsyncMock(return_value=source),
            search=AsyncMock(return_value=search_results()),
        )
        progress: list[tuple[str, int, int]] = []

        async def paginated(_client, _query, _maximum, page_progress):
            if not getattr(paginated, "started", False):
                paginated.started = True
                await asyncio.sleep(0.02)
            page_progress("page", 1, 2)
            await asyncio.sleep(0)
            page_progress("page", 2, 2)
            return []

        with (
            patch("spotmatch_core.AsyncSpotifyClient", return_value=AsyncClientContext(client)),
            patch("spotmatch_core.paginated_track_search_async", side_effect=paginated),
        ):
            find_matches(
                TRACK_ID,
                SearchOptions(search_pages=2, search_repeats=2, releases_per_artist=0),
                lambda message, current, total: progress.append((message, current, total)),
            )

        pagination_updates = [current for message, current, _total in progress if message.startswith("Paginated")]
        self.assertEqual(pagination_updates, [1, 2, 3, 4])

    def test_album_expansion_overlaps_and_isolates_individual_failures(self):
        source = track(TRACK_ID)
        releases = tuple(SimpleNamespace(id=f"album-{index}") for index in range(21))
        active = 0
        maximum_active = 0

        async def get_album(album_id):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            if album_id == "album-2":
                raise NotFoundError("missing album")
            return SimpleNamespace(name=album_id, tracks=())

        client = SimpleNamespace(
            get_track=AsyncMock(return_value=source),
            search=AsyncMock(return_value=search_results()),
            get_discography=AsyncMock(return_value=releases),
            get_album=get_album,
        )

        with patch("spotmatch_core.AsyncSpotifyClient", return_value=AsyncClientContext(client)):
            _source, _matches, coverage = find_matches(
                TRACK_ID,
                SearchOptions(search_repeats=1, releases_per_artist=21),
            )

        self.assertEqual(maximum_active, 5)
        self.assertEqual(coverage, "20 albums")

    def test_playlist_expansion_overlaps_without_changing_discovery_order(self):
        source = track(TRACK_ID)
        playlist_refs = tuple(SimpleNamespace(id=f"playlist-{index}") for index in range(21))
        active = 0
        maximum_active = 0

        async def get_playlist(playlist_id, *, max_tracks):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return SimpleNamespace(id=playlist_id, tracks=(), max_tracks=max_tracks)

        client = SimpleNamespace(
            get_track=AsyncMock(return_value=source),
            search=AsyncMock(return_value=search_results(playlists=playlist_refs)),
            get_playlist=get_playlist,
        )

        with patch("spotmatch_core.AsyncSpotifyClient", return_value=AsyncClientContext(client)):
            _source, _matches, coverage = find_matches(
                TRACK_ID,
                SearchOptions(
                    search_repeats=1,
                    releases_per_artist=0,
                    global_playlists=21,
                    tracks_per_playlist=25,
                ),
            )

        self.assertEqual(maximum_active, 5)
        self.assertIn("21 playlists", coverage)


if __name__ == "__main__":
    unittest.main()
