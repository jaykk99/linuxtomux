"""Build the nn-100h dataset from the repo's own data only.

Run from repo_root/nn-100h/:
    python3 build_dataset.py

Reads (live repo code, no invented values):
  - bridge/model_installer.py CATALOG: 4 models, 9 (model, quant) combos
    with their GGUF sizes.
  - bridge/hwdetect.py PROFILES: 4 dry-run device profiles.
  - tests/test_mesh.py FAKE_HW: the mesh-test fixture device.
  - bridge.hwdetect.detect() on THIS machine, frozen once with a
    timestamp (live RAM moves, so the snapshot is stored, not re-read).

Labels come from running the repo's own rule, exactly as pick() in
bridge/model_installer.py computes it:
    budget = min(usable_ram_gb(hw) / 1.25,
                 choose_model_storage(hw)["free_gb"] * 0.5)
    label = 1 (fits) if size_gb <= budget else 0 (no-fit)
As a sanity check, pick()'s own chosen combo must be label 1 for
every device (it is, asserted below).

Outputs (in this directory):
  - dataset.jsonl : one row per (device, model, quant), 54 rows.
  - split.json    : FROZEN grouped split (see below), seed 42.
  - DATASET.md    : provenance, counts, label definition, leakage audit.

Split (grouped, NOT random): the test set is the whole android-8gb
device family (the repo's only android-termux device, 9 rows). The
network never sees an Android device in training, so test accuracy
measures transfer to an unseen device family, not reproduction of
pick() on rows whose device it already saw. A random stratified split
would leak: pick() is deterministic in (device, quant), so a random
split puts near-identical rows on both sides and a 1.000 would mean
leakage, not learning. Test frac is 9/54 = 0.167, the closest a
whole-device holdout gets to 0.2 with 9-row device groups.

Re-running this script on another machine re-measures the live-vm row
(detect() output differs per machine) and therefore changes the
dataset; that is a new dataset version and must be committed as such.
train.py always trains on the COMMITTED dataset.jsonl / split.json,
never regenerates silently.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT))

from bridge.hwdetect import PROFILES, detect, usable_ram_gb  # noqa: E402
from bridge.model_installer import CATALOG, KV_HEADROOM, pick  # noqa: E402
from bridge.storage import choose_model_storage  # noqa: E402

# Mesh-test fixture, copied verbatim from tests/test_mesh.py FAKE_HW.
FAKE_HW = {"platform": "linux", "hostname": "x", "arch": "x86_64",
           "cpu_cores": 2, "ram_total_gb": 4.0, "ram_available_gb": 2.0,
           "disk_free_gb": 10.0, "gpu": "none", "device_model": ""}

TEST_DEVICES = ["android-8gb"]  # whole android-termux family held out
SPLIT_SEED = 42


def device_records() -> list[dict]:
    """All real device profiles in the repo, in a fixed order."""
    devices = []
    for name in ("android-8gb", "linux-vm-8gb",
                 "linux-box-32gb", "linux-box-64gb"):
        hw = dict(PROFILES[name])
        hw["device"] = name
        hw["source"] = "bridge/hwdetect.py PROFILES (repo dry-run profile)"
        devices.append(hw)
    mesh = dict(FAKE_HW)
    mesh["device"] = "mesh-tiny"
    mesh["source"] = "tests/test_mesh.py FAKE_HW (repo mesh-test fixture)"
    devices.append(mesh)
    live = detect()
    live["device"] = "live-vm"
    live["measured_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    live["source"] = ("bridge.hwdetect.detect() on this VM, snapshot "
                      "(RAM varies, so the values are frozen here)")
    devices.append(live)
    return devices


def build() -> tuple[list[dict], dict, list[dict]]:
    devices = device_records()
    combos = []
    for name, _repo, params, quants, _lineage in CATALOG:
        for quant in sorted(quants):
            fname, size = quants[quant]
            combos.append({"model": name, "params_b": params,
                           "quant": quant, "file": fname, "size_gb": size})
    rows: list[dict] = []
    budgets: dict[str, dict] = {}
    for hw in devices:
        usable = usable_ram_gb(hw)
        storage = choose_model_storage(hw)
        budget = min(usable / KV_HEADROOM,
                     float(storage.get("free_gb") or 0) * 0.5)
        budgets[hw["device"]] = {
            "usable_ram_gb": round(usable, 2),
            "storage_kind": storage.get("kind"),
            "storage_free_gb": storage.get("free_gb"),
            "budget_gb": round(budget, 2),
        }
        # Sanity: pick()'s own choice for this device must be label 1.
        choice = pick(hw, storage)
        assert choice["size_gb"] <= budget, (
            f"pick() choice does not fit its own budget on {hw['device']}")
        for combo in combos:
            label = 1 if combo["size_gb"] <= budget else 0
            rows.append({
                "row_id": f"{hw['device']}:{combo['model']}:{combo['quant']}",
                "device": hw["device"],
                "platform": hw.get("platform"),
                "cpu_cores": hw.get("cpu_cores"),
                "ram_total_gb": hw.get("ram_total_gb"),
                "ram_available_gb": hw.get("ram_available_gb"),
                "disk_free_gb": hw.get("disk_free_gb"),
                "usable_ram_gb": round(usable, 2),
                "model": combo["model"],
                "params_b": combo["params_b"],
                "quant": combo["quant"],
                "size_gb": combo["size_gb"],
                "label": label,
            })
    split = {
        "seed": SPLIT_SEED,
        "method": "grouped-by-device-family (NOT random stratified)",
        "test_devices": TEST_DEVICES,
        "train_devices": [d["device"] for d in devices
                          if d["device"] not in TEST_DEVICES],
        "test_frac_target": 0.2,
        "test_frac_actual": round(
            sum(1 for r in rows if r["device"] in TEST_DEVICES) / len(rows), 4),
        "train_idx": [i for i, r in enumerate(rows)
                      if r["device"] not in TEST_DEVICES],
        "test_idx": [i for i, r in enumerate(rows)
                     if r["device"] in TEST_DEVICES],
        "note": ("Test = whole android-8gb family (only android-termux "
                 "device in the repo). Train never sees Android, so the "
                 "test measures transfer to an unseen device family. "
                 "Exact 0.2 is impossible with 9-row device groups; "
                 "9/54 = 0.167 is the closest whole-device holdout."),
    }
    return rows, split, devices


def dataset_md(rows: list[dict], split: dict, budgets: dict,
               devices: list[dict]) -> str:
    n_fit = sum(r["label"] for r in rows)
    tr = [rows[i] for i in split["train_idx"]]
    te = [rows[i] for i in split["test_idx"]]
    lines = [
        "# nn-100h dataset — linuxtomux",
        "",
        "## Task",
        "",
        "Given a device and a (model, quant) from `bridge/model_installer.py`",
        "CATALOG, predict whether it **fits the device's budget** under",
        "`pick()`'s rule. Triage-only pre-check: `pick()` stays the",
        "authority, the network never installs anything and never overrides it.",
        "",
        "## Provenance (real repo data only, no invented devices, no synthetic rows)",
        "",
        f"- Built: {time.strftime('%Y-%m-%d %H:%M:%S %Z')} by `nn-100h/build_dataset.py`.",
        "- Models/quants: `bridge/model_installer.py` CATALOG — 4 models, "
        "9 (model, quant) combos with their GGUF sizes.",
        "- Devices (6): the 4 dry-run profiles in `bridge/hwdetect.py` PROFILES, "
        "the mesh-test fixture `FAKE_HW` in `tests/test_mesh.py`, and one frozen "
        "snapshot of `bridge.hwdetect.detect()` on this VM (`live-vm`, RAM moves "
        "so the values are frozen with a timestamp).",
        "- Labels: computed by running the repo's own rule, exactly as `pick()` "
        "does: `budget = min(usable_ram_gb / 1.25, storage_free_gb * 0.5)`; "
        "label 1 (fits) iff `size_gb <= budget`. `usable_ram_gb` and the storage "
        "choice come from the repo's own `bridge.hwdetect.usable_ram_gb` and "
        "`bridge.storage.choose_model_storage`. Every device's `pick()` choice "
        "is label 1 (asserted at build time).",
        "",
        "## Rows",
        "",
        f"- Total: {len(rows)} rows = 6 devices x 9 combos.",
        f"- Label balance: {n_fit} fit / {len(rows) - n_fit} no-fit.",
        f"- Train: {len(tr)} rows "
        f"({sum(r['label'] for r in tr)} fit / "
        f"{len(tr) - sum(r['label'] for r in tr)} no-fit).",
        f"- Test: {len(te)} rows "
        f"({sum(r['label'] for r in te)} fit / "
        f"{len(te) - sum(r['label'] for r in te)} no-fit).",
        "",
        "## Per-device budgets (pick() rule)",
        "",
    ]
    for d in devices:
        b = budgets[d["device"]]
        fit = sum(1 for r in rows
                  if r["device"] == d["device"] and r["label"] == 1)
        lines.append(
            f"- `{d['device']}`: usable_ram {b['usable_ram_gb']} GB, "
            f"storage {b['storage_kind']} free {b['storage_free_gb']} GB, "
            f"budget {b['budget_gb']} GB, fits {fit}/9.")
    lines += [
        "",
        "## Features (see train.py FEATURES)",
        "",
        "Raw inputs only: `params_b`, `size_gb`, quant one-hots + quant rank,",
        "`usable_ram_gb`, `disk_free_gb` (log-scaled), `is_android`, `cpu_cores`.",
        "Deliberately NOT features: the budget itself and the size/budget ratio.",
        "The network must learn the threshold from the raw numbers.",
        "",
        "## Split — GROUPED, leakage audit",
        "",
        "- `split.json`, seed 42. Test = the whole `android-8gb` device family",
        "  (the repo's only android-termux device). Train never sees Android.",
        "- Why grouped, not random stratified: `pick()` is a deterministic rule",
        "  in (device, quant). A random split puts near-identical rows on both",
        "  sides, so a high score would mostly mean the network reproduced the",
        "  rule on devices it already saw. The grouped split measures transfer",
        "  to an unseen device family instead.",
        "- Test frac is 9/54 = 0.167, not exactly 0.2: whole-device holdout with",
        "  9-row groups cannot hit 0.2 exactly, and stratification inside a",
        "  single held-out device is impossible by construction.",
        "- **A test accuracy of 1.000 is a leakage signal, not a win.** If it",
        "  happens, investigate (e.g. a feature smuggling the budget) before",
        "  claiming anything. The honest expectation is well below 1.000 on",
        "  9 held-out rows from an unseen platform.",
        "- Note: `is_android` is 0 on every train row and 1 on every test row,",
        "  so the network cannot learn a weight for it from training; transfer",
        "  must come from usable RAM / disk / size. That is part of the test.",
        "",
        "## Limits",
        "",
        "- 54 rows is small. One test row is 11% of test accuracy; error bars",
        "  are wide and every reported metric must carry its confusion matrix.",
        "- Labels are rule-derived: a high score means the network reproduced",
        "  `pick()`'s threshold, not that it discovered anything about hardware.",
        "- Re-running `build_dataset.py` on another machine re-measures the",
        "  `live-vm` row; that is a new dataset version, commit it as such.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    rows, split, devices = build()
    budgets = {}
    for hw in devices:
        usable = usable_ram_gb(hw)
        storage = choose_model_storage(hw)
        budgets[hw["device"]] = {
            "usable_ram_gb": round(usable, 2),
            "storage_kind": storage.get("kind"),
            "storage_free_gb": storage.get("free_gb"),
            "budget_gb": round(min(usable / KV_HEADROOM,
                                   float(storage.get("free_gb") or 0) * 0.5), 2),
        }
    with open(HERE / "dataset.jsonl", "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    with open(HERE / "split.json", "w", encoding="utf-8") as fh:
        json.dump(split, fh, indent=2)
        fh.write("\n")
    with open(HERE / "DATASET.md", "w", encoding="utf-8") as fh:
        fh.write(dataset_md(rows, split, budgets, devices))
    n_fit = sum(r["label"] for r in rows)
    print(f"wrote dataset.jsonl: {len(rows)} rows, "
          f"{n_fit} fit / {len(rows) - n_fit} no-fit")
    print(f"split: train {len(split['train_idx'])} / "
          f"test {len(split['test_idx'])} "
          f"(test devices: {split['test_devices']})")
    print("wrote split.json, DATASET.md")


if __name__ == "__main__":
    main()
