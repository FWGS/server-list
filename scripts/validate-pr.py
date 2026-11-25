#!/usr/bin/env python3
# Validate a PR against its base branch: find the server entries it adds or
# meaningfully changes, probe them, and check them against the contribution
# policy. Writes a Markdown report for the PR comment. Exits non-zero only for
# duplicate entries the PR introduces.

import argparse
import collections
import importlib.util
import re
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
		return contact_of(self.entry)

def contact_of(entry):
	raw = entry.get("contact")
	return raw.strip() if isinstance(raw, str) else raw

def collect_candidates(base, head):
	"""Returns (entries to probe, entries whose only change is `contact`, invalid)."""
	base_map = {(g, e["address"]): e for g, es in base.items() for e in es if e.get("address")}

	candidates, contact_edits, invalid = [], [], []
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
			elif contact_of(previous) != contact_of(entry):
				# nothing to probe, but the new contact still has to be usable
				contact_edits.append(Candidate(gamedir, entry, "`contact` changed"))
	return candidates, contact_edits, invalid

# --------------------------------------------------------------- contact

# Contributors write these by hand, so spelling is read loosely ("Discord: name",
# "tg @name", a bare t.me link). What is not negotiable is that every contact
# says which service it is on: a bare nickname leaves nobody to write to.
#
# The free and open services are listed first, because this is the list people
# are pointed at when their contact is not understood.
CONTACT_FORMS = [
	"- email, e.g. `contact = \"admin@example.com\"`",
	"- Matrix, as `matrix:@user:example.org` or a room alias",
	"- XMPP, as `xmpp:user@example.org`",
	"- Fediverse (Mastodon or anything else on ActivityPub), as `fediverse:@user@example.social`",
	"- Discord, as `discord:username` or a stable invite link",
	"- Telegram, as `telegram:@username` or a group link",
]

DOMAIN    = r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
MATRIX_ID = rf"[A-Za-z0-9._=/+-]+:{DOMAIN}(?::\d{{1,5}})?"

EMAIL_RE         = re.compile(rf"[A-Za-z0-9._%+-]+@{DOMAIN}")
MATRIX_RE        = re.compile(rf"(?:[@#]|[ur]/)?{MATRIX_ID}")
MATRIX_SIGIL_RE  = re.compile(rf"[@#]{MATRIX_ID}")
MATRIX_LINK_RE   = re.compile(rf"(?:https?://)?matrix\.to/#/(?:[@#]|%40|%23){MATRIX_ID}(?:\?\S*)?", re.I)
XMPP_RE          = re.compile(rf"[^\s@/]+@{DOMAIN}(?:/[^\s?]+)?(?:\?\S*)?")
FEDIVERSE_RE     = re.compile(rf"@?[A-Za-z0-9_.-]+@{DOMAIN}")
FEDIVERSE_AT_RE  = re.compile(rf"@[A-Za-z0-9_.-]+@{DOMAIN}")
LINK_RE          = re.compile(rf"(?:https?://)?{DOMAIN}/\S+")
DISCORD_USER_RE  = re.compile(r"@?[A-Za-z0-9_.]{2,32}(?:#\d{1,4})?")
DISCORD_LINK_RE  = re.compile(r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+/?", re.I)
TELEGRAM_USER_RE = re.compile(r"@?[A-Za-z][A-Za-z0-9_]{3,31}")
TELEGRAM_LINK_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?:\+|joinchat/)?[A-Za-z0-9_-]+/?", re.I)

# service -> (what may follow its tag, what to say when something else does)
SERVICES = {
	"email": ((EMAIL_RE,), "not a valid email address"),
	"matrix": ((MATRIX_RE, MATRIX_LINK_RE), "not a Matrix ID or room alias"),
	"xmpp": ((XMPP_RE,), "not an XMPP address"),
	"fediverse": ((FEDIVERSE_RE, LINK_RE), "not a Fediverse handle or profile link"),
	"discord": ((DISCORD_USER_RE, DISCORD_LINK_RE), "not a Discord username or invite link"),
	"telegram": ((TELEGRAM_USER_RE, TELEGRAM_LINK_RE), "not a Telegram @username or t.me link"),
}
TAGS = {
	**{service: service for service in SERVICES},
	"e-mail": "email", "mailto": "email",
	"jabber": "xmpp", "jid": "xmpp",
	"fedi": "fediverse", "activitypub": "fediverse", "mastodon": "fediverse",
	"tg": "telegram",
}
SERVICE_RE = re.compile(
	rf"({'|'.join(sorted(map(re.escape, TAGS), key=len, reverse=True))})(?:\s*:\s*|\s+)(.*)", re.I)

# forms that nothing else looks like, so they are understood without a tag
UNTAGGED = (EMAIL_RE, MATRIX_SIGIL_RE, MATRIX_LINK_RE, FEDIVERSE_AT_RE,
	DISCORD_LINK_RE, TELEGRAM_LINK_RE)

# "whatsapp: +1...", "signal: +1...": some other service, named the way ours are
LABELLED_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]*\s*:\s*\S.*")

# How well one contact is understood. Only a supported one can be used; the
# other two decide what the contributor is told.
CONTACT_UNCLEAR     = 0      # nothing says what this is or where it lives
CONTACT_UNSUPPORTED = 1  # reads as a contact, but not one we can use (yet)
CONTACT_SUPPORTED   = 2

RATING_NOTES = {
	CONTACT_UNCLEAR: "cannot tell which service this is on",
	CONTACT_UNSUPPORTED: "looks like a platform we do not support yet",
}

def rate_contact(token):
	"""Rates one contact. Returns (rating, detail).

	`detail` is set only when there is something more exact to say than the
	rating's own note, e.g. a supported service with a handle that cannot be one.
	"""
	m = SERVICE_RE.fullmatch(token)
	if m:
		forms, detail = SERVICES[TAGS[m.group(1).lower()]]
		if any(form.fullmatch(m.group(2)) for form in forms):
			return CONTACT_SUPPORTED, None
		return CONTACT_UNSUPPORTED, detail

	if any(form.fullmatch(token) for form in UNTAGGED):
		return CONTACT_SUPPORTED, None
	if MATRIX_RE.fullmatch(token):
		# user:server with no sigil is also what "skype:john.doe" looks like
		return CONTACT_UNSUPPORTED, (RATING_NOTES[CONTACT_UNSUPPORTED]
			+ "; if it is a Matrix ID, start it with `@`")
	if LABELLED_RE.fullmatch(token) or "/" in token:
		return CONTACT_UNSUPPORTED, None  # some other service, or a link to one
	if "@" in token[1:]:
		return CONTACT_UNSUPPORTED, "not a valid email address"
	return CONTACT_UNCLEAR, None

def unusable_contacts(contact):
	"""The (contact, rating, detail) of each contact in a `contact` value that
	is rated below supported."""
	if not isinstance(contact, str):
		return [(str(contact), CONTACT_UNCLEAR, "must be a quoted string")]
	tokens = [t.strip() for t in re.split(r"[,;]", contact)]
	if not any(tokens):
		return [(contact, CONTACT_UNCLEAR, "empty")]
	rated = [(t, *rate_contact(t)) for t in tokens if t]
	return [r for r in rated if r[1] < CONTACT_SUPPORTED]

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

def code_span(s, limit=60):
	# untrusted text bound for a PR comment: inside a code span nothing is a
	# mention or markup, provided it cannot close the span itself
	s = " ".join(str(s).split()).replace("`", "'")
	return f"`{s[:limit] + '…' if len(s) > limit else s}`"

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

def entry_fault(candidate, result, rejection=None):
	"""What disqualifies this entry from being merged, or None if nothing does.

	Returns (short label for the summary, note for the table).
	"""
	if rejection:
		return "not publicly routable", f":x: {rejection}; not probed — see below."

	if candidate.protocol is None:
		return "unknown protocol", ":x: unusable `protocol`, not probed — see below."

	# probe.py queries each entry on its declared protocol only, and reports
	# an answer on any other protocol as a non-response
	wrong = result.get("wrong_protocol") if result else None
	if wrong is not None:
		return "wrong protocol", (
			f":x: no answer on protocol {candidate.protocol}; it answered on {wrong} "
			"instead. Fix `protocol`, or the server, so they agree — it will not be "
			"published as-is.")

	if result is None:
		return "unreachable", ":x: no response — server unreachable; will not be published until it answers"

	gamedir = result.get("gamedir") or ""
	if gamedir != candidate.gamedir:
		return "wrong gamedir", (
			f":x: the server reports gamedir `{md_escape(gamedir)}` but this entry is in "
			f"`{candidate.gamedir}.toml`. It would be published to the wrong list — move it "
			f"to `{md_escape(gamedir)}.toml`.")

	return None, None

def result_cells(candidate, result, rejection=None):
	"""The responder, host and notes cells for one probed entry."""
	_, fault_note = entry_fault(candidate, result, rejection)

	if rejection or candidate.protocol is None:
		return "—", "—", fault_note

	wrong = result.get("wrong_protocol") if result else None
	if wrong is not None:
		return protocol_cell(wrong), "—", fault_note
	if result is None:
		return "—", "—", fault_note

	host = f"`{md_escape(result.get('host') or '')}`"
	if fault_note:  # answered, but from the wrong gamedir
		return protocol_cell(candidate.protocol), host, fault_note

	note = ":white_check_mark: answered on the declared protocol."
	resolved = result.get("resolved")
	if resolved and resolved != candidate.address:
		note += f" Resolves to `{md_escape(resolved)}`, published as IP."

	return protocol_cell(candidate.protocol), host, note

def run_probes(probe, candidates, rejections, query_bin, timeout):
	"""Probe up to MAX_PROBE_ENTRIES candidates. Returns (probing, skipped, results)."""
	probing, skipped = candidates, max(0, len(candidates) - MAX_PROBE_ENTRIES)
	if skipped:
		probing = candidates[:MAX_PROBE_ENTRIES]

	targets = [(c.address, c.protocol) for c in probing
		if c.protocol is not None and c.address not in rejections]
	results = probe.probe_all(query_bin, targets, timeout) if targets else {}
	return probing, skipped, results

def probe_lines(probing, skipped, total, results, rejections, touched):
	if not total:
		if not touched:
			return ["This PR changes no server entries, so there is nothing to probe.", ""]
		# the sources moved, but not in a way that changes what gets published
		return ["No entry needs probing — nothing changed `address` or `protocol`.", ""]

	lines = []
	if skipped:
		lines.append(f"> Probing the first {MAX_PROBE_ENTRIES} of {total} new or changed entries.")
		lines.append("")

	n = len(probing)
	lines.append(f"Probed {n} new or changed entr{'y' if n == 1 else 'ies'}.")
	lines.append("")
	lines += TABLE_HEADER
	for c in probing:
		responder, host, note = result_cells(c, results.get((c.address, c.protocol)),
			rejections.get(c.address))
		lines.append(row(f"`{c.gamedir}`", f"`{c.address}`", md_escape(c.change),
			protocol_cell(c.protocol, c.entry.get("protocol")), responder, host, note))

	if skipped:
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

def missing_contact_lines(checked):
	missing = [c for c in checked if not c.contact]
	if not missing:
		return []
	return [
		"",
		"### :x: Missing `contact`",
		"Per the [contribution policy](../blob/main/README.md#contribution-policy), new entries must include a `contact` field so maintainers can reach the operator. Please add one of:",
		*CONTACT_FORMS,
		"",
		"Affected entries:",
		*(f"- `{c.gamedir}` / `{c.address}`" for c in missing),
	]

def unrecognized_contact_lines(checked):
	bad = [(c, token, detail or RATING_NOTES[rating]) for c in checked if c.contact
		for token, rating, detail in unusable_contacts(c.contact)]
	if not bad:
		return []
	return [
		"",
		"### :x: Unrecognized `contact`",
		"Maintainers have to be able to tell where to write to, so per the [contribution policy](../blob/main/README.md#contribution-policy) every contact must be one of:",
		*CONTACT_FORMS,
		"",
		"Several contacts are fine, separated by commas. Affected entries:",
		*(f"- `{c.gamedir}` / `{c.address}`: {code_span(token)} — {why}" for c, token, why in bad),
		"",
		"Where you have the choice, we would rather reach you on a free and open service: email, Matrix, XMPP or the Fediverse. We are also open to supporting more platforms: if yours is missing from the list, tell us in this PR which one it is. Until it is supported, please also give one of the forms above.",
	]

def verdict_lines(head_errs, dupes, invalid, probing, results, rejections, checked):
	"""One line at the top saying whether this can be merged, and if not, why.

	The table below it has the detail; this exists so the decision can be made
	from the notification e-mail without opening anything.
	"""
	faults = collections.Counter()
	if head_errs:
		faults["TOML parse error"] += len(head_errs)
	if dupes:
		faults["duplicate entry"] += len(dupes.within) + len(dupes.across)
	if invalid:
		faults["entry without an address"] += len(invalid)

	for c in probing:
		label, _ = entry_fault(c, results.get((c.address, c.protocol)), rejections.get(c.address))
		if label:
			faults[label] += 1
	missing = sum(1 for c in checked if not c.contact)
	if missing:
		faults["missing `contact`"] += missing
	unrecognized = sum(1 for c in checked if c.contact and unusable_contacts(c.contact))
	if unrecognized:
		faults["unrecognized `contact`"] += unrecognized

	if not faults:
		n = len(probing)
		if not n:
			return [":white_check_mark: **Nothing to verify** — no entry in this PR needs probing.", ""]
		return [f":white_check_mark: **Ready to merge** — {n} entr{'y' if n == 1 else 'ies'} verified, no problems found.", ""]

	total = sum(faults.values())
	detail = ", ".join(f"{n} {label}{'' if n == 1 else 's'}" if not label.endswith("`")
		else f"{n} {label}" for label, n in faults.most_common())
	return [f":x: **Not ready** — {total} problem{'' if total == 1 else 's'}: {detail}.", ""]

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
	candidates, contact_edits, invalid = collect_candidates(base, head)
	# the contact rules cover everything the PR adds, plus any contact it rewrites
	contact_checked = candidates + contact_edits

	probe = import_probe(args.probe_script)
	rejections = {c.address: why for c in candidates
		if (why := probe.address_rejection(c.address))}
	probing, skipped, results = run_probes(
		probe, candidates, rejections, args.query, args.timeout)

	lines = [COMMENT_MARKER, "## Server list PR validation", ""]
	lines += verdict_lines(head_errs, head_dupes, invalid, probing, results,
		rejections, contact_checked)
	lines += parse_error_lines(head_errs)
	lines += duplicate_lines(head_dupes, base_dupes)
	lines += probe_lines(probing, skipped, len(candidates), results, rejections,
		touched=base != head)
	lines += invalid_lines(invalid)
	lines += non_public_lines(rejections)
	lines += bad_protocol_lines(candidates)
	lines += missing_contact_lines(contact_checked)
	lines += unrecognized_contact_lines(contact_checked)
	lines += ["", "---", FOOTER]

	emit(lines, args.out)
	return 1 if introduces_duplicates(head_dupes, base_dupes) else 0

if __name__ == "__main__":
	sys.exit(main())
