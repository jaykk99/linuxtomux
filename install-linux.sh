#!/usr/bin/env bash
# Monico Labs bridge installer for the Linux side.
#   ./install-linux.sh      (./install.sh picks this one for you)
# Copies the app to ~/.monico-bridge/app and puts launchers in ~/.local/bin.
set -eu
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/.monico-bridge/app"
BIN_DIR="$HOME/.local/bin"

if ! command -v python3 >/dev/null 2>&1 \
   || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 7))'; then
  echo "ERROR: Python 3.7 or newer is needed. Install it first, e.g.:" >&2
  echo "  sudo apt install python3     (Debian/Ubuntu/Chromebook)" >&2
  echo "  sudo dnf install python3     (Fedora)" >&2
  echo "  sudo pacman -S python        (Arch)" >&2
  exit 1
fi

mkdir -p "$APP_DIR" "$BIN_DIR"
chmod 700 "$HOME/.monico-bridge" 2>/dev/null || true
# Fresh copy each time, so files removed upstream do not linger.
if [ "$SRC_DIR" != "$APP_DIR" ]; then
  rm -rf "$APP_DIR/bridge" "$APP_DIR/ui"
  cp -r "$SRC_DIR/bridge" "$SRC_DIR/ui" "$APP_DIR/"
  cp "$SRC_DIR"/monico-server "$SRC_DIR"/monico-cmd "$SRC_DIR"/monico-model \
     "$SRC_DIR"/monico-connect "$SRC_DIR"/monico-pair \
     "$SRC_DIR"/install-termux.sh "$APP_DIR/"
fi
for cmd in monico-server monico-cmd monico-model monico-pair monico-connect; do
  cat > "$BIN_DIR/$cmd" <<EOS
#!/usr/bin/env bash
cd "$APP_DIR" && exec python3 "$APP_DIR/$cmd" "\$@"
EOS
  chmod +x "$BIN_DIR/$cmd" "$APP_DIR/$cmd"
done

# ~/.local/bin is not on PATH on every distro (or in a fresh
# Chromebook container). Add it once so the commands just work.
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    line='export PATH="$HOME/.local/bin:$PATH"'
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
      [ -f "$rc" ] || [ "$rc" = "$HOME/.bashrc" ] || continue
      grep -qsF "$line" "$rc" || printf '\n%s\n' "$line" >> "$rc"
    done
    echo "Added ~/.local/bin to your PATH. Open a new terminal, or run:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo
    ;;
esac

echo "Done. Start the server with:"
echo "  monico-server start            (phone on the same WiFi)"
echo "  monico-server start --public   (Chromebook, or phone elsewhere)"
echo "It prints a 4-digit code. On the phone run monico-connect and"
echo "type that code. A fresh code any time with:"
echo "  monico-pair"
