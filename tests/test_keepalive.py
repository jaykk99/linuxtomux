"""Connection hygiene: a POST whose body an endpoint ignores must not
desynchronise the connection.

The dashboard sends a small JSON body ({}) to the AI-engine controls, and
those handlers never read it. Closing a socket with unread request bytes
makes the kernel answer with RST, so a client can lose the response it was
about to read: that race is why /api/ai-engine/start intermittently
raised ConnectionResetError. The bridge now drains the rest of the body
before it responds, so the next request on the same connection is parsed
from a clean buffer.

Run: python3 -m pytest tests/ -q   (or python3 tests/test_keepalive.py)
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONICO_HOME", "/tmp/monico-keepalive-test-home")

from bridge import server as server_mod  # noqa: E402
from bridge.ai_gateway import AIGateway  # noqa: E402
from bridge.common import get_or_create_key  # noqa: E402
from bridge.settings import AIKeyStore  # noqa: E402


def _explicit_gateway():
    """A gateway whose engine is external: /api/ai-engine/* answers 409
    without reading the request body, which is the case that used to
    break the connection."""
    store = AIKeyStore(Path(tempfile.mkdtemp()) / "ai-keys.json")
    store.create("keepalive test")
    d = Path(tempfile.mkdtemp())
    weights = d / "model.gguf"
    weights.write_bytes(b"GGUF")
    cfg = d / "model.json"
    cfg.write_text(json.dumps({"model": "External-8B", "file": str(weights),
                               "runner": "llama-server"}))
    server_mod.BRIDGE.ai_keys = store
    server_mod.BRIDGE.ai_gateway = AIGateway(
        store, model_config=cfg, upstream_url="http://127.0.0.1:9")  # discarded
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.make_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _post(conn, path, body, key):
    payload = json.dumps(body).encode()
    conn.request("POST", path, body=payload,
                 headers={"X-Pair-Key": key,
                          "Content-Type": "application/json",
                          "Content-Length": str(len(payload))})
    resp = conn.getresponse()
    return resp.status, resp.read()


def test_unread_body_does_not_poison_the_next_request_on_the_same_connection():
    srv = _explicit_gateway()
    key, _ = get_or_create_key()
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        # 1. A POST with a body the endpoint deliberately ignores.
        status, raw = _post(conn, "/api/ai-engine/start", {}, key)
        assert status == 409, status
        payload = json.loads(raw)
        assert payload["code"] == "engine_external", payload

        # 2. The same connection must still be parseable. Before the
        #    drain, the leftover "{}" was read as a request line and the
        #    bridge answered 400 (or dropped the socket).
        status, raw = _post(conn, "/api/ai-engine/stop", {"reason": "test"}, key)
        assert status == 409, (status, raw[:200])
        assert json.loads(raw)["code"] == "engine_external"
    finally:
        conn.close()
        srv.shutdown()
        srv.server_close()


def test_a_repeated_post_never_resets_the_client():
    """The original crash was a timing race, so hammer it: twenty
    sequential POSTs that each leave a body are common for the dashboard
    polling loop."""
    srv = _explicit_gateway()
    key, _ = get_or_create_key()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        for _ in range(20):
            req = urllib.request.Request(
                base + "/api/ai-engine/start", data=b"{}", method="POST",
                headers={"X-Pair-Key": key,
                         "Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=10)
                raise AssertionError("expected 409 for an external engine")
            except urllib.error.HTTPError as exc:
                assert exc.code == 409, exc.code
                assert json.loads(exc.read())["code"] == "engine_external"
            except ConnectionResetError as exc:  # the old failure
                raise AssertionError(
                    "bridge reset the connection instead of answering: %r" % (exc,))
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_lying_content_length_does_not_hang_the_handler():
    """A client that promises more bytes than it sends must not pin the
    worker thread: the drain is bounded and the response still arrives."""
    srv = _explicit_gateway()
    key, _ = get_or_create_key()
    port = srv.server_address[1]
    sock = None
    try:
        import socket
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(
            ("POST /api/ai-engine/stop HTTP/1.1\r\n"
             "Host: 127.0.0.1\r\n"
             "X-Pair-Key: %s\r\n"
             "Content-Type: application/json\r\n"
             "Content-Length: 900\r\n\r\n{}" % key).encode())
        started = time.time()
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        # Either a real response or a closed socket is fine; hanging is not.
        assert time.time() - started < 12, "handler blocked on a short body"
        if buf:
            assert b"409" in buf.split(b"\r\n", 1)[0]
    finally:
        if sock is not None:
            sock.close()
        srv.shutdown()
        srv.server_close()
