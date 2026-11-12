"""Pools: two or more devices paired so they read, and are driven,
as one.

A pool has ONE card on the dashboard with the shared specs (RAM,
storage, cores added up across members) and each member's separate
specs under it, and ONE terminal. The terminal deals commands out
across the members so the load is split and no single device runs
out:

  - "split" (default): each command goes to one member. Members that
    are tight (RAM nearly full, or battery nearly empty and not
    charging) are skipped while a roomier one is available; among the
    rest the least busy, then the least recently used, gets it. With
    two healthy devices that is half and half. Several commands sent
    together are dealt out the same way and run at the same time.
  - "all": the command runs on every online member at once.

What a pool is NOT: it does not merge memory. One program still runs
on one device and can only use that device's RAM; the pool decides
WHICH device, and keeps both busy. Commands are independent shells,
so a `cd` or an exported variable does not carry to the next command
(the next one may land on the other device).

Pools live on the server they were created on
(~/.monico-bridge/pools.json); members can be any device in the
mesh, including ones on other servers (commands are relayed).
"""
from __future__ import annotations

import re
import secrets

from .common import load_json, save_json

RAM_TIGHT_PCT = 85.0       # a member this full is skipped if another fits
BATTERY_TIGHT_PCT = 15     # ...and so is one this empty, unless plugged in
MAX_BATCH = 16             # commands accepted in one terminal submit
MAX_MEMBERS = 8


def load_pools(path) -> dict[str, dict]:
    data = load_json(path).get("pools")
    return data if isinstance(data, dict) else {}


def save_pools(path, pools: dict[str, dict]) -> None:
    save_json(path, {"pools": pools})


def new_pool_id() -> str:
    return "pool-" + secrets.token_hex(3)


def clean_name(name) -> str:
    """Pool names are typed into monico-cmd, so keep them one word."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "").strip()).strip("-")
    return name[:32]


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def is_tight(rec: dict) -> str | None:
    """Why this member should be spared right now, or None."""
    specs = rec.get("specs") or {}
    pct = _num((specs.get("ram") or {}).get("pct"))
    if pct is not None and pct >= RAM_TIGHT_PCT:
        return f"RAM {round(pct)}% full"
    bat = specs.get("battery") or {}
    level = _num(bat.get("percent"))
    if level is not None and level <= BATTERY_TIGHT_PCT and not bat.get("plugged"):
        return f"battery {round(level)}%"
    return None


def combine(members: list[dict]) -> dict:
    """Shared specs: the online members added up. Offline members are
    left out, so the totals are what the pool can use right now."""
    ram_t = ram_u = ram_a = 0.0
    st_t = st_u = st_f = 0.0
    cores = 0
    cpu_w = cpu_sum = 0.0
    low = None
    counted = 0
    for m in members:
        specs = m.get("specs")
        if not m.get("online") or not isinstance(specs, dict):
            continue
        counted += 1
        ram = specs.get("ram") or {}
        if _num(ram.get("total_gb")):
            ram_t += ram["total_gb"]
            ram_u += _num(ram.get("used_gb")) or 0
            ram_a += _num(ram.get("available_gb")) or 0
        for s in specs.get("storage") or []:
            if _num(s.get("total_gb")) and _num(s.get("used_gb")) is not None:
                st_t += s["total_gb"]
                st_u += s["used_gb"]
                st_f += _num(s.get("free_gb")) or 0
        cpu = specs.get("cpu") or {}
        n = int(_num(cpu.get("cores")) or 0)
        cores += n
        if _num(cpu.get("pct")) is not None and n:
            cpu_w += n
            cpu_sum += cpu["pct"] * n
        bat = specs.get("battery") or {}
        if _num(bat.get("percent")) is not None and \
                (low is None or bat["percent"] < low["percent"]):
            low = {"percent": bat["percent"], "status": bat.get("status"),
                   "plugged": bool(bat.get("plugged")), "device": m.get("name")}

    def pct(used, total):
        return round(used / total * 100, 1) if total else None

    return {
        "members_counted": counted,
        "ram": {"total_gb": round(ram_t, 2), "used_gb": round(ram_u, 2),
                "available_gb": round(ram_a, 2), "pct": pct(ram_u, ram_t)},
        "storage": {"total_gb": round(st_t, 1), "used_gb": round(st_u, 1),
                    "free_gb": round(st_f, 1), "pct": pct(st_u, st_t)},
        "cpu": {"cores": cores,
                "pct": round(cpu_sum / cpu_w, 1) if cpu_w else None},
        "battery_low": low,
    }


def deal(members: list[dict], count: int, inflight: dict[str, int],
         last_used: dict[str, int], seq_start: int) -> list[str]:
    """Pick a member for each of `count` commands. Pure: takes the
    busy counts and last-used order, returns device ids (and updates
    the two dicts as it deals, so a batch spreads out)."""
    online = [m for m in members if m.get("online")]
    if not online:
        return []
    picks: list[str] = []
    for i in range(count):
        best = min(online, key=lambda m: (
            1 if is_tight(m) else 0,
            inflight.get(m["device_id"], 0),
            last_used.get(m["device_id"], 0)))
        did = best["device_id"]
        inflight[did] = inflight.get(did, 0) + 1
        last_used[did] = seq_start + i + 1
        picks.append(did)
    return picks
