"""Unit tests: the dashboard PIN gate.
Run: python3 -m pytest tests/ -q

The real PIN never appears in this repo (not even here): tests
monkeypatch the stored hash to the hash of a throwaway PIN.
"""
import hashlib
import http.client
import os
import sys
import threading
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONICO_HOME", "/tmp/monico-bridge-test-home")
import bridge.server as server_mod  # noqa: E402
from bridge.common import get_or_create_key  # noqa: E402
from bridge.server import Bridge, make_handler  # noqa: E402

KEY, _ = get_or_create_key()

TEST_PIN = "0000"
TEST_PIN_HASH = hashlib.sha256(TEST_PIN.encode()).hexdigest()


def _serve(bridge):
    prev = server_mod.BRIDGE
    server_mod.BRIDGE = bridge
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, prev


def _req(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    out_headers = dict(resp.getheaders())
    conn.close()
    return resp.status, data, out_headers


def test_pin_hash_is_a_hash_not_the_pin():
    assert len(server_mod.PIN_HASH) == 64
    assert all(c in "0123456789abcdef" for c in server_mod.PIN_HASH)
    # A 6-digit numeric PIN can never equal its own 64-hex hash.
    assert server_mod.PIN_HASH != TEST_PIN


def test_pin_flow(monkeypatch):
    monkeypatch.setattr(server_mod, "PIN_HASH", TEST_PIN_HASH)
    b = Bridge(name="linux-a", server_id="srv-aaaa")
    httpd, prev = _serve(b)
    try:
        port = httpd.server_address[1]
        # No session: the dashboard shows the PIN page, not the dashboard.
        code, body, _ = _req(port, "GET", "/dashboard")
        assert code == 200
        assert b"Enter PIN" in body
        assert b"Shared terminal" not in body
        # Wrong PIN: 403, no session cookie.
        code, body, headers = _req(
            port, "POST", "/pin-login", body="pin=9999",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert code == 403
        assert "monico_pin" not in headers.get("Set-Cookie", "")
        # Right PIN: 303 to /dashboard with a session cookie.
        code, body, headers = _req(
            port, "POST", "/pin-login", body=f"pin={TEST_PIN}",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert code == 303
        cookie = headers.get("Set-Cookie", "")
        assert cookie.startswith("monico_pin=")
        token = cookie.split(";", 1)[0].split("=", 1)[1]
        assert token and token != TEST_PIN
        # Session cookie opens the dashboard and the dashboard API.
        code, body, _ = _req(port, "GET", "/dashboard",
                             headers={"Cookie": f"monico_pin={token}"})
        assert code == 200
        assert b"Shared terminal" in body
        code, body, _ = _req(port, "GET", "/api/devices",
                             headers={"Cookie": f"monico_pin={token}"})
        assert code == 200
        # PIN session does NOT open the pairing-key-only routes.
        code, _body, _ = _req(port, "GET", "/api/apps",
                              headers={"Cookie": f"monico_pin={token}"})
        assert code == 401
    finally:
        httpd.shutdown()
        server_mod.BRIDGE = prev


def test_specs_redirects_to_dashboard():
    b = Bridge(name="linux-a", server_id="srv-aaaa")
    httpd, prev = _serve(b)
    try:
        port = httpd.server_address[1]
        code, _body, headers = _req(port, "GET", "/specs")
        assert code == 302
        assert headers.get("Location") == "/dashboard"
    finally:
        httpd.shutdown()
        server_mod.BRIDGE = prev


def test_pairing_key_still_opens_dashboard_api():
    b = Bridge(name="linux-a", server_id="srv-aaaa")
    httpd, prev = _serve(b)
    try:
        port = httpd.server_address[1]
        code, _body, _ = _req(port, "GET", "/api/devices",
                              headers={"X-Pair-Key": KEY})
        assert code == 200
    finally:
        httpd.shutdown()
        server_mod.BRIDGE = prev
