#!/bin/sh
# Run every test module that carries its own __main__ runner, after
# compiling the whole tree. No third-party packages required.
#
#   ./tests/run_all.sh
#
# pytest-only modules (connect, offline, pin, proxy, storage, tunnel…)
# are listed as skipped; `python3 -m pytest tests/ -q` runs those when
# pytest is installed.
set -e
cd "$(dirname "$0")/.."

python3 -m py_compile bridge/*.py tests/*.py monico-server monico-connect \
    monico-cmd monico-model
echo "compile OK"

fail=0
for f in tests/test_*.py; do
    echo ""
    if grep -q '__main__' "$f"; then
        echo "== $f"
        python3 "$f" || fail=1
    else
        echo "== $f (pytest-only; skipped)"
    fi
done
exit $fail
