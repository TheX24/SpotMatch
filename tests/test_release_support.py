from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from release_support import (
    ReleaseInfo,
    UpdateCheckError,
    classify_update,
    fetch_latest_release,
    is_newer_version,
    open_uri,
    parse_stable_version,
    validate_release_tag,
)


class FakeResponse:
    def __init__(self, payload: object):
        if isinstance(payload, bytes):
            self.payload = payload
        else:
            self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.payload


class ReleaseSupportTests(unittest.TestCase):
    def test_stable_version_parsing_and_comparison(self):
        self.assertEqual(parse_stable_version("1.2.0"), (1, 2, 0))
        self.assertTrue(is_newer_version("1.9.9", "1.10.0"))
        self.assertFalse(is_newer_version("2.0.0", "1.99.99"))

    def test_rejects_non_stable_versions(self):
        for value in ("v1.2.0", "1.2", "1.2.0-beta", "01.2.0", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_stable_version(value)

    def test_release_tag_must_match_version(self):
        validate_release_tag("v1.2.1", "1.2.1")
        for tag in ("1.2.1", "v1.2", "v1.2.0", "v1.2.1-beta"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                validate_release_tag(tag, "1.2.1")

    def test_fetches_latest_stable_release(self):
        payload = {
            "tag_name": "v1.3.0",
            "html_url": "https://github.com/TheX24/SpotMatch/releases/tag/v1.3.0",
        }
        opener = Mock(return_value=FakeResponse(payload))

        release = fetch_latest_release(opener=opener)

        self.assertEqual(
            release,
            ReleaseInfo(
                version="1.3.0",
                html_url="https://github.com/TheX24/SpotMatch/releases/tag/v1.3.0",
            ),
        )
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/TheX24/SpotMatch/releases/latest")
        self.assertEqual(opener.call_args.kwargs["timeout"], 5.0)

    def test_rejects_invalid_release_responses(self):
        bad_payloads = (
            b"not-json",
            {},
            {"tag_name": "v1.3.0-beta", "html_url": "https://github.com/TheX24/SpotMatch/releases/tag/v1.3.0-beta"},
            {"tag_name": "v1.3.0", "html_url": "http://github.com/TheX24/SpotMatch/releases/tag/v1.3.0"},
            {"tag_name": "v1.3.0", "html_url": "https://example.com/release"},
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload), self.assertRaises(UpdateCheckError):
                fetch_latest_release(opener=Mock(return_value=FakeResponse(payload)))

    def test_classifies_update_outcomes(self):
        newer = ReleaseInfo("1.3.0", "https://github.com/TheX24/SpotMatch/releases/tag/v1.3.0")
        current = ReleaseInfo("1.2.0", "https://github.com/TheX24/SpotMatch/releases/tag/v1.2.0")
        older = ReleaseInfo("1.1.9", "https://github.com/TheX24/SpotMatch/releases/tag/v1.1.9")
        self.assertEqual(classify_update("1.2.0", newer, None), "newer")
        self.assertEqual(classify_update("1.2.0", current, None), "current")
        self.assertEqual(classify_update("1.2.0", older, None), "current")
        self.assertEqual(classify_update("1.2.0", None, RuntimeError("offline")), "error")

    def test_uses_native_uri_launchers(self):
        startfile = Mock()
        popen = Mock()
        browser = Mock()
        self.assertEqual(open_uri("spotify:track:id", "win32", startfile=startfile, popen=popen, browser_open=browser), "native")
        startfile.assert_called_once_with("spotify:track:id")

        self.assertEqual(open_uri("spotify:track:id", "darwin", popen=popen, browser_open=browser), "native")
        popen.assert_called_with(["open", "spotify:track:id"])

        self.assertEqual(open_uri("spotify:track:id", "linux", popen=popen, browser_open=browser), "native")
        popen.assert_called_with(["xdg-open", "spotify:track:id"])
        browser.assert_not_called()

    def test_falls_back_to_browser_when_native_launch_fails(self):
        browser = Mock(return_value=True)
        result = open_uri(
            "spotify:track:id",
            "linux",
            popen=Mock(side_effect=OSError("missing")),
            browser_open=browser,
            fallback_url="https://open.spotify.com/track/id",
        )
        self.assertEqual(result, "browser")
        browser.assert_called_once_with("https://open.spotify.com/track/id")

    def test_linux_packaging_files_have_required_entries(self):
        root = Path(__file__).resolve().parents[1]
        desktop = (root / "packaging/linux/SpotMatch.desktop").read_text(encoding="utf-8")
        app_run = (root / "packaging/linux/AppRun").read_text(encoding="utf-8")
        self.assertIn("Exec=SpotMatch", desktop)
        self.assertIn("Icon=spotmatch", desktop)
        self.assertIn("Type=Application", desktop)
        self.assertIn('exec "$APPDIR/usr/bin/SpotMatch" "$@"', app_run)


if __name__ == "__main__":
    unittest.main()
