"""Unit tests: pools (paired devices): shared specs, how the terminal
deals commands out, create/delete rules, exec through the bridge.
Run: python3 tests/test_pools.py   (or python3 -m pytest tests/ -q)"""
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONICO_HOME", "/tmp/monico-pools-test-home")

from bridge import pools as poolmod  # noqa: E402
from bridge.server import Bridge  # noqa: E402


def _specs(ram_total=8.0, ram_used=2.0, disk_total=100.0, disk_used=40.0,
           cores=4, cpu=10.0, battery=None):
    return {"ram": {"total_gb": ram_total, "used_gb": ram_used,
                    "available_gb": ram_total - ram_used,
                    "pct": round(ram_used / ram_total * 100, 1)},
            "storage": [{"label": "Internal", "kind": "internal",
                         "mounted": True, "total_gb": disk_total,
                         "used_gb": disk_used,
                         "free_gb": disk_total - disk_used,
                         "pct": round(disk_used / disk_total * 100, 1)}],
            "cpu": {"cores": cores, "pct": cpu}, "battery": battery}


def _m(did, online=True, **kw):
    return {"device_id": did, "name": did, "online": online, "specs": _specs(**kw)}


def _bridge():
    b = Bridge(name="linux-a", server_id="srv-aaaa")
    b.pools = {}
    b.pools_file = b.pools_file.parent / "pools-test.json"
    if b.pools_file.exists():
        b.pools_file.unlink()
    return b


def _add(b, did, name, specs=None):
    b.register({"device_id": did, "name": name, "platform": "linux",
                "kind": "device", "hw": {}, "model": {}, "services": [],
                "specs": specs or _specs()})


def test_combined_adds_up_online_members_only():
    c = poolmod.combine([
        _m("a", ram_total=8, ram_used=2, disk_total=100, disk_used=40, cores=4, cpu=10),
        _m("b", ram_total=4, ram_used=3, disk_total=50, disk_used=10, cores=8, cpu=40,
           battery={"percent": 30, "status": "Discharging", "plugged": False}),
        _m("c", online=False, ram_total=64),
    ])
    assert c["members_counted"] == 2
    assert c["ram"] == {"total_gb": 12, "used_gb": 5, "available_gb": 7, "pct": 41.7}
    assert c["storage"]["total_gb"] == 150 and c["storage"]["free_gb"] == 100
    assert c["cpu"] == {"cores": 12, "pct": 30.0}       # weighted by cores
    assert c["battery_low"]["percent"] == 30 and c["battery_low"]["device"] == "b"


def test_deal_is_half_and_half_on_two_healthy_devices():
    members = [_m("a"), _m("b")]
    inflight, last = {}, {}
    picks = poolmod.deal(members, 4, inflight, last, 0)
    assert sorted(picks) == ["a", "a", "b", "b"], picks
    # one at a time (each finishes before the next): takes turns
    inflight, last, seq, turns = {}, {}, 0, []
    for _ in range(4):
        did = poolmod.deal(members, 1, inflight, last, seq)[0]
        seq += 1
        inflight[did] -= 1
        turns.append(did)
    assert turns in (["a", "b", "a", "b"], ["b", "a", "b", "a"]), turns


def test_deal_spares_the_device_that_is_running_out():
    full = _m("a", ram_total=8, ram_used=7.5)           # 94% RAM
    assert poolmod.is_tight(full)
    picks = poolmod.deal([full, _m("b")], 3, {}, {}, 0)
    assert picks == ["b", "b", "b"], picks
    dying = _m("p", battery={"percent": 9, "status": "Discharging", "plugged": False})
    assert poolmod.is_tight(dying) == "battery 9%"
    charging = _m("p", battery={"percent": 9, "status": "Charging", "plugged": True})
    assert poolmod.is_tight(charging) is None
    # both tight: still runs somewhere rather than refusing
    assert len(poolmod.deal([full, dying], 2, {}, {}, 0)) == 2
    # offline members never get work; nobody online -> nothing dealt
    assert poolmod.deal([_m("a", online=False), _m("b")], 2, {}, {}, 0) == ["b", "b"]
    assert poolmod.deal([_m("a", online=False)], 1, {}, {}, 0) == []


def test_pool_create_rules_and_persistence():
    b = _bridge()
    _add(b, "dev-1", "pixel")
    _add(b, "dev-2", "thinkpad")
    assert "error" in b.pool_create("duo", ["dev-1"])                 # needs 2
    assert "error" in b.pool_create("duo", ["dev-1", "dev-nope"])     # unknown
    assert "error" in b.pool_create("pixel", ["dev-1", "dev-2"])      # device name
    assert "error" in b.pool_create("phone", ["dev-1", "dev-2"])      # reserved
    res = b.pool_create("my duo!", ["dev-1", "dev-2", "dev-1"])
    assert res["ok"] and res["pool"]["name"] == "my-duo"
    assert [m["name"] for m in res["pool"]["members"]] == ["pixel", "thinkpad"]
    assert res["pool"]["combined"]["ram"]["total_gb"] == 16
    assert "error" in b.pool_create("my-duo", ["dev-1", "dev-2"])     # duplicate
    pid = res["pool"]["id"]
    assert poolmod.load_pools(b.pools_file)[pid]["members"] == ["dev-1", "dev-2"]
    assert b.pool_find("my-duo") == pid and b.pool_find(pid) == pid
    assert b.pool_delete(pid)["ok"] and b.pool_list() == []
    assert poolmod.load_pools(b.pools_file) == {}


def test_pool_exec_splits_and_runs_on_all():
    b = _bridge()
    _add(b, "dev-1", "pixel")
    _add(b, "dev-2", "thinkpad")
    pid = b.pool_create("duo", ["dev-1", "dev-2"])["pool"]["id"]
    ran = []

    def fake_submit(target, job, wait_s):
        ran.append((target, job["command"]))
        return {"id": "x", "stdout": f"{target}:{job['command']}\n",
                "stderr": "", "exit_code": 0, "duration_ms": 1}
    b.submit = fake_submit
    res = b.pool_exec("duo", ["echo 1", "echo 2", "echo 3", "echo 4"])
    assert res["mode"] == "split" and len(res["results"]) == 4
    assert [r["command"] for r in res["results"]] == ["echo 1", "echo 2", "echo 3", "echo 4"]
    by_dev = sorted(r["device"] for r in res["results"])
    assert by_dev == ["pixel", "pixel", "thinkpad", "thinkpad"], by_dev
    assert all(v == 0 for v in b.inflight.values()), b.inflight
    ran.clear()
    res = b.pool_exec(pid, ["uptime"], mode="all")
    assert sorted(t for t, _c in ran) == ["dev-1", "dev-2"]
    assert len(res["results"]) == 2
    assert "error" in b.pool_exec("duo", [])
    assert "error" in b.pool_exec("nope", ["ls"])
    assert "error" in b.pool_exec("duo", ["x"] * (poolmod.MAX_BATCH + 1))


def test_pool_exec_refuses_when_everyone_is_offline():
    b = _bridge()
    _add(b, "dev-1", "pixel")
    _add(b, "dev-2", "thinkpad")
    b.pool_create("duo", ["dev-1", "dev-2"])
    for dev in b.devices.values():
        dev["last_seen"] -= 3600
    view = b.pool_list()[0]
    assert view["online"] == 0 and view["combined"]["members_counted"] == 0
    assert "online" in b.pool_exec("duo", ["ls"])["error"]


if __name__ == "__main__":
    shutil.rmtree("/tmp/monico-pools-test-home", ignore_errors=True)
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("all pool tests passed")
