"""Runtime hygiene: atomic settings writes, the install-tarball cache
and the hardware probe caches.
Run: python3 -m pytest tests/ -q   (or python3 tests/test_runtime.py)
"""
import io
import json
import os
import stat
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONICO_HOME", "/tmp/monico-runtime-test-home")

from bridge import common, hwdetect, server  # noqa: E402


def test_save_json_is_atomic_private_and_leaves_no_temp_files():
    d = Path(tempfile.mkdtemp())
    path = d / "pools.json"
    common.save_json(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in d.iterdir()] == ["pools.json"]

    # Serialising fails before anything is written, so the old file survives.
    try:
        common.save_json(path, {"bad": {1, 2}})
        raise AssertionError("expected a TypeError for a set value")
    except TypeError:
        pass
    assert json.loads(path.read_text()) == {"a": 1}
    assert [p.name for p in d.iterdir()] == ["pools.json"]


def test_tarball_signature_tracks_changes():
    root = Path(tempfile.mkdtemp())
    (root / "bridge").mkdir()
    (root / "ui").mkdir()
    (root / "bridge" / "a.py").write_text("x = 1\n")
    (root / "ui" / "b.html").write_text("<p>hi</p>\n")
    for name in server.TARBALL_FILES:
        (root / name).write_text("#!/bin/sh\n")
    sig = server._repo_tarball_signature(root)
    assert sig == server._repo_tarball_signature(root)
    (root / "bridge" / "a.py").write_text("x = 12345\n")   # different size
    assert server._repo_tarball_signature(root) != sig


def test_tarball_is_cached_and_skips_pycache():
    first = server._repo_tarball_bytes()
    second = server._repo_tarball_bytes()
    assert first is second, "an unchanged checkout must come from cache"
    with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as tf:
        names = tf.getnames()
    assert "ui/dashboard.html" in names, names[:5]
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]


def test_gpu_probe_is_cached_for_the_process():
    class Done:
        returncode = 0
        stdout = "Fake GPU, 8192 MiB\n"

    calls = []
    real = hwdetect.subprocess.run
    hwdetect.subprocess.run = lambda *a, **k: (calls.append(a[0]), Done())[1]
    old = dict(hwdetect._GPU_CACHE)
    hwdetect._GPU_CACHE.update(at=0.0, value="")
    try:
        values = [hwdetect._gpu() for _ in range(5)]
    finally:
        hwdetect.subprocess.run = real
        hwdetect._GPU_CACHE.update(old)
    assert values == ["Fake GPU, 8192 MiB"] * 5
    assert len(calls) == 1, calls


def test_android_prop_failures_are_not_cached():
    real = hwdetect.subprocess.run
    hwdetect._PROP_CACHE.pop("ro.monico.test", None)

    def boom(*a, **k):
        raise OSError("no getprop")

    hwdetect.subprocess.run = boom
    try:
        assert hwdetect._android_prop("ro.monico.test") == ""
    finally:
        hwdetect.subprocess.run = real
    assert "ro.monico.test" not in hwdetect._PROP_CACHE


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
