"""Storage detection: the internal disk plus any plugged-in USB
drives, and where the model installer should put models.

Stdlib only (lsblk used when present, it is a standard system tool).
Read-only: this module never mounts, formats, or writes to a drive.
An unmounted USB device is reported as detected-but-unmounted rather
than mounted for you.

Every entry is shaped for the /api/devices payload:

    {"label": str, "path": str | None, "total_gb": float,
     "free_gb": float | None, "kind": "internal" | "usb",
     "mounted": bool}

Sizes are decimal GB rounded to 0.1, the same convention as
hwdetect's disk_free_gb. `path`/`free_gb` are None for a USB device
that is detected but not mounted: never invent numbers for it.

TEST HOOK: if the environment variable MONICO_USB_TEST_MOUNT names an
existing directory, detection reports that directory as a mounted USB
drive. It exists so the detection -> installer -> device-info path
can be proven on machines with no USB hardware (like the dev VM).
It is never set in production use.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

TEST_HOOK_ENV = "MONICO_USB_TEST_MOUNT"
MODEL_DIR_ENV = "MONICO_MODEL_DIR"
USB_MODELS_SUBDIR = "monico-models"
# A USB drive counts as usable model storage when it has at least
# this much free: pick() budgets half the free space, and the
# smallest catalogue model is 0.40 GB, so 1.0 GB free is the floor
# where the USB can actually hold a model with the half-free rule.
USB_MIN_FREE_GB = 1.0

_PSEUDO_FS = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup",
              "cgroup2", "mqueue", "shm", "overlay", "squashfs",
              "autofs", "configfs", "debugfs", "tracefs", "securityfs",
              "pstore", "bpf", "hugetlbfs", "fusectl", "ramfs"}


def _gb(num_bytes: float) -> float:
    return round(num_bytes / 1e9, 1)


def _entry(label: str, path: str | None, total_gb: float,
           free_gb: float | None, kind: str, mounted: bool) -> dict:
    return {"label": label, "path": path, "total_gb": total_gb,
            "free_gb": free_gb, "kind": kind, "mounted": mounted}


# ----------------------------------------------------------------------
# lsblk source (preferred: it knows the bus a disk hangs off)
# ----------------------------------------------------------------------
def _lsblk() -> list[dict] | None:
    """lsblk as a device tree, or None when lsblk is unusable."""
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-b", "-o",
             "NAME,TRAN,MOUNTPOINT,MOUNTPOINTS,FSTYPE,LABEL,SIZE,TYPE,RM"],
            capture_output=True, text=True, timeout=6)
        if out.returncode != 0:
            return None
        devs = json.loads(out.stdout).get("blockdevices")
        return devs if isinstance(devs, list) else None
    except Exception:
        return None


def _mountpoints(node: dict) -> list[str]:
    pts = node.get("mountpoints")
    if isinstance(pts, list):
        return [p for p in pts if p and not p.startswith("[")]
    mp = node.get("mountpoint")
    return [mp] if mp and not mp.startswith("[") else []


def _usb_rows_from_lsblk(devices: list[dict],
                         usage=shutil.disk_usage) -> tuple[list[dict], list[dict]]:
    """(mounted, unmounted) USB entries from an lsblk device tree.

    Pure apart from `usage` (injectable for tests). A disk counts as
    USB when it or any ancestor reports tran == "usb"; the flag is
    inherited down the tree because partitions often report no tran
    of their own.
    """
    mounted: list[dict] = []
    unmounted: list[dict] = []
    seen_paths: set[str] = set()

    def subtree(node: dict):
        yield node
        for child in node.get("children") or []:
            yield from subtree(child)

    def walk_mounted(node: dict, usb: bool, disk_label: str) -> None:
        usb = usb or str(node.get("tran") or "").lower() == "usb"
        if node.get("type") == "disk":
            disk_label = node.get("label") or disk_label
        added = False
        if usb:
            label = node.get("label") or disk_label or "USB storage"
            for mp in _mountpoints(node):
                if mp in seen_paths:
                    continue
                seen_paths.add(mp)
                try:
                    u = usage(mp)
                    mounted.append(_entry(label, mp, _gb(u.total),
                                          _gb(u.free), "usb", True))
                    added = True
                except Exception:
                    continue
        if added:
            return  # children of a mounted node add nothing
        for child in node.get("children") or []:
            walk_mounted(child, usb, disk_label)

    def walk_unmounted(node: dict, usb: bool) -> None:
        usb = usb or str(node.get("tran") or "").lower() == "usb"
        if usb and node.get("type") == "disk":
            if not any(_mountpoints(n) for n in subtree(node)):
                unmounted.append(_entry(node.get("label") or "USB storage",
                                        None,
                                        _gb(float(node.get("size") or 0)),
                                        None, "usb", False))
            return
        for child in node.get("children") or []:
            walk_unmounted(child, usb)

    for dev in devices:
        walk_mounted(dev, False, "")
        walk_unmounted(dev, False)
    return mounted, unmounted


# ----------------------------------------------------------------------
# /proc/mounts + /sys fallback (Termux and minimal systems, no lsblk)
# ----------------------------------------------------------------------
def _disk_name(part: str) -> str:
    """Partition name -> disk name (sda1 -> sda, mmcblk1p2 -> mmcblk1)."""
    if part.startswith(("mmcblk", "nvme")) and "p" in part:
        return part.rsplit("p", 1)[0]
    return part.rstrip("0123456789")


def _sys_says_usb(part: str) -> bool:
    """True when the sysfs path of a block device goes through a USB
    host. Conservative: any doubt answers False."""
    try:
        real = os.path.realpath(f"/sys/class/block/{part}")
        return "/usb" in real
    except Exception:
        return False


def _usb_rows_from_mounts(mounts_text: str, is_usb=_sys_says_usb,
                          usage=shutil.disk_usage) -> list[dict]:
    """Mounted USB entries parsed from /proc/mounts contents."""
    rows: list[dict] = []
    seen: set[str] = set()
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        source, target, fstype = parts[0], parts[1].replace("\\040", " "), parts[2]
        if fstype in _PSEUDO_FS or not source.startswith("/dev/"):
            continue
        if target in seen:
            continue
        if not is_usb(os.path.basename(source)):
            continue
        try:
            u = usage(target)
        except Exception:
            continue
        seen.add(target)
        rows.append(_entry("USB storage", target, _gb(u.total),
                           _gb(u.free), "usb", True))
    return rows


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def detect_storage() -> list[dict]:
    """All storage a device info payload should report: the internal
    disk first, then mounted USB drives, then unmounted USB devices."""
    home = str(Path.home())
    try:
        u = shutil.disk_usage(home)
        internal = _entry("Internal", home, _gb(u.total), _gb(u.free),
                          "internal", True)
    except Exception:
        internal = _entry("Internal", home, 0.0, 0.0, "internal", True)
    entries = [internal]

    usb_mounted: list[dict] = []
    usb_unmounted: list[dict] = []
    test_mount = os.environ.get(TEST_HOOK_ENV, "").strip()
    if test_mount and Path(test_mount).is_dir():
        try:
            u = shutil.disk_usage(test_mount)
            usb_mounted.append(_entry("USB storage", test_mount,
                                      _gb(u.total), _gb(u.free), "usb", True))
        except Exception:
            pass
    devs = _lsblk()
    if devs is not None:
        m, un = _usb_rows_from_lsblk(devs)
        usb_mounted.extend(m)
        usb_unmounted.extend(un)
    else:
        try:
            usb_mounted.extend(_usb_rows_from_mounts(
                Path("/proc/mounts").read_text()))
        except Exception:
            pass

    seen_paths: set[str] = set()
    for e in usb_mounted:
        if e["path"] in seen_paths:
            continue
        seen_paths.add(e["path"])
        entries.append(e)
    entries.extend(usb_unmounted)
    return entries


def choose_model_storage(hw: dict | None,
                         entries: list[dict] | None = None) -> dict:
    """Where models live on this device, and how much room is there.

    Resolution order (Jay's USB rule):
      1. $MONICO_MODEL_DIR explicit override (kind "override").
      2. A mounted, writable USB drive with at least USB_MIN_FREE_GB
         free -> <mount>/monico-models (kind "usb"); the roomiest
         such drive wins.
      3. The internal default, ~/.monico-bridge/models (kind
         "internal").

    `entries` defaults to hw["storage"]; a hw dict without a
    "storage" key (a dry-run profile) never triggers live detection,
    so profiles keep their canned internal numbers.
    """
    from .common import DATA_DIR  # local import: common has no deps on us

    override = os.environ.get(MODEL_DIR_ENV, "").strip()
    if override:
        base = Path(override).expanduser()
        anchor = base
        while not anchor.exists() and anchor.parent != anchor:
            anchor = anchor.parent
        try:
            free_gb = _gb(shutil.disk_usage(str(anchor)).free)
        except Exception:
            free_gb = 0.0
        return {"kind": "override", "models_dir": str(base),
                "mount": None, "free_gb": free_gb}

    if entries is None:
        entries = (hw or {}).get("storage") or []
    usb = [e for e in entries
           if e.get("kind") == "usb" and e.get("mounted") and e.get("path")
           and (e.get("free_gb") or 0) >= USB_MIN_FREE_GB
           and os.access(e["path"], os.W_OK)]
    if usb:
        best = max(usb, key=lambda e: e.get("free_gb") or 0)
        return {"kind": "usb",
                "models_dir": str(Path(best["path"]) / USB_MODELS_SUBDIR),
                "mount": best["path"], "free_gb": best["free_gb"]}

    free_gb = float((hw or {}).get("disk_free_gb") or 0)
    if not free_gb:
        try:
            free_gb = _gb(shutil.disk_usage(str(Path.home())).free)
        except Exception:
            free_gb = 0.0
    return {"kind": "internal", "models_dir": str(DATA_DIR / "models"),
            "mount": None, "free_gb": round(free_gb, 1)}
