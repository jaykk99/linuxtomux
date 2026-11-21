"""USB viewing and explicit re-pairing regressions.

Uses a real localhost HTTP bridge with a fake removable mount. This proves
Linux and a paired client can see the same host-side storage inventory; it
does not claim that real USB hardware or a physical Android phone was used.
"""
import json
import threading
from http.server import ThreadingHTTPServer

from bridge import cli, client, server, specs
from bridge.common import get_or_create_key


def test_usb_inventory_visible_over_authenticated_bridge(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MONICO_USB_TEST_MOUNT", str(tmp_path))
    monkeypatch.setattr(specs, "_storage_cache", (0.0, []))
    key, _ = get_or_create_key()
    previous = server.BRIDGE
    server.BRIDGE = server.Bridge(name="linux-with-usb", server_id="srv-usb-test")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:" + str(httpd.server_address[1])
    try:
        # Simulate the Termux view: it only knows the server's URL/key.
        monkeypatch.setattr(cli, "_bridge_endpoint", lambda: (url, key))
        assert cli.usb_main(["--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["servers"][0]["linux"] == "linux-with-usb"
        usbs = data["servers"][0]["usb"]
        assert any(row.get("path") == str(tmp_path)
                   and row["mounted"] for row in usbs)
        assert cli.usb_main(["list"]) == 0
        assert str(tmp_path) in capsys.readouterr().out
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.BRIDGE = previous


def test_explicit_pairing_code_replaces_stale_saved_key(tmp_path, monkeypatch):
    config = tmp_path / "client.json"
    config.write_text(json.dumps({"server_url": "http://127.0.0.1:8767",
                                  "key": "old-invalid-key",
                                  "server_id": "srv-old",
                                  "name": "phone"}))
    monkeypatch.setattr(client, "CLIENT_CONFIG", config)
    monkeypatch.setattr(client, "detect", lambda: {"device_model": "android"})
    calls = []
    monkeypatch.setattr(client, "_exchange_code",
                        lambda url, code: (calls.append((url, code)) or "new-key", "srv-new"))

    class FakeClient:
        def __init__(self, url, key, name, server_id):
            assert url == "http://127.0.0.1:8767"
            assert key == "new-key"
            assert server_id == "srv-new"
        def run(self):
            return 0

    monkeypatch.setattr(client, "Client", FakeClient)
    assert client.connect(code="4139") == 0
    assert calls == [("http://127.0.0.1:8767", "4139")]
    assert json.loads(config.read_text())["key"] == "new-key"


def test_usb_usage_validation(capsys):
    assert cli.usb_main(["--format", "unknown"]) == 2
    assert "usage" in capsys.readouterr().err
