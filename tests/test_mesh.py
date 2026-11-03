"""Unit tests: mesh registry sync, relay routing, name targeting.
Run: python3 -m pytest tests/ -q   (or python3 tests/test_mesh.py)"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONICO_HOME", "/tmp/monico-mesh-test-home")

from bridge.cli import resolve_from_list  # noqa: E402
from bridge.server import Bridge  # noqa: E402

FAKE_HW = {"platform": "linux", "hostname": "x", "arch": "x86_64",
           "cpu_cores": 2, "ram_total_gb": 4.0, "ram_available_gb": 2.0,
           "disk_free_gb": 10.0, "gpu": "none", "device_model": ""}


def _bridges():
    a = Bridge(name="linux-a", server_id="srv-aaaa")
    b = Bridge(name="linux-b", server_id="srv-bbbb")
    return a, b


def _register_phone(bridge, did="dev-t2", name="termux-t2"):
    bridge.register({"device_id": did, "name": name,
                     "platform": "android-termux", "kind": "device",
                     "hw": dict(FAKE_HW, platform="android-termux"),
                     "model": {"installed": False},
                     "services": ["Device status page"]})


def _by_name(devices):
    return {d["name"]: d for d in devices}


def test_registry_sync_merges_both_ways():
    a, b = _bridges()
    _register_phone(b)
    merged_a = a.apply_sync(b.server_id, b.name, b.known())
    names = _by_name(merged_a)
    assert set(names) == {"linux-a", "linux-b", "termux-t2"}, names.keys()
    t2 = names["termux-t2"]
    assert t2["local"] is False and t2["origin"] == "srv-bbbb"
    assert t2["online"] is True
    assert names["linux-b"]["local"] is False
    assert names["linux-a"]["self"] is True
    merged_b = b.apply_sync(a.server_id, a.name, a.known())
    names_b = _by_name(merged_b)
    assert set(names_b) == {"linux-a", "linux-b", "termux-t2"}, names_b.keys()
    assert names_b["linux-a"]["local"] is False
    assert names_b["termux-t2"]["local"] is True


def test_sync_ignores_own_origin_echo():
    a, _b = _bridges()
    ghost = {"device_id": "dev-ghost", "name": "ghost", "platform": "linux",
             "kind": "device", "online": True, "hw": {}, "model": {},
             "hosted": [], "origin": a.server_id, "origin_name": "linux-a"}
    merged = a.apply_sync("srv-bbbb", "linux-b", [ghost])
    assert "ghost" not in _by_name(merged)
    assert "dev-ghost" not in {d["device_id"] for d in a.device_list()}


def test_sync_replacement_drops_departed_devices():
    a, b = _bridges()
    _register_phone(b)
    a.apply_sync(b.server_id, b.name, b.known())
    assert "termux-t2" in _by_name(a.device_list())
    # B's next sync no longer lists the phone (it left B): the claim
    # is replaced wholesale, so A stops listing it too.
    a.apply_sync(b.server_id, b.name, [b._self_record()])
    assert "termux-t2" not in _by_name(a.device_list())
    assert "linux-b" in _by_name(a.device_list())


def test_remote_devices_age_to_offline():
    a, b = _bridges()
    _register_phone(b)
    a.apply_sync(b.server_id, b.name, b.known())
    assert _by_name(a.device_list())["termux-t2"]["online"] is True
    with a.lock:
        for claim in a.claims.values():
            claim["at"] -= 100  # peer went silent 100s ago
    devs = _by_name(a.device_list())
    assert devs["termux-t2"]["online"] is False  # listed, but offline
    assert devs["linux-b"]["online"] is False
    assert devs["linux-a"]["online"] is True  # local view unaffected


def test_relay_submit_reaches_remote_device():
    a, b = _bridges()
    _register_phone(b)
    # B registers on A (gets a job queue) and syncs its registry.
    a.register({"device_id": b.server_id, "name": b.name,
                "platform": "linux", "kind": "server", "hw": FAKE_HW,
                "model": {"installed": False}, "services": []})
    a.apply_sync(b.server_id, b.name, b.known())

    seen = {}

    def fake_peer():
        job = a.queues[b.server_id].get(timeout=10)
        seen["job"] = job
        a.complete(job["id"], {"id": job["id"], "stdout": "RELAY_OK\n",
                               "stderr": "", "exit_code": 0})

    th = threading.Thread(target=fake_peer, daemon=True)
    th.start()
    res = a.submit("dev-t2", {"type": "exec", "command": "echo RELAY_OK",
                              "timeout_s": 5}, wait_s=5)
    th.join(timeout=10)
    assert seen["job"]["type"] == "relay" and seen["job"]["target"] == "dev-t2"
    assert res["stdout"] == "RELAY_OK\n" and res["exit_code"] == 0


def test_local_and_self_submit_still_work():
    a, _b = _bridges()
    res = a.submit("linux", {"type": "exec", "command": "echo SELF_OK"},
                   wait_s=5)
    assert "SELF_OK" in res["stdout"] and res["exit_code"] == 0
    _register_phone(a, did="dev-t1", name="termux-t1")

    def fake_client():
        job = a.queues["dev-t1"].get(timeout=10)
        a.complete(job["id"], {"id": job["id"], "stdout": "LOCAL_OK\n",
                               "stderr": "", "exit_code": 0})

    th = threading.Thread(target=fake_client, daemon=True)
    th.start()
    res = a.submit("dev-t1", {"type": "exec", "command": "echo LOCAL_OK",
                              "timeout_s": 5}, wait_s=5)
    th.join(timeout=10)
    assert res["stdout"] == "LOCAL_OK\n"
    res = a.submit("dev-nope", {"type": "exec", "command": "true"}, wait_s=1)
    assert "unknown device" in res["error"]


def _dev(did, name, platform, online=True, kind="device"):
    return {"device_id": did, "name": name, "platform": platform,
            "online": online, "kind": kind}


def test_name_resolution():
    devs = [_dev("srv-aaaa", "linux-a", "linux", kind="server"),
            _dev("srv-bbbb", "linux-b", "linux", kind="server"),
            _dev("dev-t1", "termux-t1", "android-termux"),
            _dev("dev-t2", "termux-t2", "android-termux")]
    assert resolve_from_list(devs, "linux") == "linux"
    assert resolve_from_list(devs, "dev-t2") == "dev-t2"
    assert resolve_from_list(devs, "termux-t1") == "dev-t1"
    assert resolve_from_list(devs, "linux-b") == "srv-bbbb"
    # "phone" = a client device, never a mesh server (v1 bug).
    assert resolve_from_list(devs, "phone") in ("dev-t1", "dev-t2")
    try:
        resolve_from_list(devs, "nope")
        raise AssertionError("expected SystemExit")
    except SystemExit:
        pass


def test_phone_shortcut_skips_servers_and_finds_linux_clients():
    # Only servers + one Linux box that joined purely as a client:
    # "phone" must land on the client, never on a server.
    devs = [_dev("srv-aaaa", "linux-a", "linux", kind="server"),
            _dev("srv-bbbb", "linux-b", "linux", kind="server"),
            _dev("dev-l1", "linux-client", "linux", kind="device")]
    assert resolve_from_list(devs, "phone") == "dev-l1"
    try:
        resolve_from_list(devs[:2], "phone")
        raise AssertionError("expected SystemExit")
    except SystemExit:
        pass


def test_name_resolution_duplicates():
    devs = [_dev("dev-1", "phone-x", "android-termux", online=True),
            _dev("dev-2", "phone-x", "android-termux", online=False)]
    # one online twin wins
    assert resolve_from_list(devs, "phone-x") == "dev-1"
    devs[0]["online"] = False
    try:
        resolve_from_list(devs, "phone-x")
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "dev-1" in str(exc) and "dev-2" in str(exc)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
