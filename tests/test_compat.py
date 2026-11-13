"""Old-device compatibility guards.

The bridge has to run on the oldest Python still found on real boxes
(Debian 10, Raspberry Pi OS, an old Termux install), not just the dev
VM. These tests keep newer-only syntax and runtime APIs out of
bridge/: 3.7 is the floor (f-strings, `from __future__ import
annotations`, ThreadingHTTPServer), 3.8/3.9/3.10-only conveniences
are not allowed.

Run: python3 -m pytest tests/ -q   (or python3 tests/test_compat.py)
"""
import ast
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = sorted((ROOT / "bridge").glob("*.py"))
TOO_NEW_APIS = ("tomllib", "graphlib", "zoneinfo", "functools.cache",
                "asyncio.to_thread", "str.removeprefix", "str.removesuffix")


def _parse_as_37(source: str, filename: str):
    """ast.parse grew feature_version in 3.8 (int first, tuples later).
    On 3.7 itself the running grammar is already the floor, so parse
    normally. This keeps the guard runnable on the CI floor too."""
    if sys.version_info < (3, 8):
        return ast.parse(source, filename=filename)
    try:
        return ast.parse(source, filename=filename, feature_version=(3, 7))
    except TypeError:
        return ast.parse(source, filename=filename, feature_version=7)


def test_every_bridge_module_parses_as_python_37():
    for path in BRIDGE:
        _parse_as_37(path.read_text(), str(path))


def test_no_python38_only_unlink_keyword():
    for path in BRIDGE:
        assert "missing_ok=" not in path.read_text(), path


def test_no_python39_only_string_helpers():
    for path in BRIDGE:
        text = path.read_text()
        assert ".removeprefix(" not in text, path
        assert ".removesuffix(" not in text, path


def test_no_python310_runtime_union_in_isinstance():
    for path in BRIDGE:
        for line in path.read_text().splitlines():
            if "isinstance(" in line and "|" in line:
                raise AssertionError(f"{path}: runtime union: {line}")


def test_no_modern_only_stdlib():
    for path in BRIDGE:
        text = path.read_text()
        for token in TOO_NEW_APIS:
            assert token not in text, (path, token)


def test_no_runtime_dict_union_merge():
    """`a | b` on dicts is 3.9+. Detect the merge shape rather than
    `|` inside annotations/docstrings, which is harmless with
    `from __future__ import annotations`."""
    for path in BRIDGE:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)
                    and isinstance(node.left, ast.Dict)
                    and isinstance(node.right, ast.Dict)):
                raise AssertionError(
                    f"{path}:{node.lineno}: dict | dict needs Python 3.9")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
