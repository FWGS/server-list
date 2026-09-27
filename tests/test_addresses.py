#!/usr/bin/env python3
"""probe.address_rejection()"""

import ipaddress
import unittest
from harness import TestCase, load_script, stub_dns

HOSTS = {
	"good.example": ["93.184.216.34"],
	"private.example": ["192.168.50.10"],
	"split.example": ["93.184.216.34", "10.0.0.1"],
	"v6.example": ["2606:2800:220:1:248:1893:25c8:1946"],
	"v6bad.example": ["fd00::1"],
}

# (address, expected rejection substring or None if accepted)
CASES = [
	# public
	("93.184.216.34:27015", None),
	("[2606:2800:220:1:248:1893:25c8:1946]:27015", None),
	("good.example:27015", None),
	("v6.example:27015", None),

	# RFC1918
	("192.168.1.1:27015", "non-public"),
	("10.0.0.1:27015", "non-public"),
	("172.16.0.1:27015", "non-public"),
	("172.31.255.254:27015", "non-public"),
	("172.32.0.1:27015", None),

	# loopback, link-local, CGNAT, unspecified, broadcast, multicast
	("127.0.0.1:27015", "non-public"),
	("169.254.1.1:27015", "non-public"),
	("100.64.0.1:27015", "non-public"),
	("0.0.0.0:27015", "non-public"),
	("255.255.255.255:27015", "non-public"),
	("224.0.0.1:27015", "non-public"),
	("239.255.255.250:27015", "non-public"),

	# documentation and benchmark ranges
	("192.0.2.1:27015", "non-public"),
	("198.51.100.1:27015", "non-public"),
	("203.0.113.1:27015", "non-public"),
	("198.18.0.1:27015", "non-public"),

	# IPv6 loopback, link-local, ULA, and IPv4-mapped private
	("[::1]:27015", "non-public"),
	("[fe80::1]:27015", "non-public"),
	("[fc00::1]:27015", "non-public"),
	("[fd00::1]:27015", "non-public"),
	("[::ffff:192.168.1.1]:27015", "non-public"),

	# resolution
	("private.example:27015", "non-public"),
	("v6bad.example:27015", "non-public"),
	("split.example:27015", "non-public"),
	# DNS may be down, don't refuse
	("nowhere.example:27015", None),

	# malformed
	("no-port", "host:port"),
	("1.2.3.4", "host:port"),
	("1.2.3.4:0", "host:port"),
	("1.2.3.4:65536", "host:port"),
	("1.2.3.4:abc", "host:port"),
	(":27015", "host:port"),
	("", "host:port"),
]

class TestAddressRejection(TestCase):
	def setUp(self):
		super().setUp()
		self.probe = load_script("probe")

	def test_table(self):
		for address, expected in CASES:
			with self.subTest(address=address):
				self.probe.resolve_host.cache_clear()
				with stub_dns(HOSTS):
					why = self.probe.address_rejection(address)
				if expected is None:
					self.assertIsNone(why)
				else:
					self.assertIn(expected, why)

	def test_rejection_names_the_offending_ip(self):
		self.probe.resolve_host.cache_clear()
		with stub_dns(HOSTS):
			why = self.probe.address_rejection("split.example:27015")
		self.assertIn("10.0.0.1", why)
		self.assertNotIn("93.184.216.34", why)

	def test_split_hostport(self):
		cases = {
			"1.2.3.4:27015": "1.2.3.4",
			"[::1]:27015": "::1",
			"host.example:27015": "host.example",
			"no-port": None,
			"1.2.3.4:70000": None,
			None: None,
		}
		for address, expected in cases.items():
			with self.subTest(address=address):
				self.assertEqual(self.probe.split_hostport(address), expected)

	def test_multicast_is_not_public(self):
		# ipaddress thinks multicast is global
		self.assertTrue(ipaddress.ip_address("224.0.0.1").is_global)
		self.assertFalse(self.probe.is_public(ipaddress.ip_address("224.0.0.1")))

if __name__ == "__main__":
	unittest.main()
