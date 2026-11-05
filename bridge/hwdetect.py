"""Hardware detection shared by the bridge (device tiles) and the
model installer (tier picks). Stdlib only."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
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


def _android_prop(name: str) -> str:
    try:
        out = subprocess.run(["getprop", name], capture_output=True, text=True, timeout=3)
        return out.stdout.strip()
    except Exception:
        return ""


def _gpu() -> str:
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
# VM does not have (e.g. Jay's Galaxy S22) without pretending the VM
# is that hardware. Labelled as profiles everywhere they are used.
PROFILES: dict[str, dict] = {
    "galaxy-s22": {
        "platform": "android-termux", "device_model": "Samsung Galaxy S22",
        "arch": "aarch64", "cpu_cores": 8, "ram_total_gb": 8.0,
        "ram_available_gb": 0, "disk_free_gb": 64.0,
        "gpu": "Adreno 730 (no local LLM GPU path, CPU inference)",
        "hostname": "galaxy-s22 (profile)",
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
