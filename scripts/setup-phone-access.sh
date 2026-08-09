#!/bin/bash
# One-time setup: make the drafting board reachable from your phone, for free,
# with no domain, no port forwarding and nothing exposed to the internet.
#
#   phone  --https-->  Tailscale edge  -->  this Mac  -->  127.0.0.1:8000
#
# Tailscale gives this Mac a stable hostname (<machine>.<tailnet>.ts.net) and a
# real TLS certificate for it, and only devices signed into your own tailnet can
# resolve or reach that name. The board never listens on anything but loopback,
# so there is no port on this machine for anyone else to find -- not on your
# home wifi, not on a hotel network, not from outside.
#
# This link is only needed to *install* the board and to *sync* it. Once the
# phone has loaded it once, the projection is cached on the device and the
# optimiser runs in the browser, so the board keeps working with this Mac
# asleep, shut, or on the other side of the world. Come back to the link when
# you want fresher numbers.
#
# Idempotent: run it as often as you like, every step checks before it acts.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${FPL_PORT:-8000}"
LABEL="com.matt.fplboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$ROOT/.venv/bin/python"

TS=""
for candidate in /Applications/Tailscale.app/Contents/MacOS/Tailscale \
                 /usr/local/bin/tailscale /opt/homebrew/bin/tailscale; do
  [ -x "$candidate" ] && { TS="$candidate"; break; }
done

say()  { printf "\n\033[1m%s\033[0m\n" "$*"; }
todo() { printf "\033[33m  → %s\033[0m\n" "$*"; }
ok()   { printf "\033[32m  ✓ %s\033[0m\n" "$*"; }
warn() { printf "\033[33m  ! %s\033[0m\n" "$*"; }

# --------------------------------------------------------------- 1. Tailscale
say "1. Tailscale"
if [ -z "$TS" ]; then
  todo "Not installed. Run this, then re-run this script:"
  echo
  echo "      brew install --cask tailscale-app"
  echo
  echo "  It will ask for your Mac password — it installs a system network"
  echo "  extension, which is why it cannot be done unattended."
  exit 1
fi
ok "installed at $TS"

if ! "$TS" status >/dev/null 2>&1; then
  todo "Not signed in. Opening Tailscale — sign in, then re-run this script."
  open -a Tailscale 2>/dev/null || true
  exit 1
fi
ok "signed in"

DNSNAME="$("$TS" status --json 2>/dev/null \
  | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("Self",{}).get("DNSName","").rstrip("."))')"
if [ -z "$DNSNAME" ] || [ "$DNSNAME" = "$(hostname -s)" ]; then
  todo "MagicDNS is off, so this machine has no stable name to reach it by."
  echo "      Enable MagicDNS *and* HTTPS Certificates at:"
  echo "      https://login.tailscale.com/admin/dns"
  echo "      then re-run this script."
  exit 1
fi
ok "hostname $DNSNAME"

# ----------------------------------------------------------- 2. board service
say "2. Board service"

# A board you started by hand in a terminal still owns the port, and launchd
# would spend the rest of the day restarting into "address already in use".
STALE="$(pgrep -f 'fpl.py serve' | grep -v "^$$\$" || true)"
if [ -n "$STALE" ] && ! launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
  warn "stopping a board already running outside launchd (pid $(echo $STALE | tr '\n' ' '))"
  pkill -f 'fpl.py serve' || true
  sleep 2
fi

mkdir -p "$ROOT/out"
chmod +x "$ROOT/scripts/board-agent.sh"
mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__ROOT__|$ROOT|g" "$ROOT/scripts/$LABEL.plist" > "$PLIST"

# bootout then bootstrap, so re-running picks up an edited plist instead of
# silently keeping whatever launchd loaded the first time.
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$UID" "$PLIST"
ok "launchd agent installed — starts at login, restarts on crash"

printf "  waiting for the first projection"
for _ in $(seq 1 90); do
  curl -fs -o /dev/null "http://127.0.0.1:$PORT/" 2>/dev/null && break
  printf "."; sleep 1
done
echo
if ! curl -fs -o /dev/null "http://127.0.0.1:$PORT/" 2>/dev/null; then
  todo "board did not come up — check $ROOT/out/board.log"
  exit 1
fi
ok "board answering on 127.0.0.1:$PORT"

# --------------------------------------------------------------- 3. expose it
say "3. Tailscale HTTPS"
# `serve` is tailnet-only. `funnel` is the one that publishes to the whole
# internet, and this deliberately does not use it.
if ! "$TS" serve --bg "http://127.0.0.1:$PORT" 2>/tmp/fpl-serve-err; then
  todo "tailscale serve failed:"
  sed 's/^/      /' /tmp/fpl-serve-err
  echo
  echo "  Almost always HTTPS Certificates being off for the tailnet. Enable it at:"
  echo "      https://login.tailscale.com/admin/dns"
  exit 1
fi
ok "https://$DNSNAME → 127.0.0.1:$PORT, your tailnet only"

# ---------------------------------------------------------------- 4. snapshot
say "4. Snapshot"
if [ -f "$ROOT/out/snapshot.json" ]; then
  ok "$(du -h "$ROOT/out/snapshot.json" | cut -f1) · $(date -r "$ROOT/out/snapshot.json" "+%Y-%m-%d %H:%M")"
else
  todo "none yet — the board will build one on its first request"
fi

say "Done — on your phone"
cat <<EOF
  1. Install Tailscale from the App Store and sign in with the same account.
  2. Open:  https://$DNSNAME
  3. Share -> Add to Home Screen. It launches full screen, like an app.

  Let it finish loading once. That caches the app and the projection on the
  phone, and from then on it works with this Mac asleep or shut -- picking a
  squad, editing a player and re-solving the optimiser all happen on the
  device. Press Sync when you want fresher numbers and this Mac is awake.

  Fresher numbers now:  python fpl.py snapshot --refresh
  Logs:                 tail -f $ROOT/out/board.log
  Restart:              launchctl kickstart -k gui/$UID/$LABEL
  Stop:                 launchctl bootout gui/$UID/$LABEL && $TS serve reset
EOF
