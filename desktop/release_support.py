from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlparse


REPOSITORY = "TheX24/SpotMatch"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
STABLE_VERSION_RE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def parse_stable_version(value: str) -> tuple[int, int, int]:
    match = STABLE_VERSION_RE.fullmatch(value)
    if not match:
        raise ValueError(f"Invalid stable version: {value!r}")
    return tuple(int(part) for part in match.groups())


def is_newer_version(current: str, candidate: str) -> bool:
    return parse_stable_version(candidate) > parse_stable_version(current)


def validate_release_tag(tag: str, version: str) -> None:
    parsed_version = parse_stable_version(version)
    if not tag.startswith("v"):
        raise ValueError(f"Release tag must use vX.Y.Z, got {tag!r}")
    try:
        parsed_tag = parse_stable_version(tag[1:])
    except ValueError as error:
        raise ValueError(f"Release tag must use vX.Y.Z, got {tag!r}") from error
    if parsed_tag != parsed_version:
        raise ValueError(f"Release tag {tag!r} does not match VERSION {version!r}")


APP_VERSION = resource_path("VERSION").read_text(encoding="utf-8").strip()
parse_stable_version(APP_VERSION)


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    html_url: str


class UpdateCheckError(RuntimeError):
    pass


def fetch_latest_release(
    opener: Callable = urllib.request.urlopen,
    timeout: float = 5.0,
) -> ReleaseInfo:
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"SpotMatch/{APP_VERSION}",
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        tag = payload["tag_name"]
        html_url = payload["html_url"]
        if not isinstance(tag, str) or not tag.startswith("v"):
            raise ValueError("latest release tag is not stable")
        version = tag[1:]
        parse_stable_version(version)
        parsed_url = urlparse(html_url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "github.com":
            raise ValueError("latest release URL is not a secure GitHub URL")
        return ReleaseInfo(version=version, html_url=html_url)
    except Exception as error:
        if isinstance(error, UpdateCheckError):
            raise
        raise UpdateCheckError(str(error) or "Could not check for updates") from error


def classify_update(
    current: str,
    release: ReleaseInfo | None,
    error: Exception | None,
) -> Literal["newer", "current", "error"]:
    if error is not None or release is None:
        return "error"
    return "newer" if is_newer_version(current, release.version) else "current"


def open_uri(
    uri: str,
    platform_name: str | None = None,
    *,
    startfile: Callable[[str], object] | None = None,
    popen: Callable = subprocess.Popen,
    browser_open: Callable[[str], object] = webbrowser.open,
    fallback_url: str | None = None,
) -> Literal["native", "browser"]:
    platform_name = platform_name or sys.platform
    try:
        if platform_name == "win32":
            native_startfile = startfile or getattr(os, "startfile")
            native_startfile(uri)
        elif platform_name == "darwin":
            popen(["open", uri])
        else:
            popen(["xdg-open", uri])
        return "native"
    except (AttributeError, OSError, subprocess.SubprocessError):
        if not fallback_url:
            raise
        browser_open(fallback_url)
        return "browser"
