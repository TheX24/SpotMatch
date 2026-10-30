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
        server.open_streams.clear()
        server.recent_global.clear()
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

    def test_too_many_open_streams_is_refused(self):
        now = time.monotonic()
        server.open_streams["testclient"].update({n: now for n in range(server.MAX_STREAMS)})
        response = self.client.get(f"/api/match?track={TRACK}")
        self.assertEqual(response.status_code, 429)

    def test_stream_reservation_that_never_starts_expires(self):
        stale = time.monotonic() - server.STREAM_RESERVATION_SECONDS - 1
        server.open_streams["testclient"].update({1: stale, 2: None})
        self.assertEqual(server._open_stream_count("testclient"), 1)

    def test_second_search_is_refused_while_first_still_runs(self):
        # The running search is recorded synchronously, so a parallel burst can't slip past.
        server.jobs["C" * 22] = server.Job("C" * 22, "testclient")
        response = self.client.get(f"/api/match?track={TRACK}")
        self.assertEqual(response.status_code, 429)
        self.assertIn("already have a search", response.json()["error"])

    def test_queue_cap_counts_jobs_not_yet_queued(self):
        for n in range(server.MAX_SEARCHES + server.MAX_QUEUE):
            server.jobs[f"{n:022d}"] = server.Job(f"{n:022d}", f"other-{n}")
        response = self.client.get(f"/api/match?track={TRACK}")
        self.assertEqual(response.status_code, 503)

    def test_global_rate_cap_turns_new_searches_away(self):
        server.recent_global.extend([time.monotonic()] * server.GLOBAL_PER_MINUTE)
        response = self.client.get(f"/api/match?track={TRACK}")
        self.assertEqual(response.status_code, 503)

    def test_sweep_forgets_visitors_idle_for_an_hour(self):
        server.recent_searches["old"].append(time.monotonic() - 4000)
        server.recent_searches["new"].append(time.monotonic())
        server._sweep_recent()
        self.assertEqual(list(server.recent_searches), ["new"])

    def test_ipv6_visitors_count_per_64(self):
        def key(address):
            return server._client_key(SimpleNamespace(headers={"cf-connecting-ip": address}, client=None))

        with patch.object(server, "CLIENT_IP_HEADER", "cf-connecting-ip"):
            self.assertEqual(key("2001:db8:1:2::1"), key("2001:db8:1:2:ffff::9"))
            self.assertNotEqual(key("2001:db8:1:2::1"), key("2001:db8:1:3::1"))
            self.assertEqual(key("203.0.113.9"), "203.0.113.9")
            self.assertEqual(key("::ffff:203.0.113.9"), "203.0.113.9")

    def test_unexpected_errors_do_not_leak_details(self):
        message = server._describe_error(RuntimeError("socks5://warp:40001 refused"))
        self.assertNotIn("warp", message)

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
