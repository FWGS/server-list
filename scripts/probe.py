#!/usr/bin/env python3
# Probe servers/*.toml via xash3d-query, emit output/v1/servers/<gamedir>.
# State persists in <output>/state.json across runs (grace window for
# spurious failures, pruning of long-gone addresses).

import argparse
import html
import json
import statistics
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

PROTO_XASH = 49
PROTO_GOLDSRC = 48
LIVE_STATUSES = ("ok", "okwithplayers")
BACKOFF = (0.25, 1.0, 2.0, 4.0, 4.0)
STATE_PRUNE_HOURS = 30 * 24
HOUR_WINDOW_HOURS = 14 * 24
WEEKDAY_WINDOW_HOURS = 6 * 7 * 24
SAMPLE_RETAIN_HOURS = WEEKDAY_WINDOW_HOURS

def probe_one(query_bin, address, timeout):
	cmd = [query_bin, "info", address, "-j", "-c", "-P", "-t", str(int(timeout))]

	try:
		out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
	except subprocess.TimeoutExpired:
		return None

	if not out.stdout.strip():
		return None

	try:
		doc = json.loads(out.stdout)
	except json.JSONDecodeError:
		return None

	servers = doc.get("servers") or []
	return servers[0] if servers else None

def probe_with_retry(query_bin, address, tries, timeout):
	for i in range(tries):
		result = probe_one(query_bin, address, timeout)
		if result is not None and result.get("status") in LIVE_STATUSES:
			return result
		if i + 1 < tries:
			time.sleep(BACKOFF[min(i, len(BACKOFF) - 1)])
	return None

def load_sources(servers_dir):
	sources = {}
	for path in sorted(servers_dir.glob("*.toml")):
		gamedir = path.stem
		with open(path, "rb") as f:
			doc = tomllib.load(f)
		entries = doc.get("server") or []
		if not entries:
			continue
		sources[gamedir] = entries
	return sources

def write_output(output_dir, gamedir, addresses):
	out_dir = output_dir / "v1" / "servers"
	out_dir.mkdir(parents=True, exist_ok=True)
	out_path = out_dir / gamedir

	lines = [
		f"# {gamedir} server list",
		f"# Generated {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
		"# Format: <ip|gs> <address>",
		"",
	]
	for addr, proto in addresses:
		directive = "gs" if proto == PROTO_GOLDSRC else "ip"
		lines.append(f"{directive} {addr}")
	lines.append("")
	out_path.write_text("\n".join(lines))
	return out_path

def write_gamedirs(output_dir, gamedirs):
	out_dir = output_dir / "v1"
	out_dir.mkdir(parents=True, exist_ok=True)
	out_path = out_dir / "gamedirs"

	lines = [
		"# gamedir list",
		f"# Generated {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
		"# Format: <gamedir> <total_server_count> <live_server_count>",
		"",
	]
	for gamedir, count, live in gamedirs:
		lines.append(f"{gamedir} {count} {live}")
	lines.append("")
	out_path.write_text("\n".join(lines))
	return out_path

def heatmap_svg(gamedirs, grid, col_labels, total_w):
	cell_h = 30
	label_w = 90
	cols = len(col_labels)
	cell_w = (total_w - label_w) / cols
	width = total_w
	height = (len(gamedirs) + 1) * cell_h

	all_vals = [v for row in grid.values() for v in row if v is not None]
	vmax = max(all_vals) if all_vals else 0

	def color(v):
		if v is None:
			return "#f3f4f6"
		if vmax <= 0:
			return "#eef2ff"
		t = (v / vmax) ** 0.6
		r = int(238 + (49 - 238) * t)
		g = int(242 + (46 - 242) * t)
		b = int(255 + (129 - 255) * t)
		return f"#{r:02x}{g:02x}{b:02x}"

	def text_color(v):
		if v is None or vmax <= 0:
			return "#9ca3af"
		return "#ffffff" if (v / vmax) > 0.45 else "#1f2937"

	def fmt(v):
		if v is None:
			return ""
		if v == int(v):
			return str(int(v))
		return f"{v:.1f}"

	parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:g} {height}" width="100%" style="max-width:{width:g}px" font-family="system-ui,sans-serif" font-size="11">']
	for i, lbl in enumerate(col_labels):
		x = label_w + (i + 0.5) * cell_w
		parts.append(f'<text x="{x:g}" y="{cell_h // 2 + 4}" text-anchor="middle" fill="#6b7280">{html.escape(lbl)}</text>')
	for ri, gd in enumerate(gamedirs):
		y = (ri + 1) * cell_h
		parts.append(f'<text x="{label_w - 8}" y="{y + cell_h // 2 + 4}" text-anchor="end" fill="#1f2937">{html.escape(gd)}</text>')
		for ci, lbl in enumerate(col_labels):
			v = grid[gd][ci]
			x = label_w + ci * cell_w
			parts.append(f'<rect x="{x:g}" y="{y}" width="{cell_w - 1:g}" height="{cell_h - 1}" fill="{color(v)}"><title>{html.escape(gd)} {html.escape(lbl)}: {fmt(v) or "no samples"}</title></rect>')
			label = fmt(v)
			if label:
				cx = label_w + (ci + 0.5) * cell_w
				parts.append(f'<text x="{cx:g}" y="{y + cell_h // 2 + 4}" text-anchor="middle" fill="{text_color(v)}">{label}</text>')
	parts.append('</svg>')
	return "\n".join(parts)

def write_index(output_dir, sources, samples, now):
	gamedirs = sorted(sources.keys())
	now_ts = int(now.timestamp())
	hour_cutoff = now_ts - HOUR_WINDOW_HOURS * 3600
	wday_cutoff = now_ts - WEEKDAY_WINDOW_HOURS * 3600

	hour_grid = {}
	wday_grid = {}
	for gd in gamedirs:
		hb = [[] for _ in range(24)]
		wb = [[] for _ in range(7)]
		for ts, count in samples.get(gd, []):
			dt = datetime.fromtimestamp(ts, tz=timezone.utc)
			c = int(count)
			if ts >= hour_cutoff:
				hb[dt.hour].append(c)
			if ts >= wday_cutoff:
				wb[dt.weekday()].append(c)
		hour_grid[gd] = [statistics.median(b) if b else None for b in hb]
		wday_grid[gd] = [statistics.median(b) if b else None for b in wb]

	chart_w = 90 + 24 * 36
	hour_svg = heatmap_svg(gamedirs, hour_grid, [f"{h:02d}" for h in range(24)], chart_w)
	wday_svg = heatmap_svg(gamedirs, wday_grid, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], chart_w)

	gen_iso = now.replace(microsecond=0).isoformat()
	server_links = "\n".join(
		f'<li><a href="v1/servers/{html.escape(gd)}">v1/servers/{html.escape(gd)}</a></li>'
		for gd in gamedirs
	)
	hour_days = HOUR_WINDOW_HOURS // 24
	wday_weeks = WEEKDAY_WINDOW_HOURS // (7 * 24)

	html_doc = f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>Xash3D FWGS server list</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body {{ font-family: system-ui, sans-serif; max-width: 60rem; margin: 2rem auto; padding: 0 1rem; color: #1f2937; }}
.muted {{ color: #6b7280; }}
.heatmap {{ overflow-x: auto; margin: 1rem 0; }}
</style>
<h1>Xash3D FWGS server list</h1>
<p class="muted">Generated {html.escape(gen_iso)}.</p>

<h2>Median concurrent players, by UTC hour, last {hour_days} days</h2>
<div class="heatmap">
{hour_svg}
</div>

<h2>Median concurrent players, by UTC weekday, last {wday_weeks} weeks</h2>
<div class="heatmap">
{wday_svg}
</div>

<h2>Lists</h2>
<ul>
{server_links}
</ul>

<p><a href="https://github.com/FWGS/server-list">Source and contribution policy</a></p>
"""
	(output_dir / "index.html").write_text(html_doc)
	(output_dir / ".nojekyll").write_text("")

def load_state(path):
	if not path.exists():
		return {}
	try:
		with open(path) as f:
			return json.load(f)
	except (OSError, json.JSONDecodeError):
		return {}

def save_state(path, state):
	path.parent.mkdir(parents=True, exist_ok=True)
	with open(path, "w") as f:
		json.dump(state, f, indent=2, sort_keys=True)

def hours_since(iso, now):
	if not iso:
		return float("inf")
	try:
		dt = datetime.fromisoformat(iso)
	except ValueError:
		return float("inf")
	if dt.tzinfo is None:
		dt = dt.replace(tzinfo=timezone.utc)
	return (now - dt).total_seconds() / 3600.0

def main():
	ap = argparse.ArgumentParser(description=__doc__)
	ap.add_argument("--query", default="xash3d-query", help="path to the xash3d-query binary")
	ap.add_argument("--sources", default="servers", help="directory containing per-gamedir TOML sources")
	ap.add_argument("--output", default="output", help="directory to write the publishable tree into")
	ap.add_argument("--tries", type=int, default=4, help="probe attempts per server")
	ap.add_argument("--timeout", type=int, default=2, help="per-probe response timeout, in whole seconds (passed to xash3d-query -t)")
	ap.add_argument("--grace-hours", type=float, default=48.0, help="keep a silent server published if it responded within this many hours")
	args = ap.parse_args()

	sources_dir = Path(args.sources)
	output_dir = Path(args.output)
	state_path = output_dir / "state.json"

	if not sources_dir.is_dir():
		print(f"error: {sources_dir} does not exist", file=sys.stderr)
		return 2

	sources = load_sources(sources_dir)
	if not sources:
		print(f"error: no .toml files under {sources_dir}", file=sys.stderr)
		return 2

	state = load_state(state_path)
	now = datetime.now(timezone.utc)
	now_iso = now.replace(microsecond=0).isoformat()

	total = sum(len(v) for v in sources.values())
	live_now = 0
	grace_kept = 0
	print(f"probing {total} servers across {len(sources)} gamedirs  (tries={args.tries}, timeout={args.timeout}s, grace={args.grace_hours}h)", flush=True)

	samples = state.setdefault("samples", {})
	now_ts = int(now.timestamp())

	gamedirs = []
	for gamedir, entries in sources.items():
		gd_state = state.setdefault(gamedir, {})
		live_addrs = []
		gd_players = 0
		gd_responding = 0

		for entry in entries:
			address = entry.get("address")
			if not address:
				continue
			proto = int(entry.get("protocol") or PROTO_XASH)
			prev = gd_state.setdefault(address, {})

			result = probe_with_retry(args.query, address, args.tries, args.timeout)

			prev["last_attempt"] = now_iso
			if result is not None:
				prev["last_seen"] = now_iso
				prev["last_ping_ms"] = result.get("ping")
				# use the source's protocol, not the responder's
				live_addrs.append((address, proto))
				live_now += 1
				numcl = int(result.get("numcl") or 0)
				gd_players += numcl
				gd_responding += 1
				print(f"  [+] {gamedir:>12}  {address}  ping={result.get('ping')}ms  players={numcl}", flush=True)
				continue

			age = hours_since(prev.get("last_seen"), now)
			if age <= args.grace_hours:
				live_addrs.append((address, proto))
				grace_kept += 1
				print(f"  [~] {gamedir:>12}  {address}  silent now, last seen {age:.1f}h ago (grace)", flush=True)
			elif prev.get("last_seen"):
				print(f"  [-] {gamedir:>12}  {address}  silent (last seen {age:.1f}h ago)", flush=True)
			else:
				print(f"  [-] {gamedir:>12}  {address}", flush=True)

		gamedirs.append([gamedir, len(entries), len(live_addrs)])

		# always emit a file so the URL is reachable even when 0 servers respond
		live_addrs.sort()
		out_path = write_output(output_dir, gamedir, live_addrs)
		print(f"  -> {out_path}  ({len(live_addrs)} published)", flush=True)

		if gd_responding > 0:
			samples.setdefault(gamedir, []).append([now_ts, gd_players])

	out_path = write_gamedirs(output_dir, gamedirs)
	print(f"  -> {out_path}", flush=True)

	source_addrs = {gd: {e["address"] for e in entries if e.get("address")} for gd, entries in sources.items()}
	pruned = 0
	for gd in list(state.keys()):
		if gd == "samples":
			continue
		if gd not in source_addrs:
			for addr, entry in list(state[gd].items()):
				if hours_since(entry.get("last_seen"), now) > STATE_PRUNE_HOURS:
					del state[gd][addr]
					pruned += 1
			if not state[gd]:
				del state[gd]
			continue
		for addr in list(state[gd].keys()):
			if addr in source_addrs[gd]:
				continue
			if hours_since(state[gd][addr].get("last_seen"), now) > STATE_PRUNE_HOURS:
				del state[gd][addr]
				pruned += 1

	sample_cutoff = now_ts - SAMPLE_RETAIN_HOURS * 3600
	samples_pruned = 0
	for gd in list(samples.keys()):
		kept = [s for s in samples[gd] if s[0] >= sample_cutoff]
		samples_pruned += len(samples[gd]) - len(kept)
		if gd not in sources and not kept:
			del samples[gd]
			continue
		samples[gd] = kept

	write_index(output_dir, sources, samples, now)
	save_state(state_path, state)

	print(f"done: {live_now} responded, {grace_kept} kept by grace, {pruned} pruned from state, {samples_pruned} old samples dropped", flush=True)
	return 0


if __name__ == "__main__":
	sys.exit(main())
