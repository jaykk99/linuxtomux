#!/data/data/com.termux/files/usr/bin/bash
# Monico Labs bridge installer for Termux (Android).
# Pattern follows termux-code's install.sh: no package builds, files go
# to ~/.monico-bridge/app, launchers land in $PREFIX/bin, secrets are
# written to a local env file with 0600 perms, never echoed back.
#
# Run it with the one command printed by `monico-server start` on the
# Linux box (real address and pair code already filled in):
#   curl -s http://<ip>:8765/i | bash -s <ip>:8765 <code>
# That command fetches this script AND the app files from the Linux box
# itself, so it works with no internet on the phone as long as it is on
# the same WiFi. The code is exchanged for the pairing key, the files
# install, and the phone connects by itself: nothing else to type or run.
# A failed download or a bad code fails LOUDLY (the old silent curl -f
# failure is gone). Without a server it falls back to a git clone, which
# needs internet.
#
# Or clone the repo and run ./install-termux.sh from inside it.
set -eu

# Server + pair code arrive as args from the printed one-liner, or as
# env vars from the old long form. Never read a code from stdin: when
# piped (curl | bash) stdin is the script itself.
SERVER="${1:-${MONICO_SERVER:-}}"
[ -n "$SERVER" ] && [ "${SERVER#http}" = "$SERVER" ] && SERVER="http://$SERVER"
MONICO_SERVER="$SERVER"
CODE="${2:-}"
TOKEN="${MONICO_TOKEN:-}"

if [ -n "$SERVER" ] && [ -z "$TOKEN" ]; then
  if [ -z "$CODE" ]; then
    if [ -e /dev/tty ]; then
      printf "Pair code from the server screen: " > /dev/tty
      read -r CODE < /dev/tty
    fi
  fi
  if [ -z "$CODE" ]; then
    echo "ERROR: no pair code. Copy the full one-line command printed by monico-server start (it ends with the 6-digit code)." >&2
    exit 1
  fi
  base="${SERVER%/}"
  [ "${base#http}" = "$base" ] && base="http://$base"
  resp="$(curl -fsSL "$base/pair?code=$CODE" 2>/dev/null)" || {
    echo "ERROR: could not reach $base. Is the server running, and is the phone on the same WiFi?" >&2
    exit 1
  }
  TOKEN="$(printf '%s' "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("key") or "")' 2>/dev/null || true)"
  if [ -z "$TOKEN" ]; then
    echo "ERROR: bad or expired pair code. Copy the fresh one-line command from the server screen (codes last 10 minutes, one use)." >&2
    exit 1
  fi
  MONICO_TOKEN="$TOKEN"
fi

APP_DIR="$HOME/.monico-bridge/app"
BIN_DIR="${PREFIX:-$HOME/../usr}/bin"
mkdir -p "$APP_DIR" "$BIN_DIR" "$HOME/.monico-bridge" "$HOME/monico-host"

SRC_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo "")"
if [ -f "$SRC_DIR/monico-connect" ]; then
  cp -r "$SRC_DIR/bridge" "$SRC_DIR/ui" "$APP_DIR/"
  cp "$SRC_DIR"/monico-connect "$SRC_DIR"/monico-cmd "$SRC_DIR"/monico-model "$APP_DIR/"
elif [ -n "${MONICO_SERVER:-}" ]; then
  # Same WiFi, no internet needed: the Linux box serves a tarball of
  # the exact code it runs at /linuxtomux.tar.gz.
  base="${MONICO_SERVER%/}"
  tmp="$(mktemp -d)"
  curl -fsSL "$base/linuxtomux.tar.gz" | tar -xz -C "$tmp" || {
    echo "ERROR: could not download $base/linuxtomux.tar.gz. Is the server still running, and is the phone on the same WiFi?" >&2
    rm -rf "$tmp"
    exit 1
  }
  cp -r "$tmp/bridge" "$tmp/ui" "$APP_DIR/"
  cp "$tmp"/monico-connect "$tmp"/monico-cmd "$tmp"/monico-model "$APP_DIR/"
  rm -rf "$tmp"
else
  REPO="${MONICO_REPO:-https://github.com/jaykk99/linuxtomux}"
  tmp="$(mktemp -d)"
  git clone --depth 1 "$REPO" "$tmp/repo"
  cp -r "$tmp/repo/bridge" "$tmp/repo/ui" "$APP_DIR/"
  cp "$tmp/repo"/monico-connect "$tmp/repo"/monico-cmd "$tmp/repo"/monico-model "$APP_DIR/"
  rm -rf "$tmp"
fi

for cmd in monico-connect monico-cmd monico-model; do
  cat > "$BIN_DIR/$cmd" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
cd "$APP_DIR" && exec python3 "$APP_DIR/$cmd" "\$@"
EOF
  chmod +x "$BIN_DIR/$cmd" "$APP_DIR/$cmd"
done

# Save the pairing details the same way termux-code saves its token:
# a local env/config file, 0600, never printed.
if [ -n "${MONICO_SERVER:-}" ] && [ -n "${MONICO_TOKEN:-}" ]; then
  python3 - "$MONICO_SERVER" "$MONICO_TOKEN" <<'PY'
import json, os, sys
from pathlib import Path
cfg = Path.home() / ".monico-bridge" / "client.json"
cfg.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
data = {}
if cfg.exists():
    try: data = json.loads(cfg.read_text())
    except Exception: data = {}
data.update({"server_url": sys.argv[1], "key": sys.argv[2]})
cfg.write_text(json.dumps(data, indent=2) + "\n")
cfg.chmod(0o600)
print("Saved server + pairing key to ~/.monico-bridge/client.json (0600).")
PY
else
  echo "No MONICO_SERVER / MONICO_TOKEN given; run monico-connect with the server URL and pairing key printed by monico-server start."
fi

echo
if [ -n "${MONICO_SERVER:-}" ] && [ -n "${MONICO_TOKEN:-}" ]; then
  echo "Installed. Connecting this phone to the server now (leave this running):"
  exec "$BIN_DIR/monico-connect"
fi
echo "Done. Turn this phone into a server with:"
echo "  monico-connect"
echo "Run a command on the Linux box from here:"
echo "  monico-cmd linux \"uname -a\""
echo "Optional, install the biggest uncensored model this phone can run:"
echo "  monico-model install"
