#!/usr/bin/env python3
"""Runs scripts/ in-process with xash3d-query and DNS stubbed out."""

import contextlib
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FAKE_QUERY = Path(__file__).resolve().parent / "fakequery.py"

def load_script(name):
	path = SCRIPTS / f"{name}.py"
	spec = importlib.util.spec_from_file_location(f"_uut_{name.replace('-', '_')}", path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module

def fake_getaddrinfo(hosts):
	def getaddrinfo(host, port, *args, **kwargs):
		if host not in hosts:
			raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
		infos = []
		for ip in hosts[host]:
			family = socket.AF_INET6 if ":" in ip else socket.AF_INET
			sockaddr = (ip, port or 0, 0, 0) if family == socket.AF_INET6 else (ip, port or 0)
			infos.append((family, socket.SOCK_DGRAM, socket.IPPROTO_UDP, "", sockaddr))
		return infos
	return getaddrinfo

@contextlib.contextmanager
def stub_dns(hosts):
	with mock.patch("socket.getaddrinfo", fake_getaddrinfo(hosts or {})):
		yield

@contextlib.contextmanager
def chdir(path):
	old = os.getcwd()
	os.chdir(path)
	try:
		yield
	finally:
		os.chdir(old)

def write_sources(directory, gamedirs):
	directory = Path(directory)
	directory.mkdir(parents=True, exist_ok=True)
	for gamedir, entries in gamedirs.items():
		lines = []
		for entry in entries:
			lines.append("[[server]]")
			for key, value in entry.items():
				lines.append(f"{key} = {toml_value(value)}")
			lines.append("")
		(directory / f"{gamedir}.toml").write_text("\n".join(lines))
	return directory

def toml_value(value):
	if isinstance(value, bool):
		return "true" if value else "false"
	if isinstance(value, (int, float)):
		return str(value)
	return json.dumps(str(value))

def write_fixture(directory, fixture):
	path = Path(directory) / "query-fixture.json"
	path.write_text(json.dumps(fixture or {}))
	return path

def git(repo, *args, **kwargs):
	return subprocess.run(["git", "-C", str(repo), *args],
		capture_output=True, text=True, check=True, **kwargs).stdout.strip()

def make_repo(path, commits):
	# one commit per {gamedir: [entries]} snapshot, returns their shas
	path = Path(path)
	path.mkdir(parents=True, exist_ok=True)
	git(path, "init", "-q", "-b", "main")
	git(path, "config", "user.email", "test@example.invalid")
	git(path, "config", "user.name", "Test")

	shas = []
	for snapshot in commits:
		servers = path / "servers"
		if servers.exists():
			for stale in servers.glob("*.toml"):
				stale.unlink()
		write_sources(servers, snapshot)
		git(path, "add", "-A")
		git(path, "commit", "-q", "--allow-empty", "-m", f"snapshot {len(shas)}")
		shas.append(git(path, "rev-parse", "HEAD"))
	return shas

class Result:
	def __init__(self, code, stdout, out_dir=None):
		self.code = code
		self.stdout = stdout
		self.out_dir = Path(out_dir) if out_dir else None

	def published(self, gamedir):
		path = self.out_dir / "v1" / "servers" / gamedir
		if not path.exists():
			return None
		return [l for l in path.read_text().splitlines() if l and not l.startswith("#")]

	def gamedirs(self):
		path = self.out_dir / "v1" / "gamedirs"
		return [l.split() for l in path.read_text().splitlines()
			if l and not l.startswith("#")]

	def state(self):
		return json.loads((self.out_dir / "state.json").read_text())

	def status_lines(self):
		# [+]/[-]/[~]/[!] lines with padding collapsed
		out = []
		for line in self.stdout.splitlines():
			stripped = line.strip()
			if stripped[:1] == "[" and stripped[2:3] == "]":
				out.append(" ".join(stripped.split()))
		return out

def run_probe(tmp, sources, fixture=None, state=None, grace_hours=48.0, hosts=None,
		query_log=None, extra_args=()):
	probe = load_script("probe")
	sources_dir = write_sources(Path(tmp) / "servers", sources)
	out_dir = Path(tmp) / "out"
	out_dir.mkdir(exist_ok=True)
	(out_dir / "state.json").write_text(json.dumps(state if state is not None else {}))

	env = {"FAKE_QUERY_FIXTURE": str(write_fixture(tmp, fixture))}
	if query_log:
		env["FAKE_QUERY_LOG"] = str(query_log)

	argv = ["probe.py", "--query", str(FAKE_QUERY), "--sources", str(sources_dir),
		"--output", str(out_dir), "--grace-hours", str(grace_hours), *extra_args]

	buf = io.StringIO()
	with mock.patch.dict(os.environ, env), stub_dns(hosts), \
			mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(buf):
		code = probe.main()
	return Result(code, buf.getvalue(), out_dir)

def run_validator(repo, base, head, fixture=None, hosts=None, tmp=None, query_log=None):
	validator = load_script("validate-pr")
	tmp = Path(tmp or repo)

	env = {"FAKE_QUERY_FIXTURE": str(write_fixture(tmp, fixture))}
	if query_log:
		env["FAKE_QUERY_LOG"] = str(query_log)

	argv = ["validate-pr.py", "--base-ref", base, "--head-ref", head,
		"--query", str(FAKE_QUERY), "--sources", "servers",
		"--probe-script", str(SCRIPTS / "probe.py"), "--timeout", "1", "--out", "-"]

	buf = io.StringIO()
	with mock.patch.dict(os.environ, env), stub_dns(hosts), chdir(repo), \
			mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(buf):
		code = validator.main()
	return Result(code, buf.getvalue())

class TestCase(unittest.TestCase):
	def setUp(self):
		self._tmp = tempfile.TemporaryDirectory()
		self.tmp = Path(self._tmp.name)
		self.addCleanup(self._tmp.cleanup)

	def assertLineIn(self, needle, lines, msg=None):
		if not any(needle in l for l in lines):
			formatted = "\n  ".join(lines) or "(nothing)"
			self.fail(msg or f"no line containing {needle!r} in:\n  {formatted}")

	def assertNoLineIn(self, needle, lines, msg=None):
		for line in lines:
			if needle in line:
				self.fail(msg or f"unexpected line containing {needle!r}: {line}")
