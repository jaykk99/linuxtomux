"""Unit tests: the shared terminal (POST /api/exec-all).
Run: python3 -m pytest tests/ -q"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONICO_HOME", "/tmp/monico-bridge-test-home")
os.environ["MONICO_OWNER_TOKEN"] = "execall-test-token"

from bridge.server import Bridge  # noqa: E402


def test_exec_all_runs_on_this_server():
    b = Bridge(name="linux-a", server_id="srv-aaaa")
    results = b.exec_all("echo hello-all", timeout_s=10)
    assert len(results) == 1
    res = results[0]
    assert res["device_id"] == "srv-aaaa"
    assert res["exit_code"] == 0
    assert "hello-all" in res["stdout"]


def test_exec_all_blocked_command_never_runs():
    b = Bridge(name="linux-a", server_id="srv-aaaa")
    results = b.exec_all("rm -rf /", timeout_s=10)
    assert len(results) == 1
    assert results[0].get("blocked") is True
    assert results[0].get("error")


def test_exec_all_skips_offline_devices():
    b = Bridge(name="linux-a", server_id="srv-aaaa")
    b.register({"device_id": "dev-gone", "name": "gone-phone",
                "platform": "android-termux", "kind": "device", "hw": {}})
    b.devices["dev-gone"]["last_seen"] = time.time() - 3600
    results = b.exec_all("echo hi", timeout_s=10)
    assert [r["device_id"] for r in results] == ["srv-aaaa"]


def test_exec_all_fans_out_to_a_live_device():
    b = Bridge(name="linux-a", server_id="srv-aaaa")
    b.register({"device_id": "dev-phone", "name": "phone",
                "platform": "android-termux", "kind": "device", "hw": {}})

    def drainer():
        job = b.queues["dev-phone"].get(timeout=15)
        b.complete(job["id"], {"stdout": "phone-output\n", "stderr": "",
                               "exit_code": 0, "blocked": False,
                               "duration_ms": 1})

    threading.Thread(target=drainer, daemon=True).start()
    results = b.exec_all("echo hi", timeout_s=10)
    by_id = {r["device_id"]: r for r in results}
    assert set(by_id) == {"srv-aaaa", "dev-phone"}
    assert "phone-output" in by_id["dev-phone"]["stdout"]
    assert "hi" in by_id["srv-aaaa"]["stdout"]
