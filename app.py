from __future__ import annotations

import json
import os
import re
import threading
import unicodedata
import webbrowser
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from spotify_scraper import SpotifyClient
from spotify_scraper.api import parse_entities
import sv_ttk


TRACK_ID_RE = re.compile(r"(?:spotify:track:|open\.spotify\.com/track/)?([A-Za-z0-9]{22})")

PRESETS = {
    "Quick": dict(search_pages=1, search_repeats=1, releases=0, albums=0, playlists=0, playlist_tracks=0, title_floor=58, variants=False),
    "Balanced": dict(search_pages=1, search_repeats=2, releases=50, albums=0, playlists=0, playlist_tracks=0, title_floor=58, variants=False),
    "Deep": dict(search_pages=1, search_repeats=3, releases=200, albums=0, playlists=0, playlist_tracks=0, title_floor=58, variants=False),
    "Exhaustive": dict(search_pages=20, search_repeats=3, releases=200, albums=150, playlists=60, playlist_tracks=500, title_floor=58, variants=True),
}

SETTINGS_PATH = Path(os.environ.get("APPDATA", Path.home())) / "SpotMatch" / "settings.json"


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


def paginated_track_search(
    client: SpotifyClient,
    query: str,
    maximum: int = 1000,
    progress: ProgressCallback | None = None,
):
    """Page Spotify's private web search beyond SpotifyScraper's first page."""
    offset = 0
    page_size = 50
    seen: set[str] = set()
    token = client._tokens.token()
    while offset < maximum:
        if progress:
            progress("Paginating Spotify results", offset // page_size + 1, max(1, (maximum + page_size - 1) // page_size))
        data = client._search_request(
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
            yield track
        offset += page_size
        if offset >= min(results.total, maximum):
            break


def find_matches(
    raw_id: str,
    options: SearchOptions | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[object, list[Match], str]:
    options = options or SearchOptions()
    track_id = extract_track_id(raw_id)
    with SpotifyClient() as client:
        if progress:
            progress("Reading source track", 0, 1)
        source = client.get_track(track_id)
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
        search_step = 0
        for pass_index in range(options.search_repeats):
            for query_index, query in enumerate(queries):
                search_step += 1
                if progress:
                    progress(f"Search pass {pass_index + 1}/{options.search_repeats} · query {query_index + 1}/{len(queries)}", search_step, search_total)
                extra_types = (("album",) if options.global_albums else ()) + (("playlist",) if options.global_playlists else ())
                entity_types = ("track",) + extra_types
                results = client.search(query, types=entity_types, limit=50)
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
            paginated_query = f"{source.name} {primary_artist}".strip()
            pagination_total = options.search_repeats * options.search_pages
            for pass_index in range(options.search_repeats):
                def page_progress(_message: str, current: int, _total: int, pass_index=pass_index) -> None:
                    if progress:
                        progress(
                            f"Paginated pass {pass_index + 1}/{options.search_repeats} · page {current}/{options.search_pages}",
                            pass_index * options.search_pages + current,
                            pagination_total,
                        )

                for candidate in paginated_track_search(
                    client,
                    paginated_query,
                    options.search_pages * 50,
                    page_progress,
                ):
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
        for artist_index, artist in enumerate(release_artists, 1):
            if progress:
                progress("Reading credited-artist discographies", artist_index, len(release_artists))
            if not artist.id:
                continue
            for release in client.get_discography(artist.id, max_releases=release_limit):
                if release.id not in seen_releases:
                    seen_releases.add(release.id)
                    release_ids.append(release.id)

        all_album_ids = list(dict.fromkeys(release_ids + discovered_album_ids[: options.global_albums]))
        albums_scanned = 0
        album_batches = [all_album_ids[index:index + 20] for index in range(0, len(all_album_ids), 20)]
        for batch_index, album_batch in enumerate(album_batches, 1):
            if progress:
                progress("Scanning album batches", batch_index, len(album_batches))
            for album_result in client.get_albums(album_batch):
                if not album_result.ok:
                    continue
                albums_scanned += 1
                album = album_result.result
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
            playlist_batches = [playlist_ids[index:index + 10] for index in range(0, len(playlist_ids), 10)]
            for batch_index, playlist_batch in enumerate(playlist_batches, 1):
                if progress:
                    progress("Scanning playlist batches", batch_index, len(playlist_batches))
                for playlist_result in client.get_playlists(playlist_batch, max_tracks=options.tracks_per_playlist):
                    if not playlist_result.ok:
                        continue
                    playlists_scanned += 1
                    for playlist_item in playlist_result.result.tracks:
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


class SpotMatchApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("SpotMatch")
        self.geometry("1120x680")
        self.minsize(860, 520)
        self.matches: list[Match] = []
        self.source_track_id: str | None = None
        self.custom_presets: dict[str, dict[str, object]] = {}
        self._build_ui()
        self._load_settings()
        self.protocol("WM_DELETE_WINDOW", self._close)
        sv_ttk.set_theme("dark")

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        style.configure("Treeview", rowheight=32)
        style.configure("Title.TLabel", font=("Segoe UI", 24, "bold"))
        style.configure("Subtitle.TLabel", font=("Segoe UI", 10))

        header = ttk.Frame(self, padding=(24, 20, 24, 12))
        header.pack(fill="x")
        ttk.Label(header, text="SpotMatch", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="Find alternate Spotify IDs for the same track", style="Subtitle.TLabel").pack(anchor="w", pady=(2, 0))

        input_row = ttk.Frame(self, padding=(24, 0))
        input_row.pack(fill="x")
        self.input_var = tk.StringVar()
        entry = ttk.Entry(input_row, textvariable=self.input_var, font=("Segoe UI", 11))
        entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(0, 10))
        entry.bind("<Return>", lambda _event: self.search())
        self.search_button = ttk.Button(input_row, text="Find matches", command=self.search, style="Accent.TButton")
        self.search_button.pack(side="right")

        options_row = ttk.Frame(self, padding=(24, 12))
        self.options_row = options_row
        options_row.pack(fill="x")
        ttk.Label(options_row, text="Preset").pack(side="left")
        self.depth_var = tk.StringVar(value="Balanced")
        self.preset_box = ttk.Combobox(options_row, textvariable=self.depth_var, values=tuple(PRESETS), state="readonly", width=14)
        self.preset_box.pack(side="left", padx=(6, 4))
        ttk.Button(options_row, text="Save", command=self.save_custom_preset, width=5).pack(side="left", padx=(0, 4))
        ttk.Button(options_row, text="Delete", command=self.delete_custom_preset, width=6).pack(side="left", padx=(0, 18))
        ttk.Label(options_row, text="Minimum match").pack(side="left")
        self.score_var = tk.StringVar(value="60%")
        ttk.Combobox(options_row, textvariable=self.score_var, values=("50%", "60%", "70%", "75%", "80%", "90%", "95%"), state="readonly", width=6).pack(side="left", padx=(6, 18))
        ttk.Label(options_row, text="Max duration difference").pack(side="left")
        self.duration_var = tk.StringVar(value="10 sec")
        ttk.Combobox(options_row, textvariable=self.duration_var, values=("1 sec", "2 sec", "5 sec", "10 sec", "30 sec", "120 sec"), state="readonly", width=9).pack(side="left", padx=(6, 18))
        self.exact_title_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options_row, text="Exact title only", variable=self.exact_title_var).pack(side="left")
        self.advanced_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options_row, text="Show advanced options", variable=self.advanced_var, command=self.toggle_advanced).pack(side="right")

        limits = ttk.LabelFrame(self, text="Search limits (editable)", padding=(12, 8))
        self.limits_frame = limits
        self.pages_var = tk.IntVar(value=1)
        self.repeats_var = tk.IntVar(value=2)
        self.releases_var = tk.IntVar(value=50)
        self.albums_var = tk.IntVar(value=0)
        self.playlists_var = tk.IntVar(value=0)
        self.playlist_tracks_var = tk.IntVar(value=0)
        self.title_floor_var = tk.IntVar(value=58)
        self.variants_var = tk.BooleanVar(value=False)
        limit_fields = (
            ("Pages ×50", self.pages_var, 1, 20, 5),
            ("Search passes", self.repeats_var, 1, 20, 5),
            ("Artist releases", self.releases_var, 0, 1000, 7),
            ("Global albums", self.albums_var, 0, 1000, 7),
            ("Playlists", self.playlists_var, 0, 500, 6),
            ("Tracks/playlist", self.playlist_tracks_var, 0, 5000, 7),
            ("Title floor %", self.title_floor_var, 0, 100, 5),
        )
        for column, (label, variable, minimum, maximum, width) in enumerate(limit_fields):
            ttk.Label(limits, text=label).grid(row=0, column=column, padx=(0, 6), sticky="w")
            ttk.Spinbox(limits, textvariable=variable, from_=minimum, to=maximum, width=width).grid(row=1, column=column, padx=(0, 14), sticky="w")
        ttk.Checkbutton(
            limits,
            text="Include variant queries (cover, remix, live, instrumental, karaoke, sped up, slowed)",
            variable=self.variants_var,
        ).grid(row=2, column=0, columnspan=len(limit_fields), pady=(10, 0), sticky="w")
        self.preset_box.bind("<<ComboboxSelected>>", self.apply_preset)

        self.status_var = tk.StringVar(value="Paste a Spotify track ID or URL, then press Enter.")
        ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(24, 4, 24, 12)).pack(fill="x")
        self.progress_bar = ttk.Progressbar(self, mode="determinate", maximum=1)

        table_frame = ttk.Frame(self, padding=(24, 0))
        table_frame.pack(fill="both", expand=True)
        columns = ("score", "title", "artist", "duration", "difference", "album", "id")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        headings = {"score": "Match", "title": "Title", "artist": "Artist", "duration": "Length", "difference": "Difference", "album": "Album", "id": "Spotify ID"}
        widths = {"score": 65, "title": 180, "artist": 150, "duration": 65, "difference": 85, "album": 180, "id": 185}
        for column in columns:
            self.table.heading(column, text=headings[column])
            self.table.column(column, width=widths[column], minwidth=55, stretch=column in {"title", "artist", "album"})
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        scrollbar.pack(side="right", fill="y")
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.bind("<Double-1>", lambda _event: self.open_selected())

        actions = ttk.Frame(self, padding=(24, 16))
        actions.pack(fill="x")
        ttk.Button(actions, text="Copy selected IDs", command=self.copy_selected).pack(side="left")
        ttk.Button(actions, text="Copy selected + source", command=self.copy_selected_with_source).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Open in Spotify", command=self.open_selected).pack(side="left", padx=8)
        ttk.Button(actions, text="Select all", command=self.select_all).pack(side="left")
        ttk.Button(actions, text="Export selected to TXT", command=self.export_selected, style="Accent.TButton").pack(side="left", padx=8)
        ttk.Label(actions, text="Ctrl/Shift-click to select multiple · double-click to open").pack(side="right")
        entry.focus_set()

    def _settings(self) -> dict[str, object]:
        return {
            "input": self.input_var.get(),
            "preset": self.depth_var.get(),
            "minimum_score": self.score_var.get(),
            "maximum_duration": self.duration_var.get(),
            "exact_title": self.exact_title_var.get(),
            "show_advanced": self.advanced_var.get(),
            "search_pages": self.pages_var.get(),
            "search_repeats": self.repeats_var.get(),
            "artist_releases": self.releases_var.get(),
            "global_albums": self.albums_var.get(),
            "global_playlists": self.playlists_var.get(),
            "tracks_per_playlist": self.playlist_tracks_var.get(),
            "title_floor": self.title_floor_var.get(),
            "variant_queries": self.variants_var.get(),
            "custom_presets": self.custom_presets,
        }

    def _save_settings(self) -> bool:
        temporary_path = SETTINGS_PATH.with_suffix(".tmp")
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(json.dumps(self._settings(), indent=2), encoding="utf-8")
            os.replace(temporary_path, SETTINGS_PATH)
        except (OSError, tk.TclError) as error:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            messagebox.showerror(
                "Could not save settings",
                f"SpotMatch could not save your settings to:\n{SETTINGS_PATH}\n\n{error}",
                parent=self,
            )
            return False
        return True

    def _load_settings(self) -> None:
        try:
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as error:
            messagebox.showwarning(
                "Could not load settings",
                f"SpotMatch could not load your settings from:\n{SETTINGS_PATH}\n\n{error}",
                parent=self,
            )
            return
        if not isinstance(settings, dict):
            messagebox.showwarning(
                "Could not load settings",
                f"SpotMatch ignored invalid settings in:\n{SETTINGS_PATH}",
                parent=self,
            )
            return

        custom_presets = settings.get("custom_presets")
        rejected_presets: list[str] = []
        if isinstance(custom_presets, dict):
            for name, preset in custom_presets.items():
                if self._valid_custom_preset(name, preset):
                    self.custom_presets[name] = preset
                else:
                    rejected_presets.append(str(name))
        self._refresh_preset_box()

        if rejected_presets:
            names = "\n".join(f"• {name}" for name in rejected_presets)
            messagebox.showwarning(
                "Invalid custom presets",
                f"SpotMatch ignored these invalid custom presets:\n\n{names}",
                parent=self,
            )

        string_values = (
            ("input", self.input_var, None),
            ("preset", self.depth_var, tuple(PRESETS) + tuple(self.custom_presets)),
            ("minimum_score", self.score_var, ("50%", "60%", "70%", "75%", "80%", "90%", "95%")),
            ("maximum_duration", self.duration_var, ("1 sec", "2 sec", "5 sec", "10 sec", "30 sec", "120 sec")),
        )
        for key, variable, allowed in string_values:
            value = settings.get(key)
            if isinstance(value, str) and (allowed is None or value in allowed):
                variable.set(value)

        integer_values = (
            ("search_pages", self.pages_var, 1, 20),
            ("search_repeats", self.repeats_var, 1, 20),
            ("artist_releases", self.releases_var, 0, 1000),
            ("global_albums", self.albums_var, 0, 1000),
            ("global_playlists", self.playlists_var, 0, 500),
            ("tracks_per_playlist", self.playlist_tracks_var, 0, 5000),
            ("title_floor", self.title_floor_var, 0, 100),
        )
        for key, variable, minimum, maximum in integer_values:
            value = settings.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum:
                variable.set(value)

        boolean_values = (
            ("exact_title", self.exact_title_var),
            ("show_advanced", self.advanced_var),
            ("variant_queries", self.variants_var),
        )
        for key, variable in boolean_values:
            value = settings.get(key)
            if isinstance(value, bool):
                variable.set(value)
        self.toggle_advanced()

    def _close(self) -> None:
        if self._save_settings() or messagebox.askyesno(
            "Close without saving?",
            "SpotMatch could not save your settings. Close the app anyway?",
            parent=self,
        ):
            self.destroy()

    def toggle_advanced(self) -> None:
        if self.advanced_var.get():
            self.limits_frame.pack(fill="x", padx=24, pady=(0, 10), after=self.options_row)
        else:
            self.limits_frame.pack_forget()

    def _refresh_preset_box(self) -> None:
        self.preset_box.configure(values=tuple(PRESETS) + tuple(self.custom_presets))

    def _preset_values(self) -> dict[str, object]:
        return {
            "minimum_score": self.score_var.get(),
            "maximum_duration": self.duration_var.get(),
            "exact_title": self.exact_title_var.get(),
            "search_pages": self.pages_var.get(),
            "search_repeats": self.repeats_var.get(),
            "releases": self.releases_var.get(),
            "albums": self.albums_var.get(),
            "playlists": self.playlists_var.get(),
            "playlist_tracks": self.playlist_tracks_var.get(),
            "title_floor": self.title_floor_var.get(),
            "variants": self.variants_var.get(),
        }

    @staticmethod
    def _valid_custom_preset(name: object, preset: object) -> bool:
        if not isinstance(name, str) or not name.strip() or name.casefold() in {item.casefold() for item in PRESETS}:
            return False
        if not isinstance(preset, dict):
            return False
        if preset.get("minimum_score") not in ("50%", "60%", "70%", "75%", "80%", "90%", "95%"):
            return False
        if preset.get("maximum_duration") not in ("1 sec", "2 sec", "5 sec", "10 sec", "30 sec", "120 sec"):
            return False
        if not isinstance(preset.get("exact_title"), bool) or not isinstance(preset.get("variants"), bool):
            return False
        integer_values = (
            ("search_pages", 1, 20),
            ("search_repeats", 1, 20),
            ("releases", 0, 1000),
            ("albums", 0, 1000),
            ("playlists", 0, 500),
            ("playlist_tracks", 0, 5000),
            ("title_floor", 0, 100),
        )
        return all(
            isinstance(preset.get(key), int)
            and not isinstance(preset[key], bool)
            and minimum <= preset[key] <= maximum
            for key, minimum, maximum in integer_values
        )

    def save_custom_preset(self) -> None:
        name = simpledialog.askstring("Save custom preset", "Preset name:", parent=self)
        if name is None:
            return
        name = name.strip()
        if not name:
            messagebox.showerror("Invalid preset name", "Enter a preset name.", parent=self)
            return
        if name.casefold() in {item.casefold() for item in PRESETS}:
            messagebox.showerror("Built-in preset", "Choose a different name; built-in presets cannot be replaced.", parent=self)
            return
        existing = next((item for item in self.custom_presets if item.casefold() == name.casefold()), None)
        if existing and not messagebox.askyesno("Replace preset?", f'Replace the custom preset "{existing}"?', parent=self):
            return
        try:
            preset = self._preset_values()
        except tk.TclError:
            messagebox.showerror("Invalid search limit", "Enter valid numbers for every search limit.", parent=self)
            return
        if not self._valid_custom_preset(name, preset):
            messagebox.showerror(
                "Invalid custom preset",
                "Enter values within the limits shown for every search option.",
                parent=self,
            )
            return
        previous_preset = self.custom_presets.get(existing) if existing else None
        previous_selection = self.depth_var.get()
        if existing and existing != name:
            del self.custom_presets[existing]
        self.custom_presets[name] = preset
        self.depth_var.set(name)
        self._refresh_preset_box()
        if self._save_settings():
            self.status_var.set(f'Saved custom preset "{name}"')
            return

        del self.custom_presets[name]
        if existing and previous_preset is not None:
            self.custom_presets[existing] = previous_preset
        self.depth_var.set(previous_selection)
        self._refresh_preset_box()

    def delete_custom_preset(self) -> None:
        name = self.depth_var.get()
        if name not in self.custom_presets:
            messagebox.showinfo("Built-in preset", "Built-in presets cannot be deleted.", parent=self)
            return
        if not messagebox.askyesno("Delete preset?", f'Delete the custom preset "{name}"?', parent=self):
            return
        deleted_preset = self.custom_presets.pop(name)
        self.depth_var.set("Balanced")
        self._refresh_preset_box()
        self.apply_preset()
        if self._save_settings():
            self.status_var.set(f'Deleted custom preset "{name}"')
            return

        self.custom_presets[name] = deleted_preset
        self.depth_var.set(name)
        self._refresh_preset_box()
        self.apply_preset()

    def apply_preset(self, _event=None) -> None:
        preset = PRESETS.get(self.depth_var.get()) or self.custom_presets[self.depth_var.get()]
        if "minimum_score" in preset:
            self.score_var.set(preset["minimum_score"])
            self.duration_var.set(preset["maximum_duration"])
            self.exact_title_var.set(preset["exact_title"])
        self.pages_var.set(preset["search_pages"])
        self.repeats_var.set(preset["search_repeats"])
        self.releases_var.set(preset["releases"])
        self.albums_var.set(preset["albums"])
        self.playlists_var.set(preset["playlists"])
        self.playlist_tracks_var.set(preset["playlist_tracks"])
        self.title_floor_var.set(preset["title_floor"])
        self.variants_var.set(preset["variants"])

    def search(self) -> None:
        raw_id = self.input_var.get().strip()
        try:
            self.source_track_id = extract_track_id(raw_id)
        except ValueError as error:
            messagebox.showerror("Invalid Spotify ID", str(error), parent=self)
            return
        self._save_settings()
        self.search_button.configure(state="disabled", text="Searching…")
        self.status_var.set("Reading the source track and searching Spotify…")
        self.progress_bar.configure(value=0, maximum=1)
        self.progress_bar.pack(fill="x", padx=24, pady=(0, 10), before=self.table.master)
        for row in self.table.get_children():
            self.table.delete(row)
        options = SearchOptions(
            depth=self.depth_var.get(),
            minimum_score=int(self.score_var.get().rstrip("%")),
            maximum_duration_seconds=int(self.duration_var.get().split()[0]),
            exact_title=self.exact_title_var.get(),
            search_pages=max(1, self.pages_var.get()),
            search_repeats=max(1, self.repeats_var.get()),
            releases_per_artist=max(0, self.releases_var.get()),
            global_albums=max(0, self.albums_var.get()),
            global_playlists=max(0, self.playlists_var.get()),
            tracks_per_playlist=max(0, self.playlist_tracks_var.get()),
            minimum_title_similarity=min(100, max(0, self.title_floor_var.get())),
            variant_queries=self.variants_var.get(),
        )
        threading.Thread(target=self._search_worker, args=(raw_id, options), daemon=True).start()

    def _search_worker(self, raw_id: str, options: SearchOptions) -> None:
        try:
            source, matches, releases_scanned = find_matches(
                raw_id,
                options,
                lambda message, current, total: self.after(0, self._update_progress, message, current, total),
            )
            self.after(0, self._show_results, source, matches, releases_scanned)
        except Exception as error:
            self.after(0, self._show_error, str(error))

    def _show_results(self, source, matches: list[Match], coverage: str) -> None:
        self.progress_bar.pack_forget()
        self.matches = matches
        artists = ", ".join(artist.name for artist in source.artists)
        self.status_var.set(f"Source: {source.name} — {artists} · {format_duration(source.duration_ms)} · {len(matches)} alternatives · scanned {coverage}")
        for index, match in enumerate(matches):
            difference = f"{match.duration_delta_ms / 1000:.2f}s"
            self.table.insert("", "end", iid=str(index), values=(f"{match.score}%", match.title, match.artists, format_duration(match.duration_ms), difference, match.album, match.track_id))
        self.search_button.configure(state="normal", text="Find matches")
        if matches:
            self.table.selection_set("0")
            self.table.focus("0")

    def _show_error(self, error: str) -> None:
        self.progress_bar.pack_forget()
        self.search_button.configure(state="normal", text="Find matches")
        self.status_var.set("Search failed.")
        messagebox.showerror("Could not search Spotify", f"{error}\n\nCheck your internet connection and try again.", parent=self)

    def _update_progress(self, message: str, current: int, total: int) -> None:
        total = max(1, total)
        self.progress_bar.configure(maximum=total, value=min(current, total))
        self.status_var.set(f"{message} ({current}/{total})")

    def selected_matches(self) -> list[Match]:
        selection = sorted(self.table.selection(), key=int)
        if not selection:
            messagebox.showinfo("Select a result", "Select a track first.", parent=self)
            return []
        return [self.matches[int(item)] for item in selection]

    def copy_selected(self) -> None:
        matches = self.selected_matches()
        if matches:
            ids = ",".join(match.track_id for match in matches)
            self.clipboard_clear()
            self.clipboard_append(ids)
            self.status_var.set(f"Copied {len(matches)} Spotify ID{'s' if len(matches) != 1 else ''}")

    def copy_selected_with_source(self) -> None:
        matches = self.selected_matches()
        if matches and self.source_track_id:
            ids = [self.source_track_id, *(match.track_id for match in matches if match.track_id != self.source_track_id)]
            self.clipboard_clear()
            self.clipboard_append(",".join(ids))
            self.status_var.set(f"Copied source ID and {len(ids) - 1} selected alternative{'s' if len(ids) != 2 else ''}")

    def select_all(self) -> None:
        self.table.selection_set(self.table.get_children())
        self.status_var.set(f"Selected all {len(self.matches)} results")

    def export_selected(self) -> None:
        matches = self.selected_matches()
        if not matches:
            return
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Export Spotify IDs",
            defaultextension=".txt",
            filetypes=(("Text files", "*.txt"), ("All files", "*.*")),
            initialfile="spotify-ids.txt",
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as output:
            output.write(",".join(match.track_id for match in matches))
        self.status_var.set(f"Exported {len(matches)} IDs to {os.path.basename(path)}")

    def open_selected(self) -> None:
        matches = self.selected_matches()
        if not matches:
            return
        match = matches[0]
        try:
            os.startfile(f"spotify:track:{match.track_id}")
            self.status_var.set(f"Opened {match.track_id} in Spotify")
        except OSError:
            webbrowser.open(f"https://open.spotify.com/track/{match.track_id}")
            self.status_var.set(f"Opened {match.track_id} in your browser")


if __name__ == "__main__":
    SpotMatchApp().mainloop()
