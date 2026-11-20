"""Training data: real (device, model, quant) triples, labeled by the
repo's own fit rule.

Devices (nn/devices.json, 6 rows):
- the 4 dry-run profiles in bridge/hwdetect.py PROFILES
- the mesh-test fixture FAKE_HW in tests/test_mesh.py
- one snapshot of bridge.hwdetect.detect() on this VM, frozen with a
  timestamp (live RAM moves, so the snapshot is stored, not re-read)

Models: every (model, quant) in bridge/model_installer.py CATALOG
(4 models, 9 combos). Rows = 6 devices x 9 combos = 54.

Labels come from running the repo's own code, not from guessing:
for each device, usable = bridge.hwdetect.usable_ram_gb(hw) and the
storage choice = bridge.storage.choose_model_storage(hw), then the
budget is exactly what pick() computes,
    budget = min(usable / KV_HEADROOM, disk_free * 0.5),
and label = 1 (fits) when the catalog size_gb <= budget, else 0.
The picked triple for each device is always label 1; that is checked
in build_dataset and reported in the stats.

That makes the labels rule-derived, and the task says so in the
README and the PR: the network learns to approximate pick()'s
threshold from raw inputs. A near-100% test score means it reproduced
the rule, not that it discovered anything, and must be read that way.
"""
from __future__ import annotations

import json
import os
import random
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
DEVICES_PATH = HERE / "devices.json"


def _repo_root() -> Path:
    return HERE.parent


def load_devices(path: str | Path = DEVICES_PATH) -> List[Dict]:
    with open(path, encoding="utf-8") as fh:
        devices = json.load(fh)
    if not isinstance(devices, list) or not devices:
        raise ValueError(f"no devices in {path}")
    return devices


def build_dataset(devices_path: str | Path = DEVICES_PATH) -> Dict:
    """Build the 54 labeled rows. Returns rows + build stats."""
    import sys
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    # choose_model_storage reads $MONICO_MODEL_DIR for an override;
    # an override would make budgets depend on this machine's disks,
    # so training refuses to run with one set.
    if os.environ.get("MONICO_MODEL_DIR", "").strip():
        raise ValueError(
            "MONICO_MODEL_DIR is set; unset it before training so "
            "budgets come from the device records, not this disk.")
    from bridge.hwdetect import usable_ram_gb
    from bridge.model_installer import CATALOG, KV_HEADROOM, pick
    from bridge.storage import choose_model_storage

    devices = load_devices(devices_path)
    combos = []
    for name, _repo, params, quants, _lineage in CATALOG:
        for quant, (_fname, size) in sorted(quants.items()):
            combos.append({"model": name, "params_b": params,
                           "quant": quant, "size_gb": size})
    rows: List[Dict] = []
    for dev in devices:
        hw = {k: dev.get(k) for k in (
            "platform", "device_model", "arch", "cpu_cores",
            "ram_total_gb", "ram_available_gb", "disk_free_gb",
            "gpu", "hostname")}
        usable = usable_ram_gb(hw)
        storage = choose_model_storage(hw)
        disk_budget = float(storage.get("free_gb") or 0) * 0.5
        budget = min(usable / KV_HEADROOM, disk_budget)
        picked = pick(hw)
        for combo in combos:
            label = 1 if combo["size_gb"] <= budget else 0
            row = {
                "device": dev["device"],
                "platform": hw.get("platform"),
                "cpu_cores": hw.get("cpu_cores"),
                "ram_total_gb": hw.get("ram_total_gb"),
                "ram_available_gb": hw.get("ram_available_gb"),
                "disk_free_gb": hw.get("disk_free_gb"),
                "usable_ram_gb": round(usable, 2),
                "budget_gb": round(budget, 2),
                "model": combo["model"],
                "params_b": combo["params_b"],
                "quant": combo["quant"],
                "size_gb": combo["size_gb"],
                "label": label,
                "is_pick": (combo["model"] == picked["model"]
                            and combo["quant"] == picked["quant"]),
            }
            rows.append(row)
    n_pos = sum(1 for r in rows if r["label"] == 1)
    picks_fit = all(r["label"] == 1 for r in rows if r["is_pick"])
    stats = {
        "devices": len(devices),
        "combos_per_device": len(combos),
        "train_rows": len(rows),
        "n_positive": n_pos,
        "n_negative": len(rows) - n_pos,
        "picks_all_fit": picks_fit,
        "n_picks": sum(1 for r in rows if r["is_pick"]),
    }
    if not picks_fit:
        raise ValueError("pick() chose a triple labeled as not fitting; "
                         "the label rule and pick() disagree")
    return {"rows": rows, "stats": stats}


def stratified_split(rows: List[Dict], test_frac: float = 0.2,
                     seed: int = 42) -> Tuple[List[Dict], List[Dict]]:
    """Stratified train/test split (keeps the class ratio in both)."""
    rng = random.Random(seed)
    by_label: Dict[int, List[Dict]] = {}
    for r in rows:
        by_label.setdefault(r["label"], []).append(r)
    train: List[Dict] = []
    test: List[Dict] = []
    for label, items in by_label.items():
        items = list(items)
        rng.shuffle(items)
        n_test = max(1, round(len(items) * test_frac)) if len(items) > 1 else 0
        test.extend(items[:n_test])
        train.extend(items[n_test:])
    rng.shuffle(train)
    rng.shuffle(test)
    return train, test
