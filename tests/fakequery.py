#!/usr/bin/env python3
"""Fake xash3d-query.

Replies come from $FAKE_QUERY_FIXTURE, a JSON object keyed by "host:port",
anything else times out. Queried addresses are appended to $FAKE_QUERY_LOG.
"""

import json
import os
import sys
import time

def parse_argv(argv):
	addresses, protocols = [], []
	i = 0
	while i < len(argv):
		arg = argv[i]
		if arg in ("-p", "--protocol"):
			protocols = [int(x) for x in argv[i + 1].split(",")]
			i += 2
			continue
		if arg.startswith("--protocol="):
			protocols = [int(x) for x in arg.split("=", 1)[1].split(",")]
			i += 1
			continue
		if arg.startswith("-p") and len(arg) > 2 and arg[2:].isdigit():
			protocols = [int(arg[2:])]
			i += 1
			continue
		if arg in ("-t", "--server-timeout", "-T", "--master-timeout",
				"-M", "--master", "-f", "--filter", "-g", "--filter-gamedir"):
			i += 2
			continue
		if arg.startswith("-"):
			i += 1
			continue
		if arg == "info":
			i += 1
			continue
		addresses.append(arg)
		i += 1
	return addresses, protocols or [49, 48]

def main():
	argv = sys.argv[1:]
	if "--version" in argv or "-v" in argv:
		print("xash3d-query v0.2.0 (fake)")
		return 0

	fixture = {}
	path = os.environ.get("FAKE_QUERY_FIXTURE")
	if path and os.path.exists(path):
		with open(path) as f:
			fixture = json.load(f)

	addresses, protocols = parse_argv(argv)

	log = os.environ.get("FAKE_QUERY_LOG")
	if log:
		with open(log, "a") as f:
			f.write(f"{','.join(str(p) for p in protocols)} {' '.join(sorted(addresses))}\n")

	servers = []
	for address in addresses:
		spec = fixture.get(address)
		if spec is None:
			servers.append({"address": address, "status": "timeout"})
			continue
		if spec.get("status") not in (None, "ok"):
			servers.append({"address": address, "status": spec["status"]})
			continue
		server = {
			"time": int(time.time()),
			"address": address,
			"ping": spec.get("ping", 10.0),
			"status": "ok",
			"gamedir": spec.get("gamedir", "cstrike"),
			"map": spec.get("map", "de_dust2"),
			"host": spec.get("host", "Test Server"),
			# real servers answer with their own protocol whatever we asked for
			"protocol": spec.get("protocol", 49),
			"numcl": spec.get("numcl", 0),
			"maxcl": spec.get("maxcl", 32),
			"dm": True, "team": False, "coop": False,
			"password": False, "dedicated": True,
		}
		if "resolved" in spec:
			server["resolved"] = spec["resolved"]
		servers.append(server)

	json.dump({
		"protocol": protocols,
		"master_timeout": 2,
		"server_timeout": 5,
		"masters": [],
		"filter": "\\clver\\0.21\\buildnum\\4000",
		"servers": servers,
	}, sys.stdout)
	sys.stdout.write("\n")
	return 0

if __name__ == "__main__":
	sys.exit(main())
