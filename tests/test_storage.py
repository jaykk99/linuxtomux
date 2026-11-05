"""USB storage detection + model-storage resolution tests.
Run: python3 -m pytest tests/ -q"""
import os
import sys
from collections import namedtuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONICO_HOME", "/tmp/monico-bridge-test-home")

from bridge import model_installer, storage  # noqa: E402
from bridge.hwdetect import PROFILES  # noqa: E402

Usage = namedtuple("Usage", ["total", "free", "used"])


def fake_usage_factory(sizes):
    def fake_usage(path):
        total, free = sizes[str(path)]
        return Usage(total=total, free=free, used=total - free)
    return fake_usage


LSBLK_TREE = [
    {"name": "sda", "tran": "sata", "type": "disk", "size": 500_000_000_000,
     "label": None, "mountpoints": [None],
     "children": [
         {"name": "sda1", "tran": None, "type": "part", "label": None,
          "size": 500_000_000_000, "mountpoints": ["/"]}]},
    {"name": "sdb", "tran": "usb", "type": "disk", "size": 62_000_000_000,
     "label": None, "mountpoints": [None],
     "children": [
         {"name": "sdb1", "tran": None, "type": "part", "label": "USBSTICK",
          "size": 62_000_000_000, "mountpoints": ["/media/jay/USB"]}]},
    {"name": "sdc", "tran": "usb", "type": "disk", "size": 32_000_000_000,
     "label": None, "mountpoints": [None],
     "children": [
         {"name": "sdc1", "tran": None, "type": "part", "label": None,
          "size": 32_000_000_000, "mountpoints": [None]}]},
    {"name": "loop0", "tran": None, "type": "loop", "size": 1_000_000_000,
     "label": None, "mountpoints": ["[SWAP]"]},
]


def test_lsblk_usb_mounted_and_unmounted():
    usage = fake_usage_factory({"/media/jay/USB": (62_000_000_000,
                                                   40_000_000_000)})
    mounted, unmounted = storage._usb_rows_from_lsblk(LSBLK_TREE, usage=usage)
    assert len(mounted) == 1, mounted
    e = mounted[0]
    assert e["kind"] == "usb" and e["mounted"] is True
    assert e["path"] == "/media/jay/USB" and e["label"] == "USBSTICK"
    assert e["total_gb"] == 62.0 and e["free_gb"] == 40.0
    # sda (SATA root) never appears; sdc is detected but unmounted,
    # with a size and honestly-null free space.
    assert len(unmounted) == 1, unmounted
    u = unmounted[0]
    assert u["kind"] == "usb" and u["mounted"] is False
    assert u["path"] is None and u["free_gb"] is None
    assert u["total_gb"] == 32.0


MOUNTS = """proc /proc proc rw 0 0
/dev/sda1 / ext4 rw 0 0
tmpfs /run tmpfs rw 0 0
/dev/sdb1 /media/jay/USB vfat rw 0 0
/dev/sdb2 /media/jay/DATA exfat rw 0 0
"""


def test_mounts_fallback_parsing():
    usage = fake_usage_factory({"/media/jay/USB": (62_000_000_000, 10_000_000_000),
                                "/media/jay/DATA": (32_000_000_000, 5_000_000_000)})
    rows = storage._usb_rows_from_mounts(
        MOUNTS, is_usb=lambda name: name.startswith("sdb"), usage=usage)
    assert [r["path"] for r in rows] == ["/media/jay/USB", "/media/jay/DATA"]
    assert all(r["kind"] == "usb" and r["mounted"] for r in rows)
    assert storage._disk_name("sdb12") == "sdb"
    assert storage._disk_name("mmcblk1p2") == "mmcblk1"
    assert storage._disk_name("nvme0n1p3") == "nvme0n1"


def test_test_hook_reports_dir_as_usb(tmp_path, monkeypatch):
    monkeypatch.setenv(storage.TEST_HOOK_ENV, str(tmp_path))
    entries = storage.detect_storage()
    assert entries[0]["kind"] == "internal"
    usb = [e for e in entries if e["kind"] == "usb" and e["path"] == str(tmp_path)]
    assert len(usb) == 1 and usb[0]["mounted"] is True
    assert usb[0]["free_gb"] > 0


def _hw_with_usb(usb_path, usb_free, internal_free=5.0):
    return {"disk_free_gb": internal_free,
            "storage": [
                {"label": "Internal", "path": "/home/jay", "total_gb": 100.0,
                 "free_gb": internal_free, "kind": "internal", "mounted": True},
                {"label": "USB storage", "path": str(usb_path),
                 "total_gb": 62.0, "free_gb": usb_free,
                 "kind": "usb", "mounted": True}]}


def test_resolution_override_beats_usb(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere"
    monkeypatch.setenv(storage.MODEL_DIR_ENV, str(target))
    choice = storage.choose_model_storage(_hw_with_usb(tmp_path, 40.0))
    assert choice["kind"] == "override"
    assert choice["models_dir"] == str(target)


def test_resolution_usb_beats_internal(tmp_path, monkeypatch):
    monkeypatch.delenv(storage.MODEL_DIR_ENV, raising=False)
    choice = storage.choose_model_storage(_hw_with_usb(tmp_path, 40.0))
    assert choice["kind"] == "usb"
    assert choice["mount"] == str(tmp_path)
    assert choice["models_dir"] == str(tmp_path / storage.USB_MODELS_SUBDIR)
    assert choice["free_gb"] == 40.0


def test_resolution_usb_too_small_falls_back(tmp_path, monkeypatch):
    monkeypatch.delenv(storage.MODEL_DIR_ENV, raising=False)
    choice = storage.choose_model_storage(_hw_with_usb(tmp_path, 0.5))
    assert choice["kind"] == "internal"
    assert choice["free_gb"] == 5.0


def test_resolution_profile_hw_never_detects_live(monkeypatch):
    monkeypatch.delenv(storage.MODEL_DIR_ENV, raising=False)
    hw = dict(PROFILES["linux-box-32gb"])  # no "storage" key at all
    choice = storage.choose_model_storage(hw)
    assert choice["kind"] == "internal"
    assert choice["free_gb"] == hw["disk_free_gb"]


def test_pick_counts_chosen_storage_space():
    hw = dict(PROFILES["linux-vm-8gb"])
    roomy = {"kind": "usb", "models_dir": "/media/jay/USB/monico-models",
             "mount": "/media/jay/USB", "free_gb": 62.0}
    tight = {"kind": "usb", "models_dir": "/media/jay/USB/monico-models",
             "mount": "/media/jay/USB", "free_gb": 2.0}
    big = model_installer.pick(hw, roomy)
    small = model_installer.pick(hw, tight)
    assert big["params_b"] in (7.0, 8.0), big
    # tight storage: disk budget 1.0 GB caps the pick at the 0.5B Q8_0.
    assert small["params_b"] == 0.5 and small["quant"] == "Q8_0", small
    assert small["models_dir"] == "/media/jay/USB/monico-models"
    assert small["storage"]["kind"] == "usb"
