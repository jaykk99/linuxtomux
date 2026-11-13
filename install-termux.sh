#!/data/data/com.termux/files/usr/bin/bash
# Monico Labs bridge installer for Termux (Android).
# Pattern follows termux-code's install.sh: no package builds, files go
# to ~/.monico-bridge/app, launchers land in $PREFIX/bin, secrets are
# written to a local env file with 0600 perms, never echoed back.
#
# Install from the repo (the normal way):
#   git clone https://github.com/jaykk99/linuxtomux.git
#   cd linuxtomux && ./install-termux.sh
# Then connect with the 4-digit code from the Linux screen:
#   monico-connect
# The phone finds the server on the local network by itself; no IP
# to type, no command to paste. A failed download or a bad code fails
# LOUDLY (the old silent curl -f failure is gone).
#
# Fallbacks, kept working but not the main flow: a server address +
# code/token in env vars fetches the app tarball from the Linux box
# itself (no internet on the phone, same WiFi only); with no server
# at all it falls back to a git clone, which needs internet.
set -eu

# Server + pair code arrive as args from a piped install, or as
# env vars from the old long form. Never read a code from stdin: when
# piped (curl | bash) stdin is the script itself. The normal flow
# needs neither: install from the repo, then `monico-connect` asks
# for the 4-digit code itself.
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
    echo "ERROR: no pair code. Run monico-connect and type the 4-digit code from the Linux screen (monico-pair for a fresh one)." >&2
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
    echo "ERROR: bad or expired pair code. Run monico-pair on the Linux box for a fresh code (codes last 10 minutes, one use)." >&2
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
#!/usr/bin/env bash
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
# Same location the client reads (bridge/common.py DATA_DIR):
# MONICO_HOME wins when set, else ~/.monico-bridge.
cfg = Path(os.environ.get("MONICO_HOME", str(Path.home() / ".monico-bridge"))).expanduser() / "client.json"
cfg.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
data = {}
if cfg.exists():
    try: data = json.loads(cfg.read_text())
    except Exception: data = {}
data.update({"server_url": sys.argv[1], "key": sys.argv[2]})
cfg.write_text(json.dumps(data, indent=2) + "\n")
cfg.chmod(0o600)
print(f"Saved server + pairing key to {cfg} (0600).")
PY
else
  echo "No MONICO_SERVER / MONICO_TOKEN given; run monico-connect and enter the 4-digit code from the Linux screen."
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
