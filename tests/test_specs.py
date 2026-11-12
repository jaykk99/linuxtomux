"""Unit tests: live specs payload, and how specs travel through the
registry (direct report, peer sync, staleness).
Run: python3 tests/test_specs.py   (or python3 -m pytest tests/ -q)"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONICO_HOME", "/tmp/monico-specs-test-home")

from bridge import specs as specs_mod  # noqa: E402
from bridge.server import Bridge  # noqa: E402
from bridge.specs import clean_specs, live_specs  # noqa: E402

PHONE_SPECS = {
    "at": 1.0, "os": {"name": "Android 14", "kernel": "5.10"},
    "cpu": {"model": "taro", "cores": 8, "arch": "aarch64",
            "pct": None, "load1": None},
    "ram": {"total_gb": 7.4, "used_gb": 5.0, "available_gb": 2.4, "pct": 67.6},
    "storage": [{"label": "Internal", "kind": "internal", "path": "/data",
                 "mounted": True, "total_gb": 128.0, "used_gb": 90.0,
                 "free_gb": 38.0, "pct": 70.3}],
    "uptime_s": 3600,
    "battery": {"percent": 81, "status": "Charging", "plugged": True,
                "temp_c": 31.0, "source": "termux-api"},
}


def _phone(bridge, did="dev-p1", name="pixel-8", specs=None):
    info = {"device_id": did, "name": name, "platform": "android-termux",
            "kind": "device", "hw": {"platform": "android-termux"},
            "model": {"installed": False}, "services": []}
    if specs is not None:
        info["specs"] = specs
    bridge.register(info)


def _by_name(devices):
    return {d["name"]: d for d in devices}


def test_live_specs_shape_on_this_machine():
    s = live_specs()
    assert set(s) >= {"at", "os", "cpu", "ram", "storage", "uptime_s", "battery"}
    assert s["os"]["name"] and s["cpu"]["cores"] >= 1
    ram = s["ram"]
    assert ram["total_gb"] > 0 and 0 <= ram["pct"] <= 100
    assert abs(ram["used_gb"] + ram["available_gb"] - ram["total_gb"]) < 0.05
    internal = s["storage"][0]
    assert internal["kind"] == "internal" and 0 <= internal["pct"] <= 100
    assert abs(internal["used_gb"] + internal["free_gb"]
               - internal["total_gb"]) < 0.25
    assert s["uptime_s"] is None or s["uptime_s"] >= 0
    if s["cpu"]["pct"] is not None:
        assert 0 <= s["cpu"]["pct"] <= 100


def test_cpu_pct_is_none_when_proc_stat_is_hidden():
    # Android blocks /proc/stat for apps: report None, never a guess.
    real = specs_mod._read_cpu_times
    specs_mod._read_cpu_times = lambda: None
    try:
        assert specs_mod._cpu_pct() is None
    finally:
        specs_mod._read_cpu_times = real


def test_termux_battery_parse():
    real_which, real_run = specs_mod.shutil.which, specs_mod.subprocess.run

    class Out:
        stdout = ('{"health":"GOOD","percentage":42,"plugged":"UNPLUGGED",'
                  '"status":"DISCHARGING","temperature":29.3}')
    specs_mod.shutil.which = lambda _n: "/usr/bin/termux-battery-status"
    specs_mod.subprocess.run = lambda *a, **k: Out()
    try:
        bat = specs_mod._battery_termux()
    finally:
        specs_mod.shutil.which, specs_mod.subprocess.run = real_which, real_run
    assert bat == {"percent": 42, "status": "Discharging", "plugged": False,
                   "temp_c": 29.3, "source": "termux-api"}, bat


def test_clean_specs_rejects_junk():
    assert clean_specs("nope") is None
    assert clean_specs(None) is None
    assert clean_specs({"x": "y" * 20000}) is None
    assert clean_specs(PHONE_SPECS) == PHONE_SPECS


def test_server_reports_its_own_specs():
    a = Bridge(name="linux-a", server_id="srv-aaaa")
    me = _by_name(a.known())["linux-a"]
    assert me["specs"]["ram"]["total_gb"] > 0
    assert me["specs_age_s"] is not None and me["specs_age_s"] <= 5


def test_device_report_lands_on_its_card():
    a = Bridge(name="linux-a", server_id="srv-aaaa")
    _phone(a)
    assert _by_name(a.known())["pixel-8"]["specs"] is None
    assert a.set_specs("dev-p1", PHONE_SPECS) is True
    rec = _by_name(a.known())["pixel-8"]
    assert rec["specs"]["battery"]["percent"] == 81
    assert rec["specs_age_s"] == 0 and rec["online"] is True
    # unknown device (server restarted) and junk payloads are refused
    assert a.set_specs("dev-nobody", PHONE_SPECS) is False
    assert a.set_specs("dev-p1", "junk") is False


def test_register_carries_specs():
    a = Bridge(name="linux-a", server_id="srv-aaaa")
    _phone(a, specs=PHONE_SPECS)
    rec = _by_name(a.known())["pixel-8"]
    assert rec["specs"]["ram"]["pct"] == 67.6 and rec["specs_age_s"] == 0


def test_specs_cross_the_mesh_both_ways():
    a = Bridge(name="linux-a", server_id="srv-aaaa")
    b = Bridge(name="linux-b", server_id="srv-bbbb")
    _phone(b, specs=PHONE_SPECS)
    # b joined a: b is a direct device on a, then syncs.
    a.register({"device_id": b.server_id, "name": b.name, "platform": "linux",
                "kind": "server", "hw": {}, "model": {}, "services": []})
    on_a = _by_name(a.apply_sync(b.server_id, b.name, b.known()))
    assert on_a["pixel-8"]["specs"]["battery"]["percent"] == 81
    assert on_a["pixel-8"]["via"] == "linux-b"
    # the joined server's own card on a gets b's live specs too
    assert on_a["linux-b"]["specs"]["ram"]["total_gb"] > 0
    assert on_a["linux-b"]["specs_age_s"] is not None
    on_b = _by_name(b.apply_sync(a.server_id, a.name, a.known()))
    assert on_b["linux-a"]["specs"]["ram"]["total_gb"] > 0
    assert on_b["pixel-8"]["local"] is True


def test_remote_specs_age_grows_with_the_claim():
    a = Bridge(name="linux-a", server_id="srv-aaaa")
    b = Bridge(name="linux-b", server_id="srv-bbbb")
    _phone(b, specs=PHONE_SPECS)
    a.apply_sync(b.server_id, b.name, b.known())
    for claim in a.claims.values():
        claim["at"] -= 120          # the peer went quiet two minutes ago
    rec = _by_name(a.known())["pixel-8"]
    assert rec["online"] is False            # aged out, as before
    assert rec["specs_age_s"] >= 120         # and the card can say so
    assert rec["specs"]["battery"]["percent"] == 81   # last known reading


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("all specs tests passed")
