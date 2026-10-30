"""SpotMatch web service: one fixed deep search per track, streamed over SSE.

Run from the repository root:
    python -m uvicorn web.server:app --port 8765

Environment:
    SPOTMATCH_PROXY            outbound proxy for Spotify requests (e.g. socks5://127.0.0.1:40000 for WARP)
    SPOTMATCH_MAX_SEARCHES     concurrent Spotify searches before new ones queue (default 6)
    SPOTMATCH_CACHE_SECONDS    how long finished results are reused (default 86400)
    SPOTMATCH_ALLOWED_ORIGINS  comma-separated sites allowed to call the API from a browser (default *, any site)
    SPOTMATCH_CLIENT_IP_HEADER header your reverse proxy sets to the visitor's address, e.g. X-Real-IP
                               (default unset: use the connecting address). Only set it when every
                               request comes through that proxy, or visitors can fake their address.
    SPOTMATCH_MAX_QUEUE        searches allowed to wait for a slot before new ones are turned away (default 30)
    SPOTMATCH_HOURLY_LIMIT     new searches per visitor per hour; cached results are free (default 20).
                               IPv6 visitors count per /64, since one connection usually owns a whole /64.
    SPOTMATCH_GLOBAL_PER_MINUTE  new searches per minute across all visitors (default 30)
    SPOTMATCH_MAX_STREAMS      open result streams per visitor (default 3)
    SPOTMATCH_SEARCH_TIMEOUT   seconds before a search is abandoned (default 180)
    SPOTMATCH_CACHE_ENTRIES    finished results kept in memory (default 300)
    SPOTMATCH_RELEASES_PER_ARTIST  also scan this many releases per credited artist (default 0, off).
                               Catches album copies Spotify hides from search; costs ~1 request per release.
"""

from __future__ import annotations

import asyncio
import ipaddress
import itertools
import json
import logging
import os
import sys
import time
from collections import OrderedDict, defaultdict, deque
from collections.abc import AsyncIterator
from dataclasses import asdict, replace
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from spotmatch_core import WEB_SEARCH_OPTIONS, _find_matches_async, extract_track_id  # noqa: E402

PROXY = os.environ.get("SPOTMATCH_PROXY") or None
MAX_SEARCHES = int(os.environ.get("SPOTMATCH_MAX_SEARCHES", "6"))
CACHE_SECONDS = int(os.environ.get("SPOTMATCH_CACHE_SECONDS", "86400"))
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("SPOTMATCH_ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]
CLIENT_IP_HEADER = (os.environ.get("SPOTMATCH_CLIENT_IP_HEADER") or "").strip().lower()
MAX_QUEUE = int(os.environ.get("SPOTMATCH_MAX_QUEUE", "30"))
HOURLY_LIMIT = int(os.environ.get("SPOTMATCH_HOURLY_LIMIT", "20"))
GLOBAL_PER_MINUTE = int(os.environ.get("SPOTMATCH_GLOBAL_PER_MINUTE", "30"))
MAX_STREAMS = int(os.environ.get("SPOTMATCH_MAX_STREAMS", "3"))
SEARCH_TIMEOUT = int(os.environ.get("SPOTMATCH_SEARCH_TIMEOUT", "180"))
CACHE_ENTRIES = int(os.environ.get("SPOTMATCH_CACHE_ENTRIES", "300"))
SEARCH_OPTIONS = replace(
    WEB_SEARCH_OPTIONS,
    releases_per_artist=int(os.environ.get("SPOTMATCH_RELEASES_PER_ARTIST", "0")),
)
# A reserved stream that never starts (client left before the body) stops counting after this.
STREAM_RESERVATION_SECONDS = 30
# Visitors' search history is swept for stale entries once it tracks this many visitors.
RECENT_SWEEP_SIZE = 5000

log = logging.getLogger("spotmatch")


class Job:
    """One running search; every client asking for the same track shares it."""

    def __init__(self, track_id: str, owner: str) -> None:
        self.track_id = track_id
        self.owner = owner  # the visitor who started it; joiners don't own it
        self.history: list[dict] = []
        self.subscribers: list[asyncio.Queue[dict]] = []
        self.done = False

    def publish(self, event: dict) -> None:
        # Progress fires often; keep only the latest one in history for late joiners.
        if event["type"] == "progress" and self.history and self.history[-1]["type"] == "progress":
            self.history[-1] = event
        else:
            self.history.append(event)
        for queue in self.subscribers:
            queue.put_nowait(event)

    async def events(self) -> AsyncIterator[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        for event in self.history:
            queue.put_nowait(event)
        if self.done:
            while not queue.empty():
                yield queue.get_nowait()
            return
        self.subscribers.append(queue)
        try:
            while True:
                event = await queue.get()
                yield event
                if event["type"] in ("result", "error"):
                    return
        finally:
            self.subscribers.remove(queue)


search_slots = asyncio.Semaphore(MAX_SEARCHES)
waiting = 0
jobs: dict[str, Job] = {}
cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
# visitor -> {stream token: reserved-at time, or None once the stream is running}
open_streams: defaultdict[str, dict[int, float | None]] = defaultdict(dict)
stream_tokens = itertools.count()
recent_searches: defaultdict[str, deque[float]] = defaultdict(deque)
recent_global: deque[float] = deque()


def _cached(track_id: str) -> dict | None:
    entry = cache.get(track_id)
    if entry is None:
        return None
    if entry[0] <= time.monotonic():
        del cache[track_id]
        return None
    cache.move_to_end(track_id)
    return entry[1]


def _remember(track_id: str, payload: dict) -> None:
    cache[track_id] = (time.monotonic() + CACHE_SECONDS, payload)
    cache.move_to_end(track_id)
    while len(cache) > CACHE_ENTRIES:
        cache.popitem(last=False)


def _searches_left(client: str) -> int:
    """Searches this visitor may still start this hour (prunes old entries)."""
    stamps = recent_searches.get(client)
    if not stamps:
        return HOURLY_LIMIT
    cutoff = time.monotonic() - 3600
    while stamps and stamps[0] < cutoff:
        stamps.popleft()
    if not stamps:
        del recent_searches[client]
        return HOURLY_LIMIT
    return HOURLY_LIMIT - len(stamps)


def _sweep_recent() -> None:
    """Forget visitors with no search in the last hour, so the table can't grow without bound."""
    cutoff = time.monotonic() - 3600
    for client in [client for client, stamps in recent_searches.items() if not stamps or stamps[-1] < cutoff]:
        del recent_searches[client]


def _global_searches_left() -> int:
    cutoff = time.monotonic() - 60
    while recent_global and recent_global[0] < cutoff:
        recent_global.popleft()
    return GLOBAL_PER_MINUTE - len(recent_global)


def _open_stream_count(client: str) -> int:
    streams = open_streams.get(client)
    if not streams:
        return 0
    cutoff = time.monotonic() - STREAM_RESERVATION_SECONDS
    for token in [token for token, reserved in streams.items() if reserved is not None and reserved < cutoff]:
        del streams[token]
    if not streams:
        del open_streams[client]
        return 0
    return len(streams)


def _release_stream(client: str, token: int) -> None:
    streams = open_streams.get(client)
    if streams is not None:
        streams.pop(token, None)
        if not streams:
            del open_streams[client]


def _image_url(track) -> str | None:
    images = list(getattr(track, "images", None) or [])
    if not images and track.album:
        images = list(getattr(track.album, "images", None) or [])
    if not images:
        return None
    # Smallest image that is still at least 300px wide, else the largest.
    images.sort(key=lambda image: image.width or 0)
    return next((image.url for image in images if (image.width or 0) >= 300), images[-1].url)


def _result_payload(source, matches, coverage: str) -> dict:
    return {
        "type": "result",
        "source": {
            "track_id": source.id,
            "title": source.name,
            "artists": ", ".join(artist.name for artist in source.artists),
            "album": source.album.name if source.album else "",
            "duration_ms": source.duration_ms or 0,
            "image": _image_url(source),
        },
        "matches": [asdict(match) for match in matches],
        "coverage": coverage,
        "searched_at": int(time.time()),
    }


async def _run(job: Job) -> None:
    global waiting
    queued = False
    try:
        if search_slots.locked():
            queued = True
            waiting += 1
            job.publish({"type": "queued", "position": waiting})
            try:
                await search_slots.acquire()
            finally:
                waiting -= 1
        else:
            await search_slots.acquire()
        try:
            if queued and not job.subscribers:
                # Everyone who asked left while it was queued; don't spend Spotify requests on it.
                job.done = True
                return

            def progress(message: str, current: int, total: int) -> None:
                job.publish({"type": "progress", "message": message, "current": current, "total": total})

            source, matches, coverage = await asyncio.wait_for(
                _find_matches_async(job.track_id, SEARCH_OPTIONS, progress, PROXY),
                SEARCH_TIMEOUT,
            )
        finally:
            search_slots.release()
        payload = _result_payload(source, matches, coverage)
        _remember(job.track_id, payload)
        job.done = True
        job.publish(payload)
    except TimeoutError:
        job.done = True
        job.publish({"type": "error", "message": "The search took too long and was stopped. Try again later."})
    except Exception as error:  # noqa: BLE001 - surfaced to the client as an error event
        job.done = True
        job.publish({"type": "error", "message": _describe_error(error)})
    finally:
        jobs.pop(job.track_id, None)


def _describe_error(error: Exception) -> str:
    text = str(error) or type(error).__name__
    if "404" in text or "not found" in text.casefold():
        return "Spotify has no track with that ID. Check the link and try again."
    if "429" in text:
        return "Spotify is rate-limiting searches right now. Try again in a few minutes."
    # The detail can name internal hosts (like the proxy), so it stays in the server log.
    log.warning("search failed: %r", error)
    return "The search failed. Try again in a few minutes."


def _client_key(request: Request) -> str:
    address = ""
    if CLIENT_IP_HEADER:
        address = request.headers.get(CLIENT_IP_HEADER, "").split(",")[0].strip()
    if not address:
        address = request.client.host if request.client else "unknown"
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.version == 4:
        return str(ip)
    if ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    # One IPv6 connection usually owns a whole /64; count it as one visitor.
    return str(ipaddress.ip_network(f"{ip}/64", strict=False))


def _refuse(message: str, status: int, retry_after: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers={"Retry-After": str(retry_after)})


def _sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


app = FastAPI(title="SpotMatch", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["GET"], allow_headers=[])


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "searching": len(jobs) - waiting, "queued": waiting, "queue_limit": MAX_QUEUE, "cached": len(cache)}


@app.get("/api/match")
async def match(request: Request, track: str = Query(..., max_length=200)):
    try:
        track_id = extract_track_id(track)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)

    cached = _cached(track_id)
    if cached:
        async def replay() -> AsyncIterator[str]:
            yield _sse({**cached, "cached": True})

        return StreamingResponse(replay(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    # Every check below and the bookkeeping after it run without awaiting, so
    # parallel requests can't all slip past a check before any is counted.
    client = _client_key(request)
    if _open_stream_count(client) >= MAX_STREAMS:
        return _refuse("Too many searches open at once. Close one and try again.", 429, 10)
    job = jobs.get(track_id)
    if job is None:
        if any(running.owner == client for running in jobs.values()):
            return _refuse("You already have a search running. Wait for it to finish before starting another.", 429, 10)
        if _searches_left(client) <= 0:
            return _refuse(f"You've started {HOURLY_LIMIT} searches this hour. Try again later; saved results still load.", 429, 600)
        if len(jobs) >= MAX_SEARCHES + MAX_QUEUE or _global_searches_left() <= 0:
            return _refuse("SpotMatch is busy right now. Try again in a few minutes.", 503, 120)
        now = time.monotonic()
        recent_searches[client].append(now)
        recent_global.append(now)
        if len(recent_searches) > RECENT_SWEEP_SIZE:
            _sweep_recent()
        job = jobs[track_id] = Job(track_id, client)
        asyncio.get_running_loop().create_task(_run(job))

    token = next(stream_tokens)
    open_streams[client][token] = time.monotonic()

    async def stream() -> AsyncIterator[str]:
        streams = open_streams.get(client)
        if streams is not None and token in streams:
            streams[token] = None  # running: counts until it ends, no expiry
        try:
            async for event in job.events():
                yield _sse(event)
        finally:
            _release_stream(client, token)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


app.mount("/", StaticFiles(directory=Path(__file__).with_name("static"), html=True), name="static")
