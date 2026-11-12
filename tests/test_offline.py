"""Unit test: the offline-first control channel.

A real Termux-side Client registers, polls, and executes a command
over a literal LAN IP (127.0.0.1 here) with no DNS and no internet
involved anywhere in the path. If this passes, the register/poll/
exec path is proven to work on a direct local link.
Run: python3 -m pytest tests/ -q"""
import os
import socket
import sys
import threading
import time
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONICO_HOME", "/tmp/monico-bridge-test-home")
os.environ["MONICO_OWNER_TOKEN"] = "offline-test-token"

import bridge.client as client_mod  # noqa: E402
import bridge.server as server_mod  # noqa: E402
from bridge.common import get_or_create_key  # noqa: E402
from bridge.server import Bridge, make_handler  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_client_registers_and_executes_over_literal_ip():
    key, _is_new = get_or_create_key()
    bridge = Bridge(name="linux-a", server_id="srv-aaaa")
    prev = server_mod.BRIDGE
    server_mod.BRIDGE = bridge
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        # The phone's hosted service port must not collide with
        # anything else in this test process.
        client_mod.CLIENT_SERVICE_PORT = _free_port()
        client = client_mod.Client(f"http://127.0.0.1:{port}", key,
                                   "offline-phone")
        threading.Thread(target=client.run, daemon=True).start()
        deadline = time.time() + 20
        while client.device_id not in bridge.devices and time.time() < deadline:
            time.sleep(0.2)
        assert client.device_id in bridge.devices, "client never registered"
        dev = bridge.devices[client.device_id]
        assert dev.get("specs", {}).get("ram", {}).get("total_gb"), dev
        # The server can run a command on the phone and get the answer
        # back, still over the same direct IP link.
        res = bridge.submit(client.device_id,
                            {"type": "exec", "command": "echo from-phone",
                             "timeout_s": 10},
                            wait_s=30)
        assert res.get("exit_code") == 0, res
        assert "from-phone" in res.get("stdout", ""), res
    finally:
        httpd.shutdown()
        server_mod.BRIDGE = prev
