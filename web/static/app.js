"use strict";

// Where searches run: a URL the viewer saved, else the deploy's config.js, else this server.
const SERVER_KEY = "spotmatch.server";

function readSaved() {
	try { return localStorage.getItem(SERVER_KEY) || ""; } catch { return ""; }
}

function cleanBase(url) {
	return (url || "").trim().replace(/\/+$/, "");
}

function apiBase() {
	return cleanBase(readSaved() || window.SPOTMATCH_CONFIG?.apiBase || "");
}

function apiUrl(path) {
	const base = apiBase();
	return base ? `${base}/${path}` : path;
}
const LIKELY_SCORE = 90;
const LIKELY_DELTA_MS = 500;

const $ = (id) => document.getElementById(id);
const form = $("search-form");
const input = $("track");
const button = $("search-button");
const finder = $("finder");
const statusLine = $("status");
const meter = $("meter");
const meterFill = $("meter-fill");
const results = $("results");
const groups = $("groups");
const tray = $("tray");

// The logo's two rings, animated. Left ring is the source track, right ring the
// candidates. The filled lens is always the true overlap (fill moves with the
// right ring, clip with the left), so it grows as the search closes in.
const orbit = (() => {
	const CENTER = 512;
	const REST = 280; // ring distance in the logo
	const APART = 480; // just apart; the lens appears about halfway through a search
	const els = {
		pair: $("orbit-pair"),
		left: $("orbit-left"),
		right: $("orbit-right"),
		fill: $("orbit-fill"),
		clip: $("orbit-clip-circle"),
	};
	const still = matchMedia("(prefers-reduced-motion: reduce)");
	const state = { mode: "idle", progress: 0, dist: REST, distV: 0, angle: 0, angleV: 0, lens: 1, time: 0 };
	let frame = 0;
	let last = 0;

	function spring(value, velocity, target, dt, stiffness, damping) {
		const accel = stiffness * (target - value) - damping * velocity;
		velocity += accel * dt;
		return [value + velocity * dt, velocity];
	}

	function draw() {
		const half = state.dist / 2;
		const lx = CENTER - half;
		const rx = CENTER + half;
		els.left.setAttribute("cx", lx);
		els.clip.setAttribute("cx", lx);
		els.right.setAttribute("cx", rx);
		els.fill.setAttribute("cx", rx);
		els.fill.style.opacity = state.lens;
		els.pair.setAttribute("transform", `rotate(${state.angle % 360} ${CENTER} ${CENTER})`);
	}

	function targets() {
		if (state.mode === "searching") {
			const breathe = 16 * Math.sin(state.time * 2.4);
			return { dist: APART - (APART - REST - 40) * state.progress + breathe, lens: 0.25 + 0.35 * state.progress, spin: 46 };
		}
		if (state.mode === "failed") return { dist: 430, lens: 0.12, spin: 0 };
		return { dist: REST, lens: 1, spin: 0 };
	}

	function tick(now) {
		const dt = Math.min(0.05, (now - last) / 1000 || 0.016);
		last = now;
		state.time += dt;
		const goal = targets();
		if (goal.spin) {
			state.angle += goal.spin * dt;
			state.angleV = goal.spin;
		} else {
			// Settle on the nearest half turn ahead; the logo is symmetric at 180 degrees.
			const rest = Math.ceil((state.angle - 1) / 180) * 180;
			[state.angle, state.angleV] = spring(state.angle, state.angleV, rest, dt, 60, 13);
		}
		const [stiff, damp] = state.mode === "idle" ? [140, 13] : [40, 11];
		[state.dist, state.distV] = spring(state.dist, state.distV, goal.dist, dt, stiff, damp);
		state.lens += (goal.lens - state.lens) * Math.min(1, dt * 5);
		draw();
		const settled = !goal.spin && Math.abs(state.distV) < 0.5 && Math.abs(state.angleV) < 0.5
			&& Math.abs(goal.dist - state.dist) < 0.5 && Math.abs(goal.lens - state.lens) < 0.01;
		frame = settled ? 0 : requestAnimationFrame(tick);
	}

	function set(mode) {
		state.mode = mode;
		if (still.matches) {
			const goal = targets();
			Object.assign(state, { dist: goal.dist, lens: goal.lens, angle: 0, distV: 0, angleV: 0 });
			draw();
			return;
		}
		if (!frame) {
			last = performance.now();
			frame = requestAnimationFrame(tick);
		}
	}

	return {
		search() { state.progress = 0; set("searching"); },
		progress(value) { state.progress = Math.max(state.progress, value); if (still.matches) set("searching"); },
		found() { set("idle"); },
		failed() { set("failed"); },
	};
})();

// Progress events restart per phase; fold them into one 0..1 value.
const PHASES = [
	["Reading source", 0, 0.05],
	["Search pass", 0.05, 0.25],
	["Paginated pass", 0.3, 0.7],
];
let overall = 0;

function phaseProgress(message, done, total) {
	const phase = PHASES.find(([prefix]) => message.startsWith(prefix));
	if (!phase) return overall;
	overall = Math.max(overall, phase[1] + phase[2] * (done / Math.max(1, total)));
	return overall;
}

let current = null; // { source, matches, coverage, cached, searched_at }
let selected = new Set();
let renderedIds = []; // rows in on-screen order, for shift-click ranges
let anchor = null;
let controller = null;
let toastTimer = 0;

function setStatus(text, isError = false) {
	statusLine.textContent = text;
	statusLine.classList.toggle("is-error", isError);
}

function setSearching(on) {
	finder.classList.toggle("is-searching", on);
	if (on) {
		overall = 0;
		orbit.search();
	}
	button.disabled = on;
	button.textContent = on ? "Searching..." : "Find matches";
	meter.hidden = !on;
	if (!on) meterFill.style.width = "0";
}

function formatDuration(ms) {
	const seconds = Math.round(ms / 1000);
	return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function formatDelta(ms) {
	if (ms === 0) return "exact";
	return ms < 1000 ? `${ms} ms off` : `${(ms / 1000).toFixed(1)} s off`;
}

function normalize(text) {
	return text.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
		.replace(/&/g, " and ").match(/[a-z0-9]+/g)?.join(" ") ?? "";
}

function escapeHtml(text) {
	return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function toast(text) {
	const el = $("toast");
	el.textContent = text;
	el.hidden = false;
	clearTimeout(toastTimer);
	toastTimer = setTimeout(() => { el.hidden = true; }, 2200);
}

// Server-sent events over fetch, so 400/429 bodies come back as readable errors.
async function search(raw) {
	controller?.abort();
	controller = new AbortController();
	selected = new Set();
	anchor = null;
	updateTray();
	setSearching(true);
	setStatus("Starting the search...");

	let response;
	try {
		response = await fetch(apiUrl(`api/match?track=${encodeURIComponent(raw)}`), { signal: controller.signal });
	} catch (error) {
		if (error.name !== "AbortError") failed(apiBase()
			? `Couldn't reach the SpotMatch server at ${apiBase()}. Check the address under Server, or switch back to the default.`
			: "Couldn't reach the SpotMatch server. Check your connection and try again.");
		return;
	}
	if (!response.ok) {
		const body = await response.json().catch(() => ({}));
		failed(body.error || `The server answered ${response.status}.`);
		return;
	}

	const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
	let buffer = "";
	try {
		for (;;) {
			const { value, done } = await reader.read();
			if (done) break;
			buffer += value;
			let split;
			while ((split = buffer.indexOf("\n\n")) >= 0) {
				const chunk = buffer.slice(0, split);
				buffer = buffer.slice(split + 2);
				const data = chunk.split("\n").find((line) => line.startsWith("data: "));
				if (data) handle(JSON.parse(data.slice(6)));
			}
		}
	} catch (error) {
		if (error.name !== "AbortError") failed("The connection dropped before the search finished. Try again.");
		return;
	}
	if (finder.classList.contains("is-searching")) failed("The search ended without a result. Try again.");
}

function handle(event) {
	if (event.type === "queued") {
		setStatus(`Waiting for a free search slot (${event.position} ahead of you)...`);
	} else if (event.type === "progress") {
		setStatus(`${event.message}...`);
		const value = phaseProgress(event.message, event.current, event.total);
		orbit.progress(value);
		meterFill.style.width = `${Math.round(value * 100)}%`;
	} else if (event.type === "result") {
		setSearching(false);
		orbit.found();
		current = cleanResult(event);
		const count = current.matches.length;
		setStatus(event.cached ? `Showing a saved result from ${new Date(event.searched_at * 1000).toLocaleString()}.` : `Found ${count} candidate${count === 1 ? "" : "s"}.`);
		finder.classList.add("is-compact");
		render();
	} else if (event.type === "error") {
		failed(event.message);
	}
}

// The page may talk to a server someone else runs, so pin every field to the
// type the renderer expects before any of it reaches innerHTML.
function cleanResult(event) {
	const text = (value) => String(value ?? "");
	const number = (value) => (Number.isFinite(Number(value)) ? Number(value) : 0);
	const id = (value) => (/^[A-Za-z0-9]{22}$/.test(String(value)) ? String(value) : "");
	const source = event.source || {};
	return {
		cached: Boolean(event.cached),
		searched_at: number(event.searched_at),
		coverage: text(event.coverage),
		source: {
			track_id: id(source.track_id),
			title: text(source.title),
			artists: text(source.artists),
			album: text(source.album),
			duration_ms: number(source.duration_ms),
			image: /^https:\/\//.test(text(source.image)) ? text(source.image) : null,
		},
		matches: (Array.isArray(event.matches) ? event.matches : [])
			.map((m) => ({
				track_id: id(m.track_id),
				title: text(m.title),
				artists: text(m.artists),
				album: text(m.album),
				duration_ms: number(m.duration_ms),
				score: number(m.score),
				duration_delta_ms: number(m.duration_delta_ms),
			}))
			.filter((m) => m.track_id),
	};
}

function failed(message) {
	setSearching(false);
	orbit.failed();
	setStatus(message, true);
}

function filtered() {
	const minScore = Number($("min-score").value);
	const maxDelta = Number($("max-delta").value);
	const exact = $("exact-title").checked;
	const sourceTitle = normalize(current.source.title);
	return current.matches.filter((m) =>
		m.score >= minScore && m.duration_delta_ms <= maxDelta && (!exact || normalize(m.title) === sourceTitle));
}

function render() {
	const { source } = current;
	$("source").innerHTML = `
		${source.image ? `<img src="${escapeHtml(source.image)}" alt="">` : `<div class="cover-fallback"></div>`}
		<div>
			<h2>${escapeHtml(source.title)}</h2>
			<p class="who">${escapeHtml(source.artists)} on ${escapeHtml(source.album)}</p>
			<p class="facts">${formatDuration(source.duration_ms)}, ID <code>${escapeHtml(source.track_id)}</code></p>
		</div>`;
	const checked = Number(current.coverage.match(/(\d+) paginated/)?.[1] ?? 0);
	const passes = Number(current.coverage.match(/(\d+) search passes/)?.[1] ?? 1);
	$("coverage").textContent = checked
		? `Went through ${checked.toLocaleString()} Spotify search results in ${passes} passes.`
		: "";
	results.hidden = false;

	const visible = filtered();
	const likely = visible.filter((m) => m.score >= LIKELY_SCORE && m.duration_delta_ms <= LIKELY_DELTA_MS);
	const others = visible.filter((m) => !likely.includes(m));

	groups.innerHTML = "";
	renderedIds = [...likely, ...others].map((m) => m.track_id);
	if (!visible.length) {
		groups.innerHTML = `<p class="empty">No candidates pass these filters. Lower the minimum score or widen the duration.</p>`;
	} else {
		if (likely.length) groups.append(group("Likely the same recording", "Same title and artists, within half a second. Still listen before you share a TTML.", likely));
		if (others.length) groups.append(group("Check these", "Different length, edition or title wording. Often a remaster, edit or live take.", others));
	}
	updateTray();
}

function group(title, note, matches) {
	const section = document.createElement("section");
	section.className = "group";
	section.innerHTML = `
		<div class="group-head">
			<div><h3>${title} <span class="count">(${matches.length})</span></h3><p>${note}</p></div>
			<button type="button" class="link-button">Select all ${matches.length}</button>
		</div>
		<ul class="rows"></ul>`;
	const list = section.querySelector(".rows");
	for (const m of matches) list.append(row(m));
	section.querySelector(".link-button").addEventListener("click", () => {
		const allOn = matches.every((m) => selected.has(m.track_id));
		for (const m of matches) allOn ? selected.delete(m.track_id) : selected.add(m.track_id);
		syncRows();
		updateTray();
	});
	return section;
}

function row(m) {
	const li = document.createElement("li");
	li.className = "row" + (selected.has(m.track_id) ? " is-selected" : "");
	li.dataset.id = m.track_id;
	li.title = `${m.track_id}. Shift-click to select a range, double-click to open in Spotify.`;
	li.innerHTML = `
		<input type="checkbox" aria-label="Select ${escapeHtml(m.title)}" ${selected.has(m.track_id) ? "checked" : ""}>
		<div class="title-cell"><div class="title">${escapeHtml(m.title)}</div><div class="sub">${escapeHtml(m.artists)}</div></div>
		<div class="album-cell"><div class="album">${escapeHtml(m.album)}</div></div>
		<div class="time">${formatDuration(m.duration_ms)}<small class="${m.duration_delta_ms === 0 ? "is-exact" : ""}">${formatDelta(m.duration_delta_ms)}</small></div>
		<div class="score" aria-label="Match score">${m.score}</div>
		<a class="open" href="spotify:track:${encodeURIComponent(m.track_id)}" aria-label="Open in Spotify" title="Open in Spotify">
			<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M6 3H3v10h10v-3M9 2h5v5M14 2 7.5 8.5"/></svg>
		</a>`;
	li.addEventListener("mousedown", (event) => {
		if (event.shiftKey) event.preventDefault(); // keep shift-click from selecting text
	});
	li.addEventListener("click", (event) => {
		if (event.target.closest("a")) return;
		const box = li.querySelector("input");
		// A click on the checkbox has already flipped it; anywhere else flips it here.
		const on = event.target === box ? box.checked : !selected.has(m.track_id);
		if (event.shiftKey && anchor && anchor !== m.track_id) {
			const from = renderedIds.indexOf(anchor);
			const to = renderedIds.indexOf(m.track_id);
			if (from >= 0 && to >= 0) {
				const [lo, hi] = from < to ? [from, to] : [to, from];
				for (const id of renderedIds.slice(lo, hi + 1)) on ? selected.add(id) : selected.delete(id);
			}
		} else {
			on ? selected.add(m.track_id) : selected.delete(m.track_id);
		}
		anchor = m.track_id;
		syncRows();
		updateTray();
	});
	li.addEventListener("dblclick", (event) => {
		if (event.target.closest("a, input")) return;
		window.location.href = `spotify:track:${m.track_id}`;
	});
	return li;
}

function syncRows() {
	for (const li of groups.querySelectorAll(".row")) {
		const on = selected.has(li.dataset.id);
		li.classList.toggle("is-selected", on);
		li.querySelector("input").checked = on;
	}
}

function selectedIds() {
	// Keep the on-screen order so pasted lists read the same way as the page.
	const ids = renderedIds.filter((id) => selected.has(id));
	const hidden = current ? current.matches.map((m) => m.track_id).filter((id) => selected.has(id) && !ids.includes(id)) : [];
	const all = [...ids, ...hidden];
	return $("source-first").checked && current ? [current.source.track_id, ...all] : all;
}

// The bot takes IDs once, in a Discord modal capped at 4000 characters.
// Longer lists go out as a .txt file instead.
const DISCORD_LIMIT = 4000;

function updateTray() {
	const count = selected.size;
	tray.hidden = count === 0;
	$("tray-count").textContent = `${count} selected`;
	const tooLong = selectedIds().join(",").length > DISCORD_LIMIT;
	$("tray-note").hidden = !tooLong;
	$("export-text").classList.toggle("primary", tooLong);
	$("copy-selected").classList.toggle("primary", !tooLong);
}

async function copySelected() {
	const ids = selectedIds();
	const text = ids.join(",");
	try {
		await navigator.clipboard.writeText(text);
		toast(text.length > DISCORD_LIMIT
			? `Copied ${ids.length} IDs, but that's more than the bot's modal accepts. Export a .txt instead.`
			: `Copied ${ids.length} ID${ids.length === 1 ? "" : "s"}.`);
	} catch {
		window.prompt("Copy these IDs:", text);
	}
}

function exportText() {
	const text = `${selectedIds().join(",")}\n`;
	const link = document.createElement("a");
	link.href = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
	link.download = `spotmatch-${current.source.track_id}.txt`;
	link.click();
	setTimeout(() => URL.revokeObjectURL(link.href), 1000);
	toast(`Saved ${link.download}.`);
}

form.addEventListener("submit", (event) => {
	event.preventDefault();
	const raw = input.value.trim();
	if (!/[A-Za-z0-9]{22}/.test(raw)) {
		failed("That doesn't look like a Spotify track link. Paste a link like open.spotify.com/track/... or a 22-character ID.");
		return;
	}
	const url = new URL(window.location.href);
	url.searchParams.set("track", raw.match(/[A-Za-z0-9]{22}/)[0]);
	history.replaceState(null, "", url);
	search(raw);
});

for (const id of ["min-score", "max-delta", "exact-title"]) $(id).addEventListener("change", () => current && render());

$("copy-selected").addEventListener("click", copySelected);
$("export-text").addEventListener("click", exportText);
$("source-first").addEventListener("change", updateTray);
$("clear-selection").addEventListener("click", () => { selected.clear(); anchor = null; syncRows(); updateTray(); });

// Shareable links: ?track=<id> runs the search on load.
const initial = new URLSearchParams(window.location.search).get("track");
if (initial) {
	input.value = initial;
	search(initial);
}

// Ctrl+A selects every visible result, Esc clears, unless you're typing in a field.
document.addEventListener("keydown", (event) => {
	if (event.target.closest("input, textarea, select, dialog") || results.hidden) return;
	if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
		event.preventDefault();
		for (const id of renderedIds) selected.add(id);
		syncRows();
		updateTray();
	} else if (event.key === "Escape" && selected.size) {
		selected.clear();
		anchor = null;
		syncRows();
		updateTray();
	}
});

// Server setting
const serverDialog = $("server-dialog");
const serverUrl = $("server-url");
const serverStatus = $("server-status");

function serverMessage(text, kind = "") {
	serverStatus.textContent = text;
	serverStatus.className = `dialog-status ${kind ? `is-${kind}` : ""}`;
}

$("server-button").addEventListener("click", () => {
	serverUrl.value = readSaved();
	serverUrl.placeholder = window.SPOTMATCH_CONFIG?.apiBase || "Same server as this page";
	serverMessage(readSaved() ? "Using your saved server." : "Using the default server.");
	serverDialog.showModal();
});

$("server-cancel").addEventListener("click", () => serverDialog.close());

$("server-reset").addEventListener("click", () => {
	try { localStorage.removeItem(SERVER_KEY); } catch {}
	serverDialog.close();
	toast("Using the default server.");
});

$("server-form").addEventListener("submit", async (event) => {
	event.preventDefault();
	const base = cleanBase(serverUrl.value);
	if (!base) {
		$("server-reset").click();
		return;
	}
	if (!/^https?:\/\//.test(base)) {
		serverMessage("Enter a full address starting with https:// or http://.", "error");
		return;
	}
	serverMessage("Checking the server...");
	try {
		const health = await fetch(`${base}/api/health`, { signal: AbortSignal.timeout(8000) });
		const body = await health.json();
		if (!health.ok || !body.ok) throw new Error();
	} catch {
		serverMessage("No SpotMatch server answered there. Check the address, and that the server allows this site in SPOTMATCH_ALLOWED_ORIGINS.", "error");
		return;
	}
	try { localStorage.setItem(SERVER_KEY, base); } catch {}
	serverDialog.close();
	toast(`Searches now run on ${base}.`);
});
