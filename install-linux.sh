#!/usr/bin/env bash
# Monico Labs bridge installer for the Linux side.
#   ./install-linux.sh
# Copies the app to ~/.monico-bridge/app and puts launchers in ~/.local/bin.
set -eu
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/.monico-bridge/app"
BIN_DIR="$HOME/.local/bin"
mkdir -p "$APP_DIR" "$BIN_DIR" "$HOME/.monico-bridge"
cp -r "$SRC_DIR/bridge" "$SRC_DIR/ui" "$APP_DIR/"
cp "$SRC_DIR"/monico-server "$SRC_DIR"/monico-cmd "$SRC_DIR"/monico-model "$SRC_DIR"/monico-connect "$SRC_DIR"/monico-pair "$SRC_DIR"/install-termux.sh "$APP_DIR/"
for cmd in monico-server monico-cmd monico-model monico-pair; do
  cat > "$BIN_DIR/$cmd" <<EOF
#!/usr/bin/env bash
cd "$APP_DIR" && exec python3 "$APP_DIR/$cmd" "\$@"
EOF
  chmod +x "$BIN_DIR/$cmd" "$APP_DIR/$cmd"
done
echo "Done. Start the server with:"
echo "  monico-server start --public"
echo "It prints a 4-digit code. On the phone, install the repo and run"
echo "monico-connect, then type that code. A fresh code any time with:"
echo "  monico-pair"
