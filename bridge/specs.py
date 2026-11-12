"""Live device specs: the numbers that move (RAM in use, storage in
use, CPU load, uptime, battery), next to the few that do not (OS,
CPU model). Stdlib only, so the identical code runs on a Linux box
and inside Termux.

hwdetect.detect() answers "what is this device" once; live_specs()
answers "how is it doing right now" and is cheap enough to call every
few seconds. Every device in the mesh reports this payload and the
server shows them all on the dashboard (/dashboard).

Shape (any field a device cannot read is None, never a made-up
number; Android hides /proc/stat from apps, so a phone usually has
no CPU percentage):

    {"at": 1731440000.0,
     "os": {"name": "Ubuntu 24.04.1 LTS", "kernel": "6.8.0"},
     "cpu": {"model": str, "cores": 8, "arch": "aarch64",
             "pct": 12.5 | None, "load1": 0.42 | None},
     "ram": {"total_gb": 7.6, "used_gb": 5.1, "available_gb": 2.5,
             "pct": 67.1},
     "storage": [{"label": "Internal", "kind": "internal",
                  "path": "/home/jay", "total_gb": 256.0,
                  "used_gb": 101.2, "free_gb": 154.8, "pct": 39.5}],
     "uptime_s": 86400 | None,
     "battery": {"percent": 81, "status": "Charging",
                 "plugged": True, "temp_c": 31.0,
                 "source": "termux-api"} | None}
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .hwdetect import _android_prop, _meminfo, is_termux

STORAGE_REFRESH_S = 30     # re-scan which drives exist this often
BATTERY_REFRESH_S = 30     # battery moves slowly; do not hammer it
BATTERY_RETRY_S = 300      # after a failed read, wait before retrying
CPU_FIRST_SAMPLE_S = 0.15  # one short sample so the first report has a %

_lock = threading.Lock()
_static: dict | None = None
_cpu_prev: tuple[int, int] | None = None
_cpu_last_pct: float | None = None
_storage_cache: tuple[float, list[dict]] = (0.0, [])
_battery_cache: tuple[float, dict | None] = (0.0, None)
_battery_retry_at = 0.0


def _pct(used: float, total: float) -> float | None:
    if not total or total <= 0:
        return None
    return round(max(0.0, min(100.0, used / total * 100.0)), 1)


# ----------------------------------------------------------------------
# Things that do not change while the process runs: read them once.
# ----------------------------------------------------------------------
def _os_name(termux: bool) -> str:
    if termux:
        rel = _android_prop("ro.build.version.release")
        return f"Android {rel}".strip() if rel else "Android"
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return platform.system() or "Linux"


def _cpu_model(termux: bool) -> str:
    try:
        fallback = ""
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            key, _, val = line.partition(":")
            key, val = key.strip().lower(), val.strip()
            if key == "model name" and val:
                return val
            if key in ("hardware", "model") and val and not fallback:
                fallback = val
        if fallback:
            return fallback
    except Exception:
        pass
    if termux:
        for prop in ("ro.soc.model", "ro.board.platform", "ro.hardware"):
            val = _android_prop(prop)
            if val:
                return val
    return platform.processor() or ""


def _static_info() -> dict:
    global _static
    if _static is None:
        termux = is_termux()
        _static = {
            "termux": termux,
            "os": {"name": _os_name(termux), "kernel": platform.release()},
            "cpu_model": _cpu_model(termux),
            "arch": platform.machine(),
        }
    return _static


# ----------------------------------------------------------------------
# CPU: busy share of all cores between two reads of /proc/stat
# ----------------------------------------------------------------------
def _read_cpu_times() -> tuple[int, int] | None:
    """(busy, total) jiffies summed over all cores, or None when the
    kernel will not show them (Android blocks /proc/stat for apps)."""
    try:
        first = Path("/proc/stat").read_text().splitlines()[0].split()
        if first[0] != "cpu":
            return None
        vals = [int(x) for x in first[1:9]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
        total = sum(vals)
        return total - idle, total
    except Exception:
        return None


def _cpu_pct() -> float | None:
    global _cpu_prev, _cpu_last_pct
    now = _read_cpu_times()
    if now is None:
        return None
    if _cpu_prev is None:
        _cpu_prev = now
        time.sleep(CPU_FIRST_SAMPLE_S)
        now = _read_cpu_times()
        if now is None:
            return None
    busy, total = now[0] - _cpu_prev[0], now[1] - _cpu_prev[1]
    if total <= 0:
        return _cpu_last_pct   # called twice inside one tick: keep last
    _cpu_prev = now
    _cpu_last_pct = _pct(busy, total)
    return _cpu_last_pct


def _load1() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except Exception:
        return None


# ----------------------------------------------------------------------
# RAM, storage, uptime
# ----------------------------------------------------------------------
def _ram() -> dict:
    mem = _meminfo()
    total_kb = mem.get("MemTotal", 0)
    avail_kb = mem.get("MemAvailable")
    if avail_kb is None:   # very old kernels: closest honest estimate
        avail_kb = (mem.get("MemFree", 0) + mem.get("Buffers", 0)
                    + mem.get("Cached", 0))
    used_kb = max(0, total_kb - avail_kb)
    gb = 1024 * 1024
    return {"total_gb": round(total_kb / gb, 2),
            "used_gb": round(used_kb / gb, 2),
            "available_gb": round(avail_kb / gb, 2),
            "pct": _pct(used_kb, total_kb)}


def _storage_sources() -> list[dict]:
    """Which drives exist (internal + USB), re-scanned on an interval
    so a USB drive plugged in later shows up. Usage numbers are read
    fresh on every call; only the list of drives is cached."""
    global _storage_cache
    at, entries = _storage_cache
    if entries and time.time() - at < STORAGE_REFRESH_S:
        return entries
    try:
        from .storage import detect_storage
        entries = detect_storage()
    except Exception:
        entries = [{"label": "Internal", "path": str(Path.home()),
                    "kind": "internal", "mounted": True}]
    _storage_cache = (time.time(), entries)
    return entries


def _storage() -> list[dict]:
    out: list[dict] = []
    for e in _storage_sources():
        row = {"label": e.get("label") or "Storage",
               "kind": e.get("kind") or "internal",
               "path": e.get("path"), "mounted": bool(e.get("mounted")),
               "total_gb": e.get("total_gb"), "used_gb": None,
               "free_gb": None, "pct": None}
        if e.get("path") and e.get("mounted"):
            try:
                u = shutil.disk_usage(e["path"])
                # "Used" as the owner sees it: everything that is not
                # free to them (matches df's Use%, reserved blocks in).
                used = u.total - u.free
                row.update(total_gb=round(u.total / 1e9, 1),
                           used_gb=round(used / 1e9, 1),
                           free_gb=round(u.free / 1e9, 1),
                           pct=_pct(used, u.total))
            except Exception:
                pass
        out.append(row)
    return out


def _uptime_s() -> int | None:
    try:
        return int(float(Path("/proc/uptime").read_text().split()[0]))
    except Exception:
        pass
    try:
        return int(time.clock_gettime(time.CLOCK_BOOTTIME))
    except Exception:
        return None


# ----------------------------------------------------------------------
# Battery: Termux:API on a phone, sysfs on a laptop, None on a desktop
# ----------------------------------------------------------------------
def _battery_termux() -> dict | None:
    if not shutil.which("termux-battery-status"):
        return None
    try:
        out = subprocess.run(["termux-battery-status"], capture_output=True,
                             text=True, timeout=6)
        data = json.loads(out.stdout or "{}")
    except Exception:
        return None   # hangs when the Termux:API app is not installed
    if "percentage" not in data:
        return None
    status = str(data.get("status") or "").replace("_", " ").capitalize()
    plugged = str(data.get("plugged") or "").upper()
    temp = data.get("temperature")
    return {"percent": int(round(float(data["percentage"]))),
            "status": status or "Unknown",
            "plugged": bool(plugged) and not plugged.startswith("UNPLUGGED"),
            "temp_c": round(float(temp), 1) if isinstance(temp, (int, float)) else None,
            "source": "termux-api"}


def _battery_sysfs() -> dict | None:
    base = Path("/sys/class/power_supply")
    try:
        supplies = sorted(base.iterdir())
    except Exception:
        return None
    for sup in supplies:
        try:
            if (sup / "type").read_text().strip().lower() != "battery":
                continue
            if (sup / "scope").exists() and \
                    (sup / "scope").read_text().strip().lower() == "device":
                continue   # a mouse or headset battery, not this machine
            percent = int((sup / "capacity").read_text().strip())
        except Exception:
            continue
        try:
            status = (sup / "status").read_text().strip() or "Unknown"
        except Exception:
            status = "Unknown"
        return {"percent": max(0, min(100, percent)), "status": status,
                "plugged": status.lower() in ("charging", "full", "not charging"),
                "temp_c": None, "source": "sysfs"}
    return None


def _battery() -> dict | None:
    global _battery_cache, _battery_retry_at
    now = time.time()
    at, cached = _battery_cache
    if cached is not None and now - at < BATTERY_REFRESH_S:
        return cached
    if cached is None and now < _battery_retry_at:
        return None
    bat = None
    if _static_info()["termux"]:
        bat = _battery_termux()
    if bat is None:
        bat = _battery_sysfs()
    _battery_cache = (now, bat)
    if bat is None:
        _battery_retry_at = now + BATTERY_RETRY_S
    return bat


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def live_specs() -> dict:
    """One live reading of this device. Safe to call from several
    threads; never raises (a section that fails reads as None)."""
    with _lock:
        st = _static_info()

        def safe(fn, default=None):
            try:
                return fn()
            except Exception:
                return default

        return {
            "at": time.time(),
            "os": st["os"],
            "cpu": {"model": st["cpu_model"], "cores": os.cpu_count() or 1,
                    "arch": st["arch"], "pct": safe(_cpu_pct),
                    "load1": safe(_load1)},
            "ram": safe(_ram, {}),
            "storage": safe(_storage, []),
            "uptime_s": safe(_uptime_s),
            "battery": safe(_battery),
        }


def clean_specs(specs) -> dict | None:
    """Accept a specs payload from the network only if it is a plain,
    small JSON object. The dashboard escapes everything it renders;
    this just keeps junk and oversized blobs out of the registry."""
    if not isinstance(specs, dict):
        return None
    try:
        if len(json.dumps(specs)) > 16 * 1024:
            return None
    except Exception:
        return None
    return specs
