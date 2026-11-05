"""Optional, explicit-opt-in local model installer.

Uncensored ONLY: Dolphin / abliterated (Heretic-lineage) GGUF builds.
Never an RLHF-censored default, never a hosted API model. The pick is
driven by measured hardware: biggest model + best quant that fits the
device's usable RAM with headroom, and fits on disk.

Catalogue files are bartowski GGUF builds on Hugging Face (keyless
public downloads). Sizes are approximate on-disk GB for the quant.
The installer HEAD-checks the exact file before downloading and fails
honestly if a repo or file cannot be reached.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .common import DATA_DIR, MODEL_CONFIG, ensure_data_dir, save_json
from .hwdetect import PROFILES, detect, usable_ram_gb
from .storage import choose_model_storage

MODELS_DIR = DATA_DIR / "models"

# (display name, hf repo, params B, {quant: (filename, size_gb)}, lineage)
CATALOG = [
    ("Dolphin3.0-Qwen2.5-0.5B", "bartowski/Dolphin3.0-Qwen2.5-0.5B-GGUF", 0.5,
     {"Q4_K_M": ("Dolphin3.0-Qwen2.5-0.5B-Q4_K_M.gguf", 0.40),
      "Q8_0": ("Dolphin3.0-Qwen2.5-0.5B-Q8_0.gguf", 0.60)}, "Dolphin (uncensored)"),
    ("Dolphin3.0-Qwen2.5-1.5B", "bartowski/Dolphin3.0-Qwen2.5-1.5B-GGUF", 1.5,
     {"Q4_K_M": ("Dolphin3.0-Qwen2.5-1.5B-Q4_K_M.gguf", 1.05),
      "Q8_0": ("Dolphin3.0-Qwen2.5-1.5B-Q8_0.gguf", 1.70)}, "Dolphin (uncensored)"),
    ("Dolphin3.0-Qwen2.5-3B", "bartowski/Dolphin3.0-Qwen2.5-3b-GGUF", 3.0,
     {"Q4_K_M": ("Dolphin3.0-Qwen2.5-3b-Q4_K_M.gguf", 1.95),
      "Q8_0": ("Dolphin3.0-Qwen2.5-3b-Q8_0.gguf", 3.30)}, "Dolphin (uncensored)"),
    ("Dolphin3.0-Llama3.1-8B", "bartowski/Dolphin3.0-Llama3.1-8B-GGUF", 8.0,
     {"Q4_K_M": ("Dolphin3.0-Llama3.1-8B-Q4_K_M.gguf", 4.95),
      "Q5_K_M": ("Dolphin3.0-Llama3.1-8B-Q5_K_M.gguf", 5.75),
      "Q8_0": ("Dolphin3.0-Llama3.1-8B-Q8_0.gguf", 8.55)}, "Dolphin (uncensored)"),
    # NOTE (2026-11-04): bartowski never published Dolphin3.0-Qwen2.5
    # GGUFs above 3B (only 0.5B / 1.5B / 3b exist — verified against the
    # HF API). The old 7B/14B/32B/72B entries 404'd on every install.
]
QUANT_PREFERENCE = ["Q8_0", "Q5_K_M", "Q4_K_M"]  # best first when it fits
KV_HEADROOM = 1.25  # weights may use at most usable/KV_HEADROOM of usable RAM


def pick(hw: dict, storage_choice: dict | None = None) -> dict:
    """Biggest model whose weights fit usable RAM with KV headroom,
    at the best quant that still fits; must also fit on disk with the
    disk kept half free for everything else the device does.

    The disk side counts the CHOSEN storage (storage.choose_model_
    storage: $MONICO_MODEL_DIR override > plugged-in USB > internal
    default), so a USB drive's space can unlock a bigger pick than
    the internal disk alone would. With no storage_choice, the old
    behaviour is unchanged: hw["disk_free_gb"] (internal)."""
    if storage_choice is None:
        storage_choice = choose_model_storage(hw)
    usable = usable_ram_gb(hw)
    ram_budget = usable / KV_HEADROOM
    disk_budget = float(storage_choice.get("free_gb") or 0) * 0.5
    budget = min(ram_budget, disk_budget)
    best = None
    for name, repo, params, quants, lineage in CATALOG:
        for quant in QUANT_PREFERENCE:
            if quant not in quants:
                continue
            fname, size = quants[quant]
            if size <= budget:
                cand = {"model": name, "repo": repo, "quant": quant,
                        "file": fname, "size_gb": size, "params_b": params,
                        "lineage": lineage}
                if best is None or params > best["params_b"]:
                    best = cand
                break  # best quant for this model; try a bigger model
    if best is None:  # tiny device: smallest thing in the catalogue
        name, repo, params, quants, lineage = CATALOG[0]
        fname, size = quants["Q4_K_M"]
        best = {"model": name, "repo": repo, "quant": "Q4_K_M", "file": fname,
                "size_gb": size, "params_b": params, "lineage": lineage,
                "warning": "below comfortable headroom on this device"}
    best["usable_ram_gb"] = round(usable, 2)
    best["budget_gb"] = round(budget, 2)
    best["url"] = f"https://huggingface.co/{best['repo']}/resolve/main/{best['file']}"
    best["hardware"] = hw
    best["models_dir"] = storage_choice["models_dir"]
    best["storage"] = {"kind": storage_choice["kind"],
                       "path": storage_choice["models_dir"],
                       "mount": storage_choice.get("mount"),
                       "free_gb": storage_choice.get("free_gb")}
    return best


def find_runner() -> str | None:
    return shutil.which("llama-cli") or shutil.which("llama-server") or shutil.which("main")


def _head(url: str, timeout: int = 20) -> tuple[bool, str]:
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200, f"HTTP {resp.status}"
    except Exception as exc:
        return False, str(exc)


def _download(url: str, dest: Path) -> tuple[bool, str]:
    tmp = dest.with_suffix(".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "monico-model/0.1"})
        with urllib.request.urlopen(req, timeout=60) as resp, tmp.open("wb") as fh:
            shutil.copyfileobj(resp, fh, length=1024 * 1024)
        tmp.rename(dest)
        return True, f"{dest.stat().st_size / 1e9:.2f} GB"
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return False, str(exc)


def install(hw: dict, choice: dict, smoke: bool = True) -> dict:
    ensure_data_dir()
    models_dir = Path(choice.get("models_dir") or MODELS_DIR)
    models_dir.mkdir(parents=True, exist_ok=True)
    dest = models_dir / choice["file"]
    result = {"choice": choice, "steps": []}
    if dest.exists() and dest.stat().st_size > 1_000_000:
        result["steps"].append(f"model already on disk: {dest}")
    else:
        ok, why = _head(choice["url"])
        result["steps"].append(f"HEAD check {choice['url']}: {'ok' if ok else why}")
        if not ok:
            result["ok"] = False
            result["error"] = f"download source unreachable: {why}"
            return result
        ok, why = _download(choice["url"], dest)
        result["steps"].append(f"download: {'ok' if ok else why}")
        if not ok:
            result["ok"] = False
            result["error"] = f"download failed: {why}"
            return result
    cfg = {"model": choice["model"], "repo": choice["repo"], "quant": choice["quant"],
           "file": str(dest), "size_gb": choice["size_gb"],
           "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "device": hw.get("device_model") or hw.get("hostname")}
    # Record where the model actually lives (internal / USB / override)
    # so the bridge's model status never claims the wrong disk.
    if choice.get("storage"):
        cfg["storage"] = choice["storage"]
    cfg["models_dir"] = str(models_dir)
    runner = find_runner()
    cfg["runner"] = runner or "not installed yet"
    if smoke:
        if runner and "llama" in runner:
            t0 = time.time()
            try:
                proc = subprocess.run(
                    [runner, "-m", str(dest), "-p", "Say the word ready.",
                     "-n", "16", "-c", "512", "--temp", "0", "--no-display-prompt"],
                    capture_output=True, text=True, timeout=900)
                elapsed = time.time() - t0
                cfg["smoke"] = {"ok": proc.returncode == 0,
                                "seconds": round(elapsed, 1),
                                "tail": (proc.stdout or proc.stderr)[-400:]}
            except Exception as exc:
                cfg["smoke"] = {"ok": False, "error": str(exc)}
        else:
            cfg["smoke"] = {"ok": False,
                            "error": "no llama.cpp runner on this device yet "
                                     "(build note in README); model file is installed"}
    save_json(MODEL_CONFIG, cfg)
    result["config"] = cfg
    result["ok"] = True
    return result
