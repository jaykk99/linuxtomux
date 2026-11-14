#!/bin/sh
# One installer for both sides. Works the same on a Linux box and in
# Termux on Android:
#   ./install.sh          (or: sh install.sh)
# It spots Termux by itself and runs the right installer.
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -n "${TERMUX_VERSION:-}" ] || [ -d /data/data/com.termux ] \
   || case "${PREFIX:-}" in *com.termux*) true;; *) false;; esac; then
  exec bash "$DIR/install-termux.sh" "$@"
fi
exec bash "$DIR/install-linux.sh" "$@"
