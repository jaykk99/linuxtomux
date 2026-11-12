"""Unit tests: the internet-sharing proxy (CONNECT + absolute-URI).
Run: python3 -m pytest tests/ -q"""
import base64
import http.client
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONICO_HOME", "/tmp/monico-bridge-test-home")
import bridge.server as server_mod  # noqa: E402
from bridge.common import get_or_create_key  # noqa: E402
from bridge.server import Bridge, make_handler  # noqa: E402

KEY, _ = get_or_create_key()
TOKEN = KEY



class _Target(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"hello-proxy"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(bridge):
    prev = server_mod.BRIDGE
    server_mod.BRIDGE = bridge
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, prev


def _proxy_basic():
    return "Basic " + base64.b64encode(f"monico:{TOKEN}".encode()).decode()


def test_connect_tunnels_tcp_with_auth():
    target = ThreadingHTTPServer(("127.0.0.1", 0), _Target)
    threading.Thread(target=target.serve_forever, daemon=True).start()
    tport = target.server_address[1]
    b = Bridge(name="linux-a", server_id="srv-aaaa")
    httpd, prev = _serve(b)
    try:
        port = httpd.server_address[1]
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        s.sendall(f"CONNECT 127.0.0.1:{tport} HTTP/1.1\r\n"
                  f"Host: 127.0.0.1:{tport}\r\n"
                  f"Proxy-Authorization: {_proxy_basic()}\r\n\r\n".encode())
        resp = s.recv(4096)
        assert b"200" in resp.split(b"\r\n")[0], resp
        s.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
        data = b""
        s.settimeout(5)
        try:
            while b"hello-proxy" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        assert b"hello-proxy" in data, data
        s.close()
    finally:
        httpd.shutdown()
        target.shutdown()
        server_mod.BRIDGE = prev


def test_connect_without_auth_is_407():
    b = Bridge(name="linux-a", server_id="srv-aaaa")
    httpd, prev = _serve(b)
    try:
        port = httpd.server_address[1]
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        s.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        resp = s.recv(4096)
        assert resp.split(b"\r\n")[0].startswith(b"HTTP/1.1 407"), resp
        assert b"Proxy-Authenticate" in resp
        s.close()
    finally:
        httpd.shutdown()
        server_mod.BRIDGE = prev


def test_absolute_uri_get_is_forwarded():
    target = ThreadingHTTPServer(("127.0.0.1", 0), _Target)
    threading.Thread(target=target.serve_forever, daemon=True).start()
    tport = target.server_address[1]
    b = Bridge(name="linux-a", server_id="srv-aaaa")
    httpd, prev = _serve(b)
    try:
        port = httpd.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", f"http://127.0.0.1:{tport}/",
                     headers={"X-Pair-Key": TOKEN})
        resp = conn.getresponse()
        assert resp.status == 200, resp.status
        assert resp.read() == b"hello-proxy"
        conn.close()
        # Same request without auth is refused.
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", f"http://127.0.0.1:{tport}/")
        resp = conn.getresponse()
        assert resp.status == 407
        conn.close()
    finally:
        httpd.shutdown()
        target.shutdown()
        server_mod.BRIDGE = prev


def test_direct_routes_still_work_with_proxy_present():
    b = Bridge(name="linux-a", server_id="srv-aaaa")
    httpd, prev = _serve(b)
    try:
        port = httpd.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        assert resp.status == 200
        conn.close()
    finally:
        httpd.shutdown()
        server_mod.BRIDGE = prev
