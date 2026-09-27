#!/bin/sh
#
# Triggers the publish workflow, since GitHub's scheduled cron skips most runs.
# Once an hour the run goes to Blacksmith, which reaches servers GitHub can't.
#
# Usage: scripts/dispatch.sh --install  (as the user logged in with `gh`;
# re-run whenever the checkout moves)
set -eu

UNIT=server-list-dispatch

install_units() {
	self="$(cd "$(dirname "$0")" && pwd -P)/$(basename "$0")"
	dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

	if ! gh auth status >/dev/null 2>&1; then
		echo "gh is not logged in, run \`gh auth login\` first" >&2
		exit 1
	fi

	mkdir -p "$dir"
	# PATH is captured so the unit finds the same gh this shell does
	cat > "$dir/$UNIT.service" <<-EOF
		[Unit]
		Description=Dispatch the server-list publish workflow
		Wants=network-online.target
		After=network-online.target

		[Service]
		Type=oneshot
		Environment=PATH=$PATH
		ExecStart=$self
	EOF
	cat > "$dir/$UNIT.timer" <<-EOF
		[Unit]
		Description=Run server-list publish dispatch about every 10 minutes

		[Timer]
		OnBootSec=1min
		OnUnitActiveSec=10min
		RandomizedDelaySec=3min

		[Install]
		WantedBy=timers.target
	EOF

	systemctl --user daemon-reload
	systemctl --user enable --now "$UNIT.timer"
	echo "installed $dir/$UNIT.{service,timer} -> $self"

	# without lingering, user timers stop when the last session ends
	if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" != yes ]; then
		echo "note: run \`loginctl enable-linger $(id -un)\` so the timer runs without a login" >&2
	fi
}

if [ "${1:-}" = --install ]; then
	install_units
	exit
fi

REPO="${REPO:-FWGS/server-list}"
DEFAULT_RUNNER="${DEFAULT_RUNNER:-ubuntu-slim}"
BLACKSMITH_RUNNER="${BLACKSMITH_RUNNER:-blacksmith-2vcpu-ubuntu-2404-arm}"
# seconds between Blacksmith runs; must stay well inside probe.py --grace-hours
BLACKSMITH_INTERVAL="${BLACKSMITH_INTERVAL:-3600}"
STAMP="${STAMP:-${XDG_STATE_HOME:-$HOME/.local/state}/$UNIT.blacksmith}"

now=$(date +%s)

last=0
if [ -r "$STAMP" ]; then
	last=$(cat "$STAMP" 2>/dev/null) || last=0
fi
# a truncated or hand-edited stamp must not wedge the rotation
case "$last" in
	'' | *[!0-9]*) last=0 ;;
esac

runner="$DEFAULT_RUNNER"
if [ "$((now - last))" -ge "$BLACKSMITH_INTERVAL" ]; then
	runner="$BLACKSMITH_RUNNER"
fi

echo "$(date -Is) dispatching publish.yml on $runner"

if ! gh workflow run publish.yml --repo "$REPO" --ref main --field runner="$runner"; then
	echo "$(date -Is) dispatch failed" >&2
	exit 1
fi

# only after a dispatch actually succeeded, so a failed Blacksmith run is
# retried on the next tick instead of waiting out the whole interval
if [ "$runner" = "$BLACKSMITH_RUNNER" ]; then
	mkdir -p "$(dirname "$STAMP")"
	echo "$now" > "$STAMP"
fi
