from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from spotify_scraper import NotFoundError

from app import SearchOptions, find_matches


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


def search_results(*tracks):
    return SimpleNamespace(tracks=tracks, albums=(), playlists=())


class SearchResilienceTests(unittest.TestCase):
    def client_context(self, client):
        context = Mock()
        context.__enter__ = Mock(return_value=client)
        context.__exit__ = Mock(return_value=False)
        return context

    def test_failed_standard_query_does_not_abort_later_queries(self):
        source = track(TRACK_ID)
        candidate = track(CANDIDATE_ID)
        client = Mock()
        client.get_track.return_value = source
        client.search.side_effect = [
            NotFoundError("Spotify pathfinder returned a null entity union."),
            search_results(candidate),
            search_results(),
            search_results(),
        ]

        with patch("app.SpotifyClient", return_value=self.client_context(client)):
            _, matches, _ = find_matches(
                TRACK_ID,
                SearchOptions(search_repeats=1, releases_per_artist=0),
            )

        self.assertEqual([match.track_id for match in matches], [CANDIDATE_ID])
        self.assertEqual(client.search.call_count, 3)

    def test_failed_paginated_pass_does_not_abort_later_passes(self):
        source = track(TRACK_ID)
        candidate = track(CANDIDATE_ID)
        client = Mock()
        client.get_track.return_value = source
        client.search.return_value = search_results()

        with (
            patch("app.SpotifyClient", return_value=self.client_context(client)),
            patch(
                "app.paginated_track_search",
                side_effect=[
                    NotFoundError("Spotify pathfinder returned a null entity union."),
                    iter((candidate,)),
                ],
            ),
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


if __name__ == "__main__":
    unittest.main()
