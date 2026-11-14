"""Feature extraction: (device, model, quant) triple -> numeric vector.

Every feature is a raw input to the fit decision: the model's size and
parameter count from the installer's CATALOG, the quant, and the
device's usable RAM / free disk / platform / cores from the device
record. Nothing derived from the label rule is a feature: the budget
(min(usable/1.25, disk_free*0.5)) and the size/budget ratio are NOT
inputs. The network has to learn the threshold from the raw numbers,
the same way pick() computes it.

Triple shape (see nn/data.py):
    {"device": str, "platform": str, "cpu_cores": int,
     "ram_total_gb": float, "ram_available_gb": float,
     "disk_free_gb": float, "model": str, "params_b": float,
     "quant": str, "size_gb": float, "usable_ram_gb": float}
"""
from __future__ import annotations

import math
from typing import Dict, List

QUANTS = ("Q4_K_M", "Q5_K_M", "Q8_0")
# Best first, as in bridge/model_installer.py QUANT_PREFERENCE.
QUANT_PREFERENCE = ["Q8_0", "Q5_K_M", "Q4_K_M"]


def feature_names() -> List[str]:
    return [
        "params_b",
        "size_gb",
        "quant:Q4_K_M",
        "quant:Q5_K_M",
        "quant:Q8_0",
        "quant_rank",
        "usable_ram_gb",
        "disk_free_gb",
        "is_android",
        "cpu_cores",
    ]


def extract(row: Dict) -> List[float]:
    """One dataset row -> feature vector in feature_names() order."""
    vec: List[float] = []
    vec.append(float(row.get("params_b") or 0) / 8.0)
    vec.append(float(row.get("size_gb") or 0) / 10.0)
    quant = str(row.get("quant") or "")
    for q in QUANTS:
        vec.append(1.0 if quant == q else 0.0)
    rank = (QUANT_PREFERENCE.index(quant)
            if quant in QUANT_PREFERENCE else len(QUANT_PREFERENCE))
    vec.append(rank / max(1, len(QUANT_PREFERENCE) - 1))
    vec.append(float(row.get("usable_ram_gb") or 0) / 64.0)
    disk = float(row.get("disk_free_gb") or 0)
    vec.append(min(1.0, math.log10(1.0 + disk) / math.log10(1801.0)))
    vec.append(1.0 if str(row.get("platform") or "") == "android-termux"
               else 0.0)
    vec.append(float(row.get("cpu_cores") or 0) / 24.0)
    return vec


def top_features(row: Dict, net, k: int = 3) -> List[Dict]:
    """Top contributing features for one scored triple (leave-one-out)."""
    x = extract(row)
    base = net.predict_proba(x)
    names = feature_names()
    deltas = []
    for i, name in enumerate(names):
        if x[i] == 0.0:
            continue
        x2 = list(x)
        x2[i] = 0.0
        d = base - net.predict_proba(x2)
        deltas.append((abs(d), d, name))
    deltas.sort(reverse=True)
    return [{"feature": name,
             "direction": "up" if d > 0 else "down",
             "delta": round(d, 4)}
            for _abs, d, name in deltas[:k]]
