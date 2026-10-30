from __future__ import annotations

import importlib.util
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

HAS_WEB = importlib.util.find_spec("fastapi") is not None and importlib.util.find_spec("httpx") is not None

if HAS_WEB:
    from fastapi.testclient import TestClient

    from spotmatch_core import Match
    from web import server

TRACK = "A" * 22


def fake_source():
    return SimpleNamespace(
        id=TRACK,
        name="Song",
        artists=(SimpleNamespace(name="Artist"),),
        album=SimpleNamespace(name="Album", images=[]),
        images=[],
        duration_ms=180_000,
    )


def events(response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


@unittest.skipUnless(HAS_WEB, "web requirements not installed")
class WebServerTests(unittest.TestCase):
    def setUp(self):
        server.cache.clear()
        server.jobs.clear()
        server.recent_searches.clear()
        server.active_clients.clear()
        server.waiting = 0
        self.client = TestClient(server.app)

    def search_patch(self):
        match = Match("B" * 22, "Song", "Artist", "Other album", 180_000, 100, 0)

        async def fake_find(_track, _options, _progress, _proxy):
            return fake_source(), [match], "0 albums"

        return patch.object(server, "_find_matches_async", fake_find)

    def test_result_streams_then_comes_from_cache(self):
        with self.search_patch():
            first = events(self.client.get(f"/api/match?track={TRACK}"))
        self.assertEqual(first[-1]["type"], "result")
        self.assertEqual(first[-1]["matches"][0]["track_id"], "B" * 22)

        second = events(self.client.get(f"/api/match?track={TRACK}"))
        self.assertTrue(second[0]["cached"])
        self.assertEqual(len(server.recent_searches["testclient"]), 1)

    def test_bad_input_is_rejected(self):
        response = self.client.get("/api/match?track=hello")
        self.assertEqual(response.status_code, 400)

    def test_hourly_limit_refuses_new_searches(self):
        now = time.monotonic()
        server.recent_searches["testclient"].extend([now] * server.HOURLY_LIMIT)
        response = self.client.get(f"/api/match?track={TRACK}")
        self.assertEqual(response.status_code, 429)
        self.assertIn("this hour", response.json()["error"])

    def test_hourly_limit_forgets_old_searches(self):
        server.recent_searches["testclient"].extend([time.monotonic() - 4000] * server.HOURLY_LIMIT)
        self.assertEqual(server._searches_left("testclient"), server.HOURLY_LIMIT)
        self.assertNotIn("testclient", server.recent_searches)

    def test_full_queue_turns_new_searches_away(self):
        server.waiting = server.MAX_QUEUE
        response = self.client.get(f"/api/match?track={TRACK}")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Retry-After", response.headers)

    def test_too_many_open_streams_is_refused(self):
        server.active_clients["testclient"] = server.MAX_STREAMS
        response = self.client.get(f"/api/match?track={TRACK}")
        self.assertEqual(response.status_code, 429)

    def test_cache_drops_least_recently_used(self):
        with patch.object(server, "CACHE_ENTRIES", 2):
            server._remember("one", {"n": 1})
            server._remember("two", {"n": 2})
            server._cached("one")
            server._remember("three", {"n": 3})
        self.assertEqual(list(server.cache), ["one", "three"])

    def test_client_ip_header_is_used_only_when_configured(self):
        request = SimpleNamespace(headers={"x-real-ip": "203.0.113.9"}, client=SimpleNamespace(host="10.0.0.1"))
        self.assertEqual(server._client_key(request), "10.0.0.1")
        with patch.object(server, "CLIENT_IP_HEADER", "x-real-ip"):
            self.assertEqual(server._client_key(request), "203.0.113.9")


if __name__ == "__main__":
    unittest.main()
