#!/usr/bin/env python3
"""scripts/probe.py"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from harness import TestCase, run_probe

def hours_ago(n):
	return (datetime.now(timezone.utc) - timedelta(hours=n)).replace(microsecond=0).isoformat()

LIVE_XASH = {"protocol": 49, "host": "Xash Server", "gamedir": "cstrike", "numcl": 4}
LIVE_GOLDSRC = {"protocol": 48, "host": "GoldSrc Server", "gamedir": "cstrike", "numcl": 2}

class TestPublishing(TestCase):
	def test_live_server_is_published_with_declared_protocol(self):
		r = run_probe(self.tmp,
			{"cstrike": [{"address": "1.1.1.1:27015", "protocol": 49},
				{"address": "2.2.2.2:27015", "protocol": 48}]},
			fixture={"1.1.1.1:27015": LIVE_XASH, "2.2.2.2:27015": LIVE_GOLDSRC})
		self.assertEqual(r.code, 0)
		self.assertEqual(r.published("cstrike"), ["ip 1.1.1.1:27015", "gs 2.2.2.2:27015"])

	def test_silent_server_is_not_published(self):
		r = run_probe(self.tmp, {"cstrike": [{"address": "1.1.1.1:27015", "protocol": 49}]},
			fixture={})
		self.assertEqual(r.published("cstrike"), [])
		self.assertLineIn("silent", r.status_lines())

	def test_answer_on_other_protocol_counts_as_dead(self):
		r = run_probe(self.tmp, {"cstrike": [{"address": "1.1.1.1:27015", "protocol": 48}]},
			fixture={"1.1.1.1:27015": LIVE_XASH})
		self.assertEqual(r.published("cstrike"), [])
		self.assertLineIn("answers protocol 49, not 48", r.status_lines())

	def test_domain_is_published_as_resolved_ip(self):
		r = run_probe(self.tmp, {"tfc": [{"address": "srv.example:27018", "protocol": 49}]},
			fixture={"srv.example:27018": dict(LIVE_XASH, resolved="93.184.216.34:27018")},
			hosts={"srv.example": ["93.184.216.34"]})
		self.assertEqual(r.published("tfc"), ["ip 93.184.216.34:27018"])
		self.assertLineIn("srv.example:27018 -> 93.184.216.34:27018", r.status_lines())

	def test_empty_file_emitted_when_nothing_answers(self):
		r = run_probe(self.tmp, {"dmc": [{"address": "1.1.1.1:27015", "protocol": 49}]},
			fixture={})
		# file must exist even if empty
		self.assertEqual(r.published("dmc"), [])

	def test_gamedirs_counts_total_and_live(self):
		r = run_probe(self.tmp,
			{"cstrike": [{"address": "1.1.1.1:27015", "protocol": 49},
				{"address": "2.2.2.2:27015", "protocol": 49}]},
			fixture={"1.1.1.1:27015": LIVE_XASH})
		self.assertEqual(r.gamedirs(), [["cstrike", "2", "1"]])

class TestGrace(TestCase):
	def test_grace_keeps_a_recently_seen_server(self):
		r = run_probe(self.tmp, {"cstrike": [{"address": "1.1.1.1:27015", "protocol": 49}]},
			fixture={}, state={"cstrike": {"1.1.1.1:27015": {"last_seen": hours_ago(2)}}})
		self.assertEqual(r.published("cstrike"), ["ip 1.1.1.1:27015"])
		self.assertLineIn("(grace)", r.status_lines())

	def test_grace_expires(self):
		r = run_probe(self.tmp, {"cstrike": [{"address": "1.1.1.1:27015", "protocol": 49}]},
			fixture={}, state={"cstrike": {"1.1.1.1:27015": {"last_seen": hours_ago(100)}}})
		self.assertEqual(r.published("cstrike"), [])

	def test_grace_cannot_publish_a_private_address(self):
		r = run_probe(self.tmp, {"cstrike": [{"address": "10.0.0.1:27015", "protocol": 49}]},
			fixture={}, state={"cstrike": {"10.0.0.1:27015": {"last_seen": hours_ago(1)}}})
		self.assertEqual(r.published("cstrike"), [])

class TestAddressSafety(TestCase):
	def test_private_address_is_never_queried(self):
		log = self.tmp / "queried.log"
		run_probe(self.tmp,
			{"cstrike": [{"address": "192.168.1.1:27015", "protocol": 49},
				{"address": "1.1.1.1:27015", "protocol": 49}]},
			fixture={"1.1.1.1:27015": LIVE_XASH}, query_log=log)
		asked = log.read_text() if log.exists() else ""
		self.assertNotIn("192.168.1.1", asked)
		self.assertIn("1.1.1.1", asked)

	def test_rebinding_after_target_selection_is_caught(self):
		# public at target selection, private by the time it's queried
		r = run_probe(self.tmp, {"cstrike": [{"address": "rebind.example:27015", "protocol": 49}]},
			fixture={"rebind.example:27015": dict(LIVE_XASH, resolved="192.168.1.50:27015")},
			hosts={"rebind.example": ["93.184.216.34"]})
		self.assertEqual(r.published("cstrike"), [])
		self.assertLineIn("not published", r.status_lines())
		self.assertLineIn("192.168.1.50", r.status_lines())

	def test_invalid_protocol_is_skipped_not_fatal(self):
		r = run_probe(self.tmp,
			{"cstrike": [{"address": "1.1.1.1:27015", "protocol": "garbage"},
				{"address": "2.2.2.2:27015", "protocol": 49}]},
			fixture={"2.2.2.2:27015": LIVE_XASH})
		self.assertEqual(r.code, 0)
		self.assertEqual(r.published("cstrike"), ["ip 2.2.2.2:27015"])
		self.assertLineIn("invalid protocol", r.status_lines())

	def test_entry_without_address_is_ignored(self):
		r = run_probe(self.tmp, {"cstrike": [{"protocol": 49}]}, fixture={})
		self.assertEqual(r.code, 0)
		self.assertEqual(r.published("cstrike"), [])

class TestState(TestCase):
	def test_last_seen_and_ping_recorded(self):
		r = run_probe(self.tmp, {"cstrike": [{"address": "1.1.1.1:27015", "protocol": 49}]},
			fixture={"1.1.1.1:27015": dict(LIVE_XASH, ping=42.5)})
		entry = r.state()["cstrike"]["1.1.1.1:27015"]
		self.assertEqual(entry["last_ping_ms"], 42.5)
		self.assertIn("last_seen", entry)
		self.assertIn("last_attempt", entry)

	def test_silent_updates_attempt_but_not_seen(self):
		r = run_probe(self.tmp, {"cstrike": [{"address": "1.1.1.1:27015", "protocol": 49}]},
			fixture={})
		entry = r.state()["cstrike"]["1.1.1.1:27015"]
		self.assertIn("last_attempt", entry)
		self.assertNotIn("last_seen", entry)

	def test_departed_address_pruned_after_a_month(self):
		r = run_probe(self.tmp, {"cstrike": [{"address": "1.1.1.1:27015", "protocol": 49}]},
			fixture={"1.1.1.1:27015": LIVE_XASH},
			state={"cstrike": {
				"1.1.1.1:27015": {"last_seen": hours_ago(1)},
				"gone.example:27015": {"last_seen": hours_ago(24 * 40)},
				"recent.example:27015": {"last_seen": hours_ago(24 * 5)},
			}})
		cstrike = r.state()["cstrike"]
		self.assertNotIn("gone.example:27015", cstrike)
		self.assertIn("recent.example:27015", cstrike)

	def test_samples_recorded_and_trimmed(self):
		old = int((datetime.now(timezone.utc) - timedelta(days=60)).timestamp())
		r = run_probe(self.tmp, {"cstrike": [{"address": "1.1.1.1:27015", "protocol": 49}]},
			fixture={"1.1.1.1:27015": dict(LIVE_XASH, numcl=7)},
			state={"samples": {"cstrike": [[old, 3]]}})
		samples = r.state()["samples"]["cstrike"]
		self.assertNotIn(old, [s[0] for s in samples])
		self.assertEqual(samples[-1][1], 7)

	def test_no_samples_flag_keeps_history_but_adds_nothing(self):
		# used on runners that can't reach every server
		seed = {"samples": {"valve": [[1789000000, 4]]}}
		r = run_probe(self.tmp, {"valve": [{"address": "1.1.1.1:27015", "protocol": 49}]},
			fixture={"1.1.1.1:27015": dict(LIVE_XASH, gamedir="valve", numcl=9)},
			state=seed, extra_args=["--no-samples"])
		self.assertEqual(r.state()["samples"]["valve"], [[1789000000, 4]])
		self.assertEqual(r.published("valve"), ["ip 1.1.1.1:27015"])

	def test_samples_recorded_without_the_flag(self):
		seed = {"samples": {"valve": [[1789000000, 4]]}}
		r = run_probe(self.tmp, {"valve": [{"address": "1.1.1.1:27015", "protocol": 49}]},
			fixture={"1.1.1.1:27015": dict(LIVE_XASH, gamedir="valve", numcl=9)},
			state=seed)
		self.assertEqual([v for _, v in r.state()["samples"]["valve"]], [4, 9])

	def test_grace_server_contributes_no_players(self):
		r = run_probe(self.tmp,
			{"valve": [{"address": "1.1.1.1:27015", "protocol": 49},
				{"address": "2.2.2.2:27015", "protocol": 49}]},
			fixture={"2.2.2.2:27015": dict(LIVE_XASH, gamedir="valve", numcl=5)},
			state={"valve": {"1.1.1.1:27015": {"last_seen": hours_ago(1)}}})
		self.assertEqual(r.published("valve"), ["ip 1.1.1.1:27015", "ip 2.2.2.2:27015"])
		self.assertEqual([v for _, v in r.state()["samples"]["valve"]], [5])

	def test_no_sample_when_nothing_responded(self):
		r = run_probe(self.tmp, {"cstrike": [{"address": "1.1.1.1:27015", "protocol": 49}]},
			fixture={})
		self.assertNotIn("cstrike", r.state().get("samples", {}))

if __name__ == "__main__":
	unittest.main()
