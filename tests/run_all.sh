#!/bin/sh
# Run the whole suite: compile the tree first, then every test module.
# No third-party packages are required, but pytest is used when it is
# importable so the pytest-only modules run too.
#
#   ./tests/run_all.sh
set -e
cd "$(dirname "$0")/.."

sh -n install.sh && bash -n install-linux.sh && bash -n install-termux.sh
python3 -m py_compile bridge/*.py tests/*.py monico-server monico-connect \
    monico-cmd monico-model
echo "compile OK"

# Six modules (connect, offline, pin, proxy, storage, tunnel) only carry
# pytest style tests. They used to be skipped silently here, which meant
# CI never ran them. Use pytest when it is available; otherwise fall back
# to the __main__ runners and print exactly which files went untested.
if python3 -c 'import pytest' >/dev/null 2>&1; then
    exec python3 -m pytest tests/ -q
fi

echo "pytest not installed: running __main__ runners only"
fail=0
skipped=""
for f in tests/test_*.py; do
    echo ""
    if grep -q '__main__' "$f"; then
        echo "== $f"
        python3 "$f" || fail=1
    else
        echo "== $f (skipped: needs pytest)"
        skipped="$skipped $(basename "$f")"
    fi
done
if [ -n "$skipped" ]; then
    echo ""
    echo "NOT RUN (need pytest):$skipped"
    echo "install it with: python3 -m pip install pytest"
fi
exit $fail
