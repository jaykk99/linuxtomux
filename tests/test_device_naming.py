"""Device naming: a phone shows up under its own real model name.

Jay has more than one phone, so nothing in the product may assume the
Termux device is a Galaxy S22. connect() already defaults the name to
detect().device_model (Android ro.product.model) with a "termux-device"
fallback; this test pins that behavior with a Pixel 8 standing in for
"not an S22", at both the connect() and the server registry layers.

Run: python3 -m pytest tests/test_device_naming.py -q"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONICO_HOME", "/tmp/monico-bridge-test-home")

from bridge import client as clientmod  # noqa: E402
from bridge.server import Bridge  # noqa: E402


def test_pixel8_connect_uses_its_own_model_name(monkeypatch):
    monkeypatch.setattr(clientmod, "detect",
                        lambda: {"device_model": "Google Pixel 8"})
    monkeypatch.setattr(clientmod, "load_json", lambda _path: {})
    monkeypatch.setenv("MONICO_SERVER", "http://127.0.0.1:18765")
    monkeypatch.setenv("MONICO_TOKEN", "test-key")
    monkeypatch.delenv("MONICO_NAME", raising=False)

    seen = {}

    class StubClient:
        def __init__(self, server_url, key, name):
            seen.update(server_url=server_url, key=key, name=name)

        def run(self):
            pass

    monkeypatch.setattr(clientmod, "Client", StubClient)
    clientmod.connect()
    assert seen["name"] == "Google Pixel 8", seen
    assert "s22" not in seen["name"].lower()


def test_pixel8_registers_and_displays_under_its_own_name():
    bridge = Bridge(name="test-server")
    bridge.register({"device_id": "dev-pixel8", "name": "Google Pixel 8",
                     "device_model": "Google Pixel 8"})
    names = [d.get("name", "") for d in bridge.device_list()]
    assert "Google Pixel 8" in names, names
    assert not any("s22" in (n or "").lower() for n in names), names


def test_connect_falls_back_to_termux_device_without_a_model(monkeypatch):
    monkeypatch.setattr(clientmod, "detect", lambda: {"device_model": ""})
    monkeypatch.setattr(clientmod, "load_json", lambda _path: {})
    monkeypatch.setenv("MONICO_SERVER", "http://127.0.0.1:18765")
    monkeypatch.setenv("MONICO_TOKEN", "test-key")
    monkeypatch.delenv("MONICO_NAME", raising=False)

    seen = {}

    class StubClient:
        def __init__(self, server_url, key, name):
            seen["name"] = name

        def run(self):
            pass

    monkeypatch.setattr(clientmod, "Client", StubClient)
    clientmod.connect()
    assert seen["name"] == "termux-device", seen
