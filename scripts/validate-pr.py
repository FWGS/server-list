#!/usr/bin/env python3
# Validate a PR against its base branch: find the server entries it adds or
# meaningfully changes, probe them, and check them against the contribution
# policy. Writes a Markdown report for the PR comment. Exits non-zero only for
# duplicate entries the PR introduces.

import argparse
import collections
import importlib.util
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROTO_XASH = 49
PROTO_GOLDSRC = 48
PROTO_NAMES = {PROTO_XASH: "Xash", PROTO_GOLDSRC: "GoldSrc"}
MAX_PROBE_ENTRIES = 50
COMMENT_MARKER = "<!-- pr-validate-bot -->"
FOOTER = "<sub>Probes can be flaky on the first try; the nightly publish workflow re-probes with a 48 h grace window, so a single :x: here is not necessarily fatal. Re-push to re-run.</sub>"

TABLE_HEADER = [
	"| Gamedir | Address | Change | Claimed | Responder | Host | Notes |",
	"|---|---|---|---|---|---|---|",
]

# --------------------------------------------------------------- sources

def import_probe(probe_path):
	spec = importlib.util.spec_from_file_location("probe", probe_path)
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod

def git_show(ref, path):
	r = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, check=False)
	if r.returncode != 0:
		return None
	return r.stdout

def list_tomls(ref, sources_dir):
	r = subprocess.run(["git", "ls-tree", "-r", "--name-only", ref, "--", sources_dir], capture_output=True, text=True, check=True)
	return [l for l in r.stdout.splitlines() if l.endswith(".toml")]

def load_entries(ref, sources_dir):
	gamedirs = {}
	errors = []
	for path in list_tomls(ref, sources_dir):
		raw = git_show(ref, path)
		if raw is None:
			continue
		try:
			doc = tomllib.loads(raw.decode("utf-8"))
		except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
			errors.append((path, str(e)))
			continue
		gamedirs[Path(path).stem] = doc.get("server") or []
	return gamedirs, errors

# --------------------------------------------------------------- entries

def parse_protocol(raw):
	# the declared protocol, or None when it isn't one we can probe with
	if raw is None:
		return PROTO_XASH
	try:
		value = int(raw)
	except (TypeError, ValueError):
		return None
	return value if value in PROTO_NAMES else None

def probe_fields(entry):
	# fields that decide what gets published for an address, so a change to
	# any of them warrants a re-probe even when the address is untouched
	return {
		"protocol": entry.get("protocol", PROTO_XASH),
		"force": bool(entry.get("force")),
	}

def diff_probe_fields(before, after):
	a, b = probe_fields(before), probe_fields(after)
	return [f"`{k}` {a[k]} → {b[k]}" for k in a if a[k] != b[k]]

@dataclass
class Candidate:
	"""An entry this PR adds or meaningfully changes."""
	gamedir: str
	entry: dict
	change: str  # "new", or a description of the fields that changed

	@property
	def address(self):
		return self.entry["address"]

	@property
	def protocol(self):
		return parse_protocol(self.entry.get("protocol"))

	@property
	def contact(self):
		return (self.entry.get("contact") or "").strip()

def collect_candidates(base, head):
	base_map = {(g, e["address"]): e for g, es in base.items() for e in es if e.get("address")}

	candidates, invalid = [], []
	for gamedir, entries in head.items():
		for entry in entries:
			if not entry.get("address"):
				invalid.append((gamedir, "entry has no `address` field"))
				continue
			previous = base_map.get((gamedir, entry["address"]))
			if previous is None:
				# an unknown address is either a brand new entry or an existing
				# one whose address changed: indistinguishable, and both want a probe
				candidates.append(Candidate(gamedir, entry, "new"))
				continue
			changes = diff_probe_fields(previous, entry)
			if changes:
				candidates.append(Candidate(gamedir, entry, ", ".join(changes)))
	return candidates, invalid

# ------------------------------------------------------------ duplicates

@dataclass
class Duplicates:
	within: dict  # (gamedir, address) -> how many times it appears
	across: dict  # address -> the gamedirs listing it

	def __bool__(self):
		return bool(self.within or self.across)

def find_duplicates(gamedirs):
	# an address:port serves exactly one gamedir, so it must appear exactly
	# once across the whole source tree
	counts = collections.Counter()
	for gamedir, entries in gamedirs.items():
		for entry in entries:
			if entry.get("address"):
				counts[(gamedir, entry["address"])] += 1

	by_addr = collections.defaultdict(set)
	for gamedir, address in counts:
		by_addr[address].add(gamedir)

	return Duplicates(
		within={k: n for k, n in counts.items() if n > 1},
		across={a: sorted(gs) for a, gs in by_addr.items() if len(gs) > 1},
	)

def introduces_duplicates(head_dupes, base_dupes):
	# duplicates already sitting on the base branch are not this PR's fault
	return (any(k not in base_dupes.within for k in head_dupes.within)
		or any(a not in base_dupes.across for a in head_dupes.across))

# ---------------------------------------------------------------- report

def md_escape(s):
	return (s or "").replace("|", "\\|").replace("\n", " ").replace("\r", " ")

def row(*cells):
	return "| " + " | ".join(cells) + " |"

def protocol_cell(value, raw=None):
	if value in PROTO_NAMES:
		return f"{value} ({PROTO_NAMES[value]})"
	return f"`{raw if raw is not None else value}` :x:"

def parse_error_lines(errors):
	if not errors:
		return []
	return [
		"### :x: TOML parse errors",
		*(f"- `{path}`: {md_escape(e)}" for path, e in errors),
		"",
		"Fix these before merging — the publish workflow would fail on main otherwise.",
		"",
	]

def duplicate_lines(head_dupes, base_dupes):
	if not head_dupes:
		return []

	def seen_before(is_old):
		return " *(already on the base branch)*" if is_old else ""

	lines = ["### :x: Duplicate entries"]
	for (gamedir, address), n in sorted(head_dupes.within.items()):
		lines.append(f"- `{address}` appears {n} times in `{gamedir}.toml`"
			+ seen_before((gamedir, address) in base_dupes.within))
	for address, gamedirs in sorted(head_dupes.across.items()):
		listed = ", ".join(f"`{g}`" for g in gamedirs)
		lines.append(f"- `{address}` is listed under several gamedirs: {listed}"
			+ seen_before(address in base_dupes.across)
			+ " — one address:port serves a single gamedir")
	lines.append("")
	lines.append("Every address must appear exactly once. `probe.py` keys its state and its probe targets by address, so the extra copies are silently dropped from the published list while still inflating the entry count in `v1/gamedirs`.")
	lines.append("")
	return lines

def result_cells(candidate, result, rejection=None):
	"""The responder, host and notes cells for one probed entry."""
	if rejection:
		return "—", "—", f":x: {rejection}; not probed — see below."

	if candidate.protocol is None:
		return "—", "—", ":x: unusable `protocol`, not probed — see below."

	# probe.py queries each entry on its declared protocol only, and reports
	# an answer on any other protocol as a non-response
	wrong = result.get("wrong_protocol") if result else None
	if wrong is not None:
		return protocol_cell(wrong), "—", (
			f":x: no answer on protocol {candidate.protocol}; it answered on {wrong} "
			"instead. Fix `protocol`, or the server, so they agree — it will not be "
			"published as-is.")

	if result is None:
		return "—", "—", ":x: no response — server unreachable; will not be published until it answers"

	note = ":white_check_mark: answered on the declared protocol."
	gamedir = result.get("gamedir") or ""
	if gamedir != candidate.gamedir:
		note += f" responder gamedir: `{md_escape(gamedir)}`."

	resolved = result.get("resolved")
	if resolved and resolved != candidate.address:
		note += f" Resolves to `{md_escape(resolved)}`, published as IP."

	return protocol_cell(candidate.protocol), f"`{md_escape(result.get('host') or '')}`", note

def probe_lines(probe, candidates, rejections, query_bin, timeout):
	if not candidates:
		return ["No new or changed server entries to probe.", ""]

	lines = []
	probing, skipped = candidates, len(candidates) - MAX_PROBE_ENTRIES
	if skipped > 0:
		probing = candidates[:MAX_PROBE_ENTRIES]
		lines.append(f"> Probing the first {MAX_PROBE_ENTRIES} of {len(candidates)} new or changed entries.")
		lines.append("")

	targets = [(c.address, c.protocol) for c in probing
		if c.protocol is not None and c.address not in rejections]
	results = probe.probe_all(query_bin, targets, timeout) if targets else {}

	n = len(probing)
	lines.append(f"Probed {n} new or changed entr{'y' if n == 1 else 'ies'}.")
	lines.append("")
	lines += TABLE_HEADER
	for c in probing:
		responder, host, note = result_cells(c, results.get((c.address, c.protocol)),
			rejections.get(c.address))
		lines.append(row(f"`{c.gamedir}`", f"`{c.address}`", md_escape(c.change),
			protocol_cell(c.protocol, c.entry.get("protocol")), responder, host, note))

	if skipped > 0:
		lines.append("")
		lines.append(f"> Skipped probing {skipped} additional entries. Open a smaller PR if you need all of them validated automatically.")
	return lines

def invalid_lines(invalid):
	if not invalid:
		return []
	return ["", "### :x: Invalid entries",
		*(f"- `{gamedir}`: {why}" for gamedir, why in invalid)]

def non_public_lines(rejections):
	if not rejections:
		return []
	return [
		"",
		"### :x: Address is not publicly routable",
		*(f"- `{address}` {why}" for address, why in sorted(rejections.items())),
		"",
		"Entries must point at a public address. The prober refuses these so a list entry cannot aim it at a private network, and an unroutable address would be useless to clients anyway.",
	]

def bad_protocol_lines(candidates):
	bad = [c for c in candidates if c.protocol is None]
	if not bad:
		return []
	return ["", "### :x: Unknown protocol",
		*(f"- `{c.gamedir}` / `{c.address}` declares `protocol = {c.entry.get('protocol')}`"
			f" — expected {PROTO_GOLDSRC} (GoldSrc) or {PROTO_XASH} (Xash)." for c in bad)]

def missing_contact_lines(candidates):
	missing = [c for c in candidates if not c.contact]
	if not missing:
		return []
	return [
		"",
		"### :warning: Missing `contact`",
		"Per the [contribution policy](../blob/main/README.md#contribution-policy), new entries must include a `contact` field so maintainers can reach the operator. Please add one of:",
		"- email, e.g. `contact = \"admin@example.com\"`",
		"- Discord, as `discord:username` or a stable invite link",
		"- Telegram, as `telegram:@username` or a group link",
		"",
		"Affected entries:",
		*(f"- `{c.gamedir}` / `{c.address}`" for c in missing),
	]

def emit(lines, dest):
	text = "\n".join(lines).rstrip() + "\n"
	if dest == "-":
		sys.stdout.write(text)
	else:
		Path(dest).write_text(text)

# ------------------------------------------------------------------ main

def main():
	ap = argparse.ArgumentParser(description=__doc__)
	ap.add_argument("--base-ref", required=True)
	ap.add_argument("--head-ref", required=True)
	ap.add_argument("--query", required=True, help="path to xash3d-query")
	ap.add_argument("--sources", default="servers")
	ap.add_argument("--probe-script", default="scripts/probe.py")
	ap.add_argument("--timeout", type=int, default=6)
	ap.add_argument("--out", default="-", help="output file; - for stdout")
	args = ap.parse_args()

	base, _ = load_entries(args.base_ref, args.sources)
	head, head_errs = load_entries(args.head_ref, args.sources)

	head_dupes = find_duplicates(head)
	base_dupes = find_duplicates(base)
	candidates, invalid = collect_candidates(base, head)

	lines = [COMMENT_MARKER, "## Server list PR validation", ""]
	lines += parse_error_lines(head_errs)
	lines += duplicate_lines(head_dupes, base_dupes)

	if not (candidates or head_errs or invalid or head_dupes):
		lines.append("No new or changed server entries in this PR. Nothing to probe.")
		emit(lines, args.out)
		return 0

	probe = import_probe(args.probe_script)
	rejections = {c.address: why for c in candidates
		if (why := probe.address_rejection(c.address))}

	lines += probe_lines(probe, candidates, rejections, args.query, args.timeout)
	lines += invalid_lines(invalid)
	lines += non_public_lines(rejections)
	lines += bad_protocol_lines(candidates)
	lines += missing_contact_lines(candidates)
	lines += ["", "---", FOOTER]

	emit(lines, args.out)
	return 1 if introduces_duplicates(head_dupes, base_dupes) else 0

if __name__ == "__main__":
	sys.exit(main())
