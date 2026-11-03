#!/data/data/com.termux/files/usr/bin/bash
# Monico Labs bridge installer for Termux (Android).
# Pattern follows termux-code's install.sh: no package builds, files go
# to ~/.monico-bridge/app, launchers land in $PREFIX/bin, secrets are
# written to a local env file with 0600 perms, never echoed back.
#
#   pkg install python git
#   curl -fsSL <host>/install-termux.sh | MONICO_SERVER=http://<linux>:8765 MONICO_TOKEN=<pairing key> bash
#
# Or clone the repo and run ./install-termux.sh from inside it.
set -eu

APP_DIR="$HOME/.monico-bridge/app"
BIN_DIR="${PREFIX:-$HOME/../usr}/bin"
mkdir -p "$APP_DIR" "$BIN_DIR" "$HOME/.monico-bridge" "$HOME/monico-host"

SRC_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo "")"
if [ -f "$SRC_DIR/monico-connect" ]; then
  cp -r "$SRC_DIR/bridge" "$SRC_DIR/ui" "$APP_DIR/"
  cp "$SRC_DIR"/monico-connect "$SRC_DIR"/monico-cmd "$SRC_DIR"/monico-model "$APP_DIR/"
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
  echo "No MONICO_SERVER / MONICO_TOKEN given; run monico-connect --server <url> --key <key> once."
fi

echo
echo "Done. Turn this phone into a server with:"
echo "  monico-connect"
echo "Run a command on the Linux box from here:"
echo "  monico-cmd linux \"uname -a\""
echo "Optional, install the biggest uncensored model this phone can run:"
echo "  monico-model install"
