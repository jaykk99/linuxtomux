"""Hardware detection shared by the bridge (device tiles) and the
model installer (tier picks). Stdlib only."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from pathlib import Path


def _meminfo() -> dict:
    info: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith(":"):
                info[parts[0][:-1]] = int(parts[1])  # kB
    except Exception:
        pass
    return info


def is_termux() -> bool:
    prefix = os.environ.get("PREFIX", "")
    return ("com.termux" in prefix or bool(os.environ.get("TERMUX_VERSION"))
            or Path("/data/data/com.termux").exists())


# Android build props and the GPU do not change while the process
# runs, and each probe is a subprocess (getprop / nvidia-smi / rocm-smi).
# The server re-detects hardware every 30s, so caching these keeps old
# phones from spawning tools they cannot spare. A GPU is re-checked
# every few minutes for an eGPU or a driver appearing late.
_PROP_CACHE: dict[str, str] = {}
_GPU_CACHE: dict = {"at": 0.0, "value": ""}
GPU_TTL_S = 300


def _android_prop(name: str) -> str:
    if name in _PROP_CACHE:
        return _PROP_CACHE[name]
    try:
        out = subprocess.run(["getprop", name], capture_output=True,
                             text=True, timeout=3)
        value = out.stdout.strip()
    except Exception:
        value = ""
    if value:   # never cache a transient failure
        _PROP_CACHE[name] = value
    return value


def _gpu() -> str:
    now = time.monotonic()
    if _GPU_CACHE["value"] and now - _GPU_CACHE["at"] < GPU_TTL_S:
        return _GPU_CACHE["value"]
    value = _probe_gpu()
    _GPU_CACHE["at"] = now
    _GPU_CACHE["value"] = value
    return value


def _probe_gpu() -> str:
    for cmd in (["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                ["rocm-smi", "--showproductname"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=4)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip().splitlines()[0]
        except Exception:
            continue
    return "none detected (CPU inference)"


def detect() -> dict:
    """Base hardware facts. Never raises: on a stripped or odd box the
    caller gets zeros/empties, not a crash (Bridge and Client both call
    this at startup)."""
    try:
        return _detect_inner()
    except Exception:
        return {"platform": "linux" if not is_termux() else "android-termux",
                "device_model": "", "arch": platform.machine(),
                "cpu_cores": os.cpu_count() or 1, "ram_total_gb": 0,
                "ram_available_gb": 0, "disk_free_gb": 0,
                "gpu": "none detected (CPU inference)",
                "hostname": platform.node()}


def _detect_inner() -> dict:
    mem = _meminfo()
    total_kb = mem.get("MemTotal", 0)
    avail_kb = mem.get("MemAvailable", 0)
    termux = is_termux()
    model_name = ""
    if termux:
        model_name = " ".join(x for x in [_android_prop("ro.product.manufacturer"),
                                          _android_prop("ro.product.model")] if x)
    disk = shutil.disk_usage(str(Path.home()))
    hw = {
        "platform": "android-termux" if termux else "linux",
        "device_model": model_name,
        "arch": platform.machine(),
        "cpu_cores": os.cpu_count() or 1,
        "ram_total_gb": round(total_kb / 1024 / 1024, 2),
        "ram_available_gb": round(avail_kb / 1024 / 1024, 2),
        "disk_free_gb": round(disk.free / 1e9, 1),
        "gpu": _gpu(),
        "hostname": platform.node(),
    }
    # Additive: internal disk + any plugged-in USB drives, so device
    # info (and the model installer's storage pick) sees USB space.
    # disk_free_gb above stays internal-only, exactly as before.
    try:
        from .storage import detect_storage
        hw["storage"] = detect_storage()
    except Exception:
        hw["storage"] = []
    return hw


def usable_ram_gb(hw: dict) -> float:
    """RAM a local model may plan against, with headroom for the OS.

    Android keeps a big slice for itself + the Termux app, so a phone
    plans against total minus a 2.8GB reserve. A Linux box plans
    against MemAvailable, which is the honest number there.
    """
    total = float(hw.get("ram_total_gb") or 0)
    if hw.get("platform") == "android-termux":
        return max(0.5, total - 2.8)
    avail = float(hw.get("ram_available_gb") or 0)
    if avail > 0:
        return avail
    return total * 0.6


# Named dry-run profiles so tier picks can be shown for hardware this
# VM does not have (e.g. an 8 GB Android phone) without pretending the VM
# is that hardware. Labelled as profiles everywhere they are used.
PROFILES: dict[str, dict] = {
    "android-8gb": {
        "platform": "android-termux", "device_model": "Android phone (8 GB)",
        "arch": "aarch64", "cpu_cores": 8, "ram_total_gb": 8.0,
        "ram_available_gb": 0, "disk_free_gb": 64.0,
        "gpu": "mobile GPU (no local LLM GPU path, CPU inference)",
        "hostname": "android-8gb (profile)",
    },
    "linux-vm-8gb": {
        "platform": "linux", "device_model": "", "arch": "x86_64",
        "cpu_cores": 4, "ram_total_gb": 8.0, "ram_available_gb": 6.5,
        "disk_free_gb": 62.0, "gpu": "none detected (CPU inference)",
        "hostname": "linux-vm (profile)",
    },
    "linux-box-32gb": {
        "platform": "linux", "device_model": "", "arch": "x86_64",
        "cpu_cores": 16, "ram_total_gb": 32.0, "ram_available_gb": 29.0,
        "disk_free_gb": 900.0, "gpu": "none detected (CPU inference)",
        "hostname": "linux-box (profile)",
    },
    "linux-box-64gb": {
        "platform": "linux", "device_model": "", "arch": "x86_64",
        "cpu_cores": 24, "ram_total_gb": 64.0, "ram_available_gb": 58.0,
        "disk_free_gb": 1800.0, "gpu": "none detected (CPU inference)",
        "hostname": "linux-box-64 (profile)",
    },
}
