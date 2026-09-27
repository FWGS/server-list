#!/usr/bin/env python3
"""scripts/validate-pr.py"""

import unittest
from harness import TestCase, git, make_repo, run_validator

LIVE = {"protocol": 49, "host": "Test Server", "gamedir": "cstrike", "numcl": 1}
LIVE48 = {"protocol": 48, "host": "GoldSrc", "gamedir": "cstrike", "numcl": 1}

def entry(address, **kw):
	e = {"address": address, "protocol": 49, "contact": "admin@example.invalid"}
	e.update(kw)
	return e

class ValidatorCase(TestCase):
	def validate(self, base_snapshot, head_snapshot, **kw):
		repo = self.tmp / "repo"
		base, head = make_repo(repo, [base_snapshot, head_snapshot])
		return run_validator(repo, base, head, tmp=self.tmp, **kw)

class TestChangeDetection(ValidatorCase):
	def test_new_entry_is_probed(self):
		r = self.validate(
			{"cstrike": [entry("1.1.1.1:27015")]},
			{"cstrike": [entry("1.1.1.1:27015"), entry("2.2.2.2:27015")]},
			fixture={"2.2.2.2:27015": LIVE})
		self.assertIn("Probed 1 new or changed entry", r.stdout)
		self.assertIn("2.2.2.2:27015", r.stdout)
		self.assertNotIn("1.1.1.1:27015", r.stdout)

	def test_protocol_only_change_is_probed(self):
		r = self.validate(
			{"cstrike": [entry("1.1.1.1:27015", protocol=49)]},
			{"cstrike": [entry("1.1.1.1:27015", protocol=48)]},
			fixture={"1.1.1.1:27015": LIVE48})
		self.assertIn("Probed 1 new or changed entry", r.stdout)
		self.assertIn("`protocol` 49 → 48", r.stdout)

	def test_address_change_is_probed(self):
		r = self.validate(
			{"cstrike": [entry("1.1.1.1:27015")]},
			{"cstrike": [entry("3.3.3.3:27015")]},
			fixture={"3.3.3.3:27015": LIVE})
		self.assertIn("3.3.3.3:27015", r.stdout)

	def test_cosmetic_change_is_not_probed(self):
		r = self.validate(
			{"cstrike": [entry("1.1.1.1:27015", host="Old name")]},
			{"cstrike": [entry("1.1.1.1:27015", host="New name")]})
		self.assertIn("No entry needs probing", r.stdout)
		self.assertIn("Nothing to verify", r.stdout)
		self.assertEqual(r.code, 0)

	def test_explicit_default_protocol_is_not_a_change(self):
		r = self.validate(
			{"cstrike": [{"address": "1.1.1.1:27015", "contact": "a@b.invalid"}]},
			{"cstrike": [{"address": "1.1.1.1:27015", "contact": "a@b.invalid", "protocol": 49}]})
		self.assertIn("No entry needs probing", r.stdout)

class TestProbeReporting(ValidatorCase):
	def test_matching_protocol_passes(self):
		r = self.validate({"cstrike": []}, {"cstrike": [entry("1.1.1.1:27015")]},
			fixture={"1.1.1.1:27015": LIVE})
		self.assertIn("answered on the declared protocol", r.stdout)

	def test_wrong_protocol_is_reported_as_no_answer(self):
		r = self.validate({"cstrike": []},
			{"cstrike": [entry("1.1.1.1:27015", protocol=48)]},
			fixture={"1.1.1.1:27015": LIVE})
		self.assertIn("no answer on protocol 48", r.stdout)
		self.assertIn("answered on 49", r.stdout)

	def test_unreachable_is_reported(self):
		r = self.validate({"cstrike": []}, {"cstrike": [entry("1.1.1.1:27015")]}, fixture={})
		self.assertIn("no response", r.stdout)

	def test_responder_gamedir_mismatch_is_a_fault(self):
		r = self.validate({"valve": []}, {"valve": [entry("1.1.1.1:27015")]},
			fixture={"1.1.1.1:27015": dict(LIVE, gamedir="cstrike")})
		self.assertIn("reports gamedir `cstrike`", r.stdout)
		self.assertIn("move it to `cstrike.toml`", r.stdout)
		self.assertIn("1 wrong gamedir", r.stdout)

	def test_resolved_domain_is_noted(self):
		r = self.validate({"tfc": []}, {"tfc": [entry("srv.example:27018")]},
			fixture={"srv.example:27018": dict(LIVE, gamedir="tfc",
				resolved="93.184.216.34:27018")},
			hosts={"srv.example": ["93.184.216.34"]})
		self.assertIn("Resolves to", r.stdout)
		self.assertIn("Ready to merge", r.stdout)

class TestDuplicates(ValidatorCase):
	def test_duplicate_in_one_file_fails(self):
		r = self.validate({"cstrike": []},
			{"cstrike": [entry("1.1.1.1:27015"), entry("1.1.1.1:27015")]},
			fixture={"1.1.1.1:27015": LIVE})
		self.assertIn("Duplicate entries", r.stdout)
		self.assertIn("appears 2 times", r.stdout)
		self.assertEqual(r.code, 1)

	def test_same_address_across_gamedirs_fails(self):
		r = self.validate({"cstrike": [], "valve": []},
			{"cstrike": [entry("1.1.1.1:27015")], "valve": [entry("1.1.1.1:27015")]},
			fixture={"1.1.1.1:27015": LIVE})
		self.assertIn("several gamedirs", r.stdout)
		self.assertEqual(r.code, 1)

	def test_duplicate_without_new_entries_still_fails(self):
		r = self.validate(
			{"cstrike": [entry("1.1.1.1:27015")]},
			{"cstrike": [entry("1.1.1.1:27015"), entry("1.1.1.1:27015")]})
		self.assertIn("Duplicate entries", r.stdout)
		self.assertEqual(r.code, 1)

	def test_preexisting_duplicate_does_not_fail_the_pr(self):
		dup = {"cstrike": [entry("1.1.1.1:27015"), entry("1.1.1.1:27015")]}
		head = {"cstrike": dup["cstrike"] + [entry("2.2.2.2:27015")]}
		r = self.validate(dup, head, fixture={"2.2.2.2:27015": LIVE})
		self.assertIn("already on the base branch", r.stdout)
		self.assertEqual(r.code, 0)

	def test_clean_pr_exits_zero(self):
		r = self.validate({"cstrike": []}, {"cstrike": [entry("1.1.1.1:27015")]},
			fixture={"1.1.1.1:27015": LIVE})
		self.assertNotIn("Duplicate", r.stdout)
		self.assertEqual(r.code, 0)

class TestPolicy(ValidatorCase):
	def test_missing_contact_is_flagged(self):
		r = self.validate({"cstrike": []},
			{"cstrike": [{"address": "1.1.1.1:27015", "protocol": 49}]},
			fixture={"1.1.1.1:27015": LIVE})
		self.assertIn("Missing `contact`", r.stdout)

	def test_blank_contact_is_flagged(self):
		r = self.validate({"cstrike": []},
			{"cstrike": [entry("1.1.1.1:27015", contact="   ")]},
			fixture={"1.1.1.1:27015": LIVE})
		self.assertIn("Missing `contact`", r.stdout)

	def test_unknown_protocol_is_flagged_and_not_probed(self):
		log = self.tmp / "queried.log"
		r = self.validate({"cstrike": []},
			{"cstrike": [entry("1.1.1.1:27015", protocol=47)]}, query_log=log)
		self.assertIn("Unknown protocol", r.stdout)
		asked = log.read_text() if log.exists() else ""
		self.assertNotIn("1.1.1.1", asked)

	def test_missing_address_is_flagged(self):
		r = self.validate({"cstrike": []}, {"cstrike": [{"protocol": 49}]})
		self.assertIn("Invalid entries", r.stdout)
		self.assertIn("no `address` field", r.stdout)

	def test_private_address_is_flagged_and_not_probed(self):
		log = self.tmp / "queried.log"
		r = self.validate({"cstrike": []},
			{"cstrike": [entry("192.168.1.1:27015")]}, query_log=log)
		self.assertIn("not publicly routable", r.stdout)
		asked = log.read_text() if log.exists() else ""
		self.assertNotIn("192.168.1.1", asked)

	def test_parse_error_is_reported(self):
		repo = self.tmp / "repo"
		base, head = make_repo(repo, [{"cstrike": [entry("1.1.1.1:27015")]}] * 2)
		(repo / "servers" / "cstrike.toml").write_text("[[server]\nbroken =\n")
		git(repo, "add", "-A")
		git(repo, "commit", "-q", "-m", "break it")
		head = git(repo, "rev-parse", "HEAD")
		r = run_validator(repo, base, head, tmp=self.tmp)
		self.assertIn("TOML parse errors", r.stdout)

class TestVerdict(ValidatorCase):
	def test_clean_pr_says_ready_to_merge(self):
		r = self.validate({"cstrike": []},
			{"cstrike": [entry("1.1.1.1:27015"), entry("2.2.2.2:27015")]},
			fixture={"1.1.1.1:27015": LIVE, "2.2.2.2:27015": LIVE})
		self.assertIn(":white_check_mark: **Ready to merge** — 2 entries verified", r.stdout)

	def test_verdict_is_the_first_line_after_the_heading(self):
		r = self.validate({"cstrike": []}, {"cstrike": [entry("1.1.1.1:27015")]},
			fixture={"1.1.1.1:27015": LIVE})
		body = [l for l in r.stdout.splitlines() if l.strip()]
		self.assertIn("Ready to merge", body[2])

	def test_problems_are_counted_and_named(self):
		r = self.validate({"cstrike": []}, {"cstrike": [
			entry("1.1.1.1:27015"),                     # unreachable
			entry("2.2.2.2:27015", contact=""),         # unreachable + no contact
			entry("192.168.1.1:27015"),                 # not routable
		]}, fixture={})
		self.assertIn(":x: **Not ready**", r.stdout)
		self.assertIn("2 unreachables", r.stdout)
		self.assertIn("1 not publicly routable", r.stdout)
		self.assertIn("1 missing `contact`", r.stdout)

	def test_empty_pr_says_so(self):
		r = self.validate({"cstrike": [entry("1.1.1.1:27015")]},
			{"cstrike": [entry("1.1.1.1:27015")]})
		self.assertIn("changes no server entries", r.stdout)
		self.assertIn("Nothing to verify", r.stdout)

	def test_duplicate_shows_in_the_verdict(self):
		r = self.validate({"cstrike": []},
			{"cstrike": [entry("1.1.1.1:27015"), entry("1.1.1.1:27015")]},
			fixture={"1.1.1.1:27015": LIVE})
		self.assertIn("1 duplicate entry", r.stdout)

class TestTruncation(ValidatorCase):
	def test_large_pr_is_capped_but_policy_still_applies(self):
		many = [{"address": f"1.1.1.{i}:27015", "protocol": 49} for i in range(1, 61)]
		r = self.validate({"cstrike": []}, {"cstrike": many}, fixture={})
		self.assertIn("Probing the first 50 of 60", r.stdout)
		self.assertIn("Skipped probing 10 additional entries", r.stdout)
		# contact check still covers unprobed entries
		self.assertIn("1.1.1.60:27015", r.stdout)

if __name__ == "__main__":
	unittest.main()
