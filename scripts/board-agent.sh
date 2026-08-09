#!/bin/bash
# Runs the drafting board as a background service, so the phone can sync
# whenever this Mac happens to be awake. Started by
# ~/Library/LaunchAgents/com.matt.fplboard.plist at login, restarted by launchd
# if it exits.
#
# Note what this deliberately does *not* do: hold the Mac awake. It used to,
# with `caffeinate -s`, back when the phone was a thin client and a sleeping
# laptop meant a dead board. The board now runs the projection maths and the
# MILP in the browser off a cached snapshot, so the phone works with this
# machine asleep, shut or in another country. All the laptop is for is
# re-projecting, and that can wait until it is next open.
#
# The board binds loopback and has no auth of its own here. Tailscale terminates
# TLS and decides who gets through; binding to 127.0.0.1 is what guarantees that
# is the only way in, so there is no port on this machine for anything on the
# local network to find.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${FPL_PORT:-8000}"

mkdir -p "$ROOT/out"
cd "$ROOT"

exec "$ROOT/.venv/bin/python" fpl.py serve \
  --host 127.0.0.1 --port "$PORT" --no-open
