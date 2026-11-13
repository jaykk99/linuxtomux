"""Unit tests: OpenAI-compatible gateway (key auth, model listing,
upstream proxying, SSE streaming, honest 503s) and its /v1 endpoints.
Run: python3 -m pytest tests/ -q   (or python3 tests/test_ai_gateway.py)
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONICO_HOME", "/tmp/monico-ai-test-home")
os.environ.pop("MONICO_LLAMA_URL", None)

from bridge import ai_gateway as gw_mod  # noqa: E402
from bridge import server as server_mod  # noqa: E402
from bridge.ai_gateway import AIGateway, GatewayError  # noqa: E402
from bridge.common import get_or_create_key  # noqa: E402
from bridge.settings import AIKeyStore  # noqa: E402


# ---------------------------------------------------------------- fixtures
def _keys():
    store = AIKeyStore(Path(tempfile.mkdtemp()) / "ai-keys.json")
    created = store.create("test agent")
    return store, created["key"]


def _model_config(model="Dolphin3.0-Llama3.1-8B"):
    d = Path(tempfile.mkdtemp())
    weights = d / "model.gguf"
    weights.write_bytes(b"GGUF")
    cfg = d / "model.json"
    cfg.write_text(json.dumps({"model": model, "file": str(weights),
                               "runner": "llama-server"}))
    return cfg


def _upstream(health=True):
    """A fake OpenAI-compatible server: /health (optional, for older
    builds), /v1/models, chat JSON, SSE, and errors."""
    records = []

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/v1/models":
                body = json.dumps({"object": "list", "data": [
                    {"id": "fake-model", "object": "model",
                     "owned_by": "fake"}]}).encode()
            elif self.path == "/health" and not health:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            else:
                body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
            records.append({"path": self.path, "payload": payload})
            if payload.get("fail"):
                body = json.dumps({"error": {"message": "model exploded",
                                             "type": "api_error"}}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if payload.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for chunk in (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
                              b'data: [DONE]\n\n'):
                    self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                return
            body = json.dumps({
                "object": "chat.completion", "model": payload.get("model"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "ok"}}],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}", records


# ------------------------------------------------------------------- unit
def test_models_lists_the_installed_model():
    store, _key = _keys()
    gw = AIGateway(store, model_config=_model_config())
    ids = [m["id"] for m in gw.models()]
    assert "Dolphin3.0-Llama3.1-8B" in ids, ids
    assert gw_mod.MODEL_ALIAS in ids, ids


def test_external_server_models_and_v1_base_url():
    # Clients paste both the root and the /v1 base URL; both must work,
    # and an external server's own models must stay visible.
    store, _key = _keys()
    httpd, url, records = _upstream()
    try:
        gw = AIGateway(store, model_config=_model_config(),
                       upstream_url=url + "/v1")
        up = gw.post("/v1/chat/completions", {"messages": []})
        up.close()
        assert records[0]["path"] == "/v1/chat/completions", records
        ids = [m["id"] for m in gw.models()]
        assert "fake-model" in ids, ids
        assert "Dolphin3.0-Llama3.1-8B" in ids, ids
    finally:
        httpd.shutdown()


def test_older_llama_cpp_without_health_is_still_reachable():
    # Old llama.cpp builds have no /health; /v1/models is the signal.
    store, _key = _keys()
    httpd, url, _records = _upstream(health=False)
    try:
        gw = AIGateway(store, model_config=_model_config(), upstream_url=url)
        assert gw._healthy(url) is True
        assert gw.status()["reachable"] is True
        assert gw.status()["state"] == "external"
    finally:
        httpd.shutdown()


def test_existing_gguf_can_be_served_without_reinstalling():
    # Old phones and pre-existing setups already have a model file;
    # MONICO_MODEL_FILE points at it and no download is needed.
    store, _key = _keys()
    weights = Path(tempfile.mkdtemp()) / "my-model.Q4.gguf"
    weights.write_bytes(b"GGUF")
    old = os.environ.get("MONICO_MODEL_FILE")
    os.environ["MONICO_MODEL_FILE"] = str(weights)
    try:
        gw = AIGateway(store, model_config=Path(tempfile.mkdtemp()) / "none.json")
        cfg = gw.installed()
        assert cfg and cfg["file"] == str(weights), cfg
        assert cfg["source"] == "MONICO_MODEL_FILE"
        ids = [m["id"] for m in gw.models()]
        assert "my-model.Q4" in ids, ids
    finally:
        if old is None:
            os.environ.pop("MONICO_MODEL_FILE", None)
        else:
            os.environ["MONICO_MODEL_FILE"] = old


def test_no_model_means_no_model_list():
    store, _key = _keys()
    gw = AIGateway(store, model_config=Path(tempfile.mkdtemp()) / "none.json")
    assert gw.models() == []


def test_auth_accepts_only_created_keys():
    store, key = _keys()
    gw = AIGateway(store, model_config=_model_config())
    assert gw.authenticate(f"Bearer {key}")["name"] == "test agent"
    assert gw.authenticate("Bearer mk-ai-not-real") is None
    assert gw.authenticate(f"Basic {key}") is None
    assert gw.authenticate(None) is None
    assert gw.authenticate("") is None


def test_chat_completion_is_forwarded():
    store, _key = _keys()
    httpd, url, records = _upstream()
    try:
        gw = AIGateway(store, model_config=_model_config(), upstream_url=url)
        up = gw.post("/v1/chat/completions",
                     {"model": "monico-local",
                      "messages": [{"role": "user", "content": "hello"}]})
        try:
            body = json.loads(up.read())
        finally:
            up.close()
        assert up.status == 200
        assert body["choices"][0]["message"]["content"] == "ok"
        assert records[0]["path"] == "/v1/chat/completions"
        assert records[0]["payload"]["messages"][0]["content"] == "hello"
    finally:
        httpd.shutdown()


def test_upstream_error_is_passed_through():
    store, _key = _keys()
    httpd, url, _records = _upstream()
    try:
        gw = AIGateway(store, model_config=_model_config(), upstream_url=url)
        try:
            gw.post("/v1/chat/completions", {"fail": True})
            raise AssertionError("expected a GatewayError")
        except GatewayError as exc:
            assert exc.status == 503
            assert exc.payload["error"]["message"] == "model exploded"
    finally:
        httpd.shutdown()


def test_streaming_lines_are_forwarded():
    store, _key = _keys()
    httpd, url, _records = _upstream()
    try:
        gw = AIGateway(store, model_config=_model_config(), upstream_url=url)
        up = gw.post("/v1/chat/completions",
                     {"model": "monico-local", "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]})
        try:
            lines = b"".join(up.iter_lines()).decode()
        finally:
            up.close()
        assert '"content":"hi"' in lines, lines
        assert "data: [DONE]" in lines, lines
    finally:
        httpd.shutdown()


def test_no_model_installed_is_a_503_not_a_guess():
    store, _key = _keys()
    gw = AIGateway(store, model_config=Path(tempfile.mkdtemp()) / "none.json")
    gw._healthy = lambda url: False   # nothing answers on the default port
    try:
        gw.target()
        raise AssertionError("expected a GatewayError")
    except GatewayError as exc:
        assert exc.status == 503
        assert exc.payload["error"]["code"] == "model_not_installed"
        assert "monico-model install" in exc.payload["error"]["message"]


_FAKE_SERVER = '''#!/usr/bin/env python3
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_GET(self):
        b = b'{"status":"ok"}'
        self.send_response(200); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        b = json.dumps({"object": "chat.completion", "choices": [{"message": {"content": "started"}}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def test_engine_is_started_from_the_installed_model():
    # The managed path: no MONICO_LLAMA_URL, nothing on 8080, a model on
    # disk and a llama-server binary on PATH. The gateway starts it and
    # the request goes through. Skipped when 8080 is already occupied.
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8080)) == 0:
            print("  (skipped: something already listens on 8080)")
            return
    store, _key = _keys()
    bindir = Path(tempfile.mkdtemp())
    script = bindir / "llama-server"
    script.write_text(_FAKE_SERVER)
    script.chmod(0o755)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(bindir) + os.pathsep + old_path
    gw = AIGateway(store, model_config=_model_config())
    try:
        assert gw.target() == "http://127.0.0.1:8080"
        up = gw.post("/v1/chat/completions", {"messages": []})
        try:
            body = json.loads(up.read())
        finally:
            up.close()
        assert body["choices"][0]["message"]["content"] == "started"
    finally:
        os.environ["PATH"] = old_path
        if gw._proc is not None:
            gw._proc.terminate()
            try:
                gw._proc.wait(timeout=5)
            except Exception:
                gw._proc.kill()


# ---------------------------------------------------------- engine control
def test_status_reports_model_and_stopped_state():
    store, _key = _keys()
    gw = AIGateway(store, model_config=_model_config())
    gw._healthy = lambda url: False
    st = gw.status()
    assert st["installed"] is True
    assert st["model"] == "Dolphin3.0-Llama3.1-8B"
    assert st["state"] == "stopped" and st["managed"] is True
    assert st["can_stop"] is False and st["pid"] is None
    assert st["can_start"] is st["has_llama_server"]
    if not st["has_llama_server"]:
        assert "llama-server" in st["hint"]


def test_external_engine_cannot_be_started_or_stopped():
    store, _key = _keys()
    httpd, url, _records = _upstream()
    try:
        gw = AIGateway(store, model_config=_model_config(), upstream_url=url)
        st = gw.status()
        assert st["state"] == "external" and st["managed"] is False
        assert st["can_start"] is False and st["can_stop"] is False
        assert "MONICO_LLAMA_URL" in st["hint"]
        assert gw.start()["code"] == "engine_external"
        assert gw.stop()["code"] == "engine_external"
    finally:
        httpd.shutdown()


def test_start_then_stop_the_managed_engine():
    # Same managed path as the request-time start, but explicit: start()
    # returns before the model is ready, status() shows Starting, then
    # Running, and stop() kills only this bridge's process.
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8080)) == 0:
            print("  (skipped: something already listens on 8080)")
            return
    store, _key = _keys()
    bindir = Path(tempfile.mkdtemp())
    script = bindir / "llama-server"
    script.write_text(_FAKE_SERVER)
    script.chmod(0o755)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(bindir) + os.pathsep + old_path
    gw = AIGateway(store, model_config=_model_config())
    try:
        res = gw.start()
        assert res["ok"] is True, res
        assert res["engine"]["state"] in ("starting", "running"), res["engine"]
        deadline = time.time() + 10
        st = res["engine"]
        while time.time() < deadline and st["state"] != "running":
            time.sleep(0.2)
            st = gw.status()
        assert st["state"] == "running", st
        assert st["owned"] is True and st["pid"] and st["can_stop"] is True
        assert st["can_start"] is False

        proc = gw._proc
        stop = gw.stop()
        assert stop["ok"] is True and stop["stopped"] is True, stop
        assert proc is not None and proc.poll() is not None
        deadline = time.time() + 5
        while time.time() < deadline and gw.status()["reachable"]:
            time.sleep(0.1)
        assert gw.status()["state"] == "stopped", gw.status()
    finally:
        os.environ["PATH"] = old_path
        if gw._proc is not None:
            gw._proc.terminate()
            try:
                gw._proc.wait(timeout=5)
            except Exception:
                gw._proc.kill()


# --------------------------------------------------------------- endpoints
def _bridge():
    store, key = _keys()
    httpd, url, records = _upstream()
    server_mod.BRIDGE.ai_keys = store
    server_mod.BRIDGE.ai_gateway = AIGateway(
        store, model_config=_model_config(), upstream_url=url)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.make_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", key, records, httpd


def _req(url, payload=None, key=None, stream=False):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers, method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_ai_engine_endpoint_requires_auth_and_serves_status():
    srv, base, _key, _records, httpd = _bridge()
    pairing, _ = get_or_create_key()
    try:
        assert _req(base + "/api/ai-engine")[0] == 401
        req = urllib.request.Request(base + "/api/ai-engine",
                                     headers={"X-Pair-Key": pairing})
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
            engine = json.loads(resp.read())["engine"]
        assert engine["state"] == "external" and engine["reachable"] is True
    finally:
        srv.shutdown()
        httpd.shutdown()


def test_ai_engine_start_refuses_an_external_engine():
    srv, base, _key, _records, httpd = _bridge()
    pairing, _ = get_or_create_key()
    try:
        for path in ("/api/ai-engine/start", "/api/ai-engine/stop"):
            req = urllib.request.Request(base + path, data=b"{}", method="POST",
                                         headers={"X-Pair-Key": pairing,
                                                  "Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=10)
                raise AssertionError("expected 409 for an external engine")
            except urllib.error.HTTPError as exc:
                assert exc.code == 409, (path, exc.code)
                payload = json.loads(exc.read())
                assert payload["code"] == "engine_external", payload
    finally:
        srv.shutdown()
        httpd.shutdown()


def test_v1_sends_cors_headers_and_answers_preflight():
    # Browser-based AI clients on phones need CORS; the key is still
    # required for every real request.
    srv, base, key, _records, httpd = _bridge()
    try:
        req = urllib.request.Request(
            base + "/v1/chat/completions", method="OPTIONS",
            headers={"Origin": "http://localhost:3000",
                     "Access-Control-Request-Method": "POST"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 204
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"
            assert "POST" in resp.headers.get("Access-Control-Allow-Methods", "")
        req = urllib.request.Request(
            base + "/v1/models", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"
    finally:
        srv.shutdown()
        httpd.shutdown()


def test_v1_requires_an_ai_key():
    srv, base, _key, _records, httpd = _bridge()
    try:
        code, body = _req(base + "/v1/models")
        assert code == 401 and json.loads(body)["error"]["code"] == "invalid_api_key"
        code, body = _req(base + "/v1/chat/completions",
                          {"messages": [{"role": "user", "content": "hi"}]})
        assert code == 401, (code, body)
        code, body = _req(base + "/v1/models", key="mk-ai-wrong")
        assert code == 401, (code, body)
    finally:
        srv.shutdown()
        httpd.shutdown()


def test_v1_models_chat_and_stream_through_the_bridge():
    srv, base, key, records, httpd = _bridge()
    try:
        code, body = _req(base + "/v1/models", key=key)
        assert code == 200
        ids = [m["id"] for m in json.loads(body)["data"]]
        assert "Dolphin3.0-Llama3.1-8B" in ids, ids

        code, body = _req(base + "/v1/chat/completions",
                          {"model": "monico-local",
                           "messages": [{"role": "user", "content": "hi"}]},
                          key=key)
        assert code == 200, (code, body)
        assert json.loads(body)["object"] == "chat.completion"
        assert records[-1]["payload"]["messages"][0]["content"] == "hi"

        code, body = _req(base + "/v1/chat/completions",
                          {"messages": "not-a-list"}, key=key)
        assert code == 400 and "messages" in json.loads(body)["error"]["message"]

        code, body = _req(base + "/v1/chat/completions",
                          {"model": "monico-local", "stream": True,
                           "messages": [{"role": "user", "content": "stream"}]},
                          key=key)
        assert code == 200, (code, body)
        assert "data: [DONE]" in body, body
        assert '"content":"hi"' in body, body
    finally:
        srv.shutdown()
        httpd.shutdown()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
