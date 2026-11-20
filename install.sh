#!/bin/sh
# Run the correct installer on ordinary Linux or on Termux for Android.
# Usage: sh install.sh (does not require executable file permissions).
set -eu
DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -n "${TERMUX_VERSION:-}" ] || [ -d /data/data/com.termux/files/usr ] ||
  case "${PREFIX:-}" in */com.termux/*) true ;; *) false ;; esac
then
  exec bash "$DIR/install-termux.sh" "$@"
fi
exec bash "$DIR/install-linux.sh" "$@"
