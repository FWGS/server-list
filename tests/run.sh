#!/bin/sh
# Runs the test suite; arguments are passed to unittest. See README.md.
set -eu
cd "$(dirname "$0")"
if [ "$#" -eq 0 ]; then
	exec python3 -m unittest discover -s . -t .
fi
exec python3 -m unittest "$@"
