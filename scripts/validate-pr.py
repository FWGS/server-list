#!/usr/bin/env python3

import argparse
import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

PROTO_XASH = 49
PROTO_GOLDSRC = 48
PROTO_NAMES = {PROTO_XASH: "Xash", PROTO_GOLDSRC: "GoldSrc"}
VALID_PROTOCOLS = (PROTO_XASH, PROTO_GOLDSRC)
MAX_PROBE_ENTRIES = 50
COMMENT_MARKER = "<!-- pr-validate-bot -->"

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
		gamedir = Path(path).stem
		raw = git_show(ref, path)
		if raw is None:
			continue
		try:
			doc = tomllib.loads(raw.decode("utf-8"))
		except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
			errors.append((path, str(e)))
			continue
		gamedirs[gamedir] = doc.get("server") or []
	return gamedirs, errors

def md_escape(s):
	return (s or "").replace("|", "\\|").replace("\n", " ").replace("\r", " ")

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

	lines = [COMMENT_MARKER, "## Server list PR validation", ""]

	if head_errs:
		lines.append("### :x: TOML parse errors")
		for path, e in head_errs:
			lines.append(f"- `{path}`: {md_escape(e)}")
		lines.append("")
		lines.append("Fix these before merging — the publish workflow would fail on main otherwise.")
		lines.append("")

	base_set = {(g, e.get("address")) for g, es in base.items() for e in es if e.get("address")}

	new_entries = []
	invalid_entries = []
	for g, es in head.items():
		for e in es:
			addr = e.get("address")
			if not addr:
				invalid_entries.append((g, "<missing address>", "entry has no `address` field"))
				continue
			if (g, addr) in base_set:
				continue
			new_entries.append((g, e))

	if not new_entries and not head_errs and not invalid_entries:
		lines.append("No new server entries in this PR. Nothing to probe.")
		emit(lines, args.out)
		return 0

	probed = []
	if new_entries:
		truncated = False
		if len(new_entries) > MAX_PROBE_ENTRIES:
			lines.append(f"> Probing the first {MAX_PROBE_ENTRIES} of {len(new_entries)} new entries.")
			lines.append("")
			new_entries = new_entries[:MAX_PROBE_ENTRIES]
			truncated = True

		probe = import_probe(args.probe_script)

		results = probe.probe_all(args.query, [e["address"] for _, e in new_entries], args.timeout)

		n = len(new_entries)
		lines.append(f"Probed {n} new entr{'y' if n == 1 else 'ies'}.")
		lines.append("")
		lines.append("| Gamedir | Address | Claimed | Responder | Host | Notes |")
		lines.append("|---|---|---|---|---|---|")

		for g, e in new_entries:
			addr = e["address"]
			claimed = e.get("protocol")
			try:
				claimed_int = int(claimed) if claimed is not None else PROTO_XASH
			except (TypeError, ValueError):
				claimed_int = None

			result = results.get(addr)
			probed.append((g, addr, e, claimed_int, result))

			claimed_cell = f"{claimed_int} ({PROTO_NAMES[claimed_int]})" if claimed_int in PROTO_NAMES else f"`{claimed}` :x:"

			if result is None:
				lines.append(f"| `{g}` | `{addr}` | {claimed_cell} | — | — | :x: no response — server unreachable; will not be published until it answers |")
				continue

			got = result.get("protocol")
			host = md_escape(result.get("host") or "")
			gd = result.get("gamedir") or ""
			gd_note = "" if gd == g else f" responder gamedir: `{md_escape(gd)}`."
			got_cell = f"{got} ({PROTO_NAMES[got]})" if got in PROTO_NAMES else f"`{got}`"

			if got == claimed_int:
				note = ":white_check_mark: protocol match." + gd_note
			elif claimed_int == PROTO_GOLDSRC and got == PROTO_XASH:
				note = ":warning: responder advertises Xash. This is the known GoldSrc-fronting-as-Xash workaround (legacy UDP master compatibility), accepted." + gd_note
			elif claimed_int == PROTO_XASH and got == PROTO_GOLDSRC:
				note = ":warning: claimed Xash but responder advertises GoldSrc — please double-check `protocol`." + gd_note
			else:
				note = f":warning: unexpected responder protocol {got}." + gd_note

			lines.append(f"| `{g}` | `{addr}` | {claimed_cell} | {got_cell} | `{host}` | {note} |")

		if truncated:
			lines.append("")
			lines.append(f"> Skipped probing {len(new_entries) - MAX_PROBE_ENTRIES} additional entries. Open a smaller PR if you need all of them validated automatically.")

	# Post-table policy checks.
	bad_proto = []
	missing_contact = []
	for g, addr, e, claimed_int, _ in probed:
		if claimed_int not in VALID_PROTOCOLS:
			bad_proto.append((g, addr, e.get("protocol")))
		if not (e.get("contact") or "").strip():
			missing_contact.append((g, addr))

	if invalid_entries:
		lines.append("")
		lines.append("### :x: Invalid entries")
		for g, addr, why in invalid_entries:
			lines.append(f"- `{g}` / `{addr}`: {why}")

	if bad_proto:
		lines.append("")
		lines.append("### :x: Unknown protocol")
		for g, addr, p in bad_proto:
			lines.append(f"- `{g}` / `{addr}` declares `protocol = {p}` — expected 48 (GoldSrc) or 49 (Xash).")

	if missing_contact:
		lines.append("")
		lines.append("### :warning: Missing `contact`")
		lines.append("Per the [contribution policy](../blob/main/README.md#contribution-policy), new entries must include a `contact` field so maintainers can reach the operator. Please add one of:")
		lines.append("- email, e.g. `contact = \"admin@example.com\"`")
		lines.append("- Discord, as `discord:username` or a stable invite link")
		lines.append("- Telegram, as `telegram:@username` or a group link")
		lines.append("")
		lines.append("Affected entries:")
		for g, addr in missing_contact:
			lines.append(f"- `{g}` / `{addr}`")

	lines.append("")
	lines.append("---")
	lines.append("<sub>Probes can be flaky on the first try; the nightly publish workflow re-probes with a 48 h grace window, so a single :x: here is not necessarily fatal. Re-push to re-run.</sub>")

	emit(lines, args.out)
	return 0

def emit(lines, dest):
	text = "\n".join(lines).rstrip() + "\n"
	if dest == "-":
		sys.stdout.write(text)
	else:
		Path(dest).write_text(text)

if __name__ == "__main__":
	sys.exit(main())
