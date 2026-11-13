"""OpenAI-compatible AI endpoint, backed by the installed llama.cpp model.

Keys come from the settings page (`mk-ai-...`, stored hashed by
bridge.settings.AIKeyStore). Requests are forwarded to a llama.cpp
server:

* `MONICO_LLAMA_URL` (openai-compatible base URL) if set: that server
  is used as-is, and nothing is started here.
* otherwise http://127.0.0.1:8080: if nothing healthy answers there and
  `monico-model` installed a model, this module starts
  `llama-server -m <file>` once (lazily, on the first request) and
  waits for it to become ready.

Nothing is faked: no model installed, no `llama-server` binary, or a
server that dies while loading all come back as a 503 with the real
reason, in OpenAI's error shape.

Served by bridge/server.py:

    GET  /v1/models
    POST /v1/chat/completions
    POST /v1/completions

Stdlib only, like the rest of the bridge.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .common import DATA_DIR, MODEL_CONFIG, installed_model, log

DEFAULT_LLAMA_URL = "http://127.0.0.1:8080"
START_TIMEOUT_S = 240      # a cold 8B load on a small box can take a while
REQUEST_TIMEOUT_S = 900    # long generations are slow on CPU
MODEL_ALIAS = "monico-local"   # short model name clients can send


def _normalise_base(url: str) -> str:
    """Accept both spellings clients paste in: the server root
    (http://box:11434) and its OpenAI base (http://box:11434/v1).
    Forwarding always uses root + /v1/<route>, so /v1 must not double."""
    url = str(url or "").strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3].rstrip("/")
    return url


def openai_error(message: str, type_: str = "invalid_request_error",
                 code: str | None = None) -> dict:
    return {"error": {"message": message, "type": type_,
                      "param": None, "code": code}}


class GatewayError(Exception):
    """A failure that is already shaped as an OpenAI error response."""

    def __init__(self, status: int, payload: dict) -> None:
        super().__init__(payload.get("error", {}).get("message", "gateway error"))
        self.status = status
        self.payload = payload


class UpstreamResponse:
    """One in-flight answer from llama-server (streamed or whole)."""

    def __init__(self, status: int, content_type: str,
                 resp: http.client.HTTPResponse,
                 conn: http.client.HTTPConnection) -> None:
        self.status = status
        self.content_type = content_type or "application/json"
        self._resp = resp
        self._conn = conn

    def read(self) -> bytes:
        return self._resp.read()

    def iter_lines(self):
        """SSE lines exactly as the upstream sent them."""
        while True:
            line = self._resp.readline()
            if not line:
                return
            yield line

    def close(self) -> None:
        try:
            self._resp.close()
        finally:
            self._conn.close()


class AIGateway:
    def __init__(self, keys, model_config: Path | str | None = None,
                 upstream_url: str | None = None) -> None:
        self.keys = keys
        self.model_config = Path(model_config) if model_config else MODEL_CONFIG
        env_url = os.environ.get("MONICO_LLAMA_URL", "").strip()
        self._upstream = _normalise_base(
            upstream_url or env_url or DEFAULT_LLAMA_URL)
        self._explicit = bool(upstream_url or env_url)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._log_path = DATA_DIR / "llama-server.log"

    # -- auth -----------------------------------------------------------
    def authenticate(self, authorization: str | None) -> dict | None:
        """The key record for `Authorization: Bearer mk-ai-...`, or None."""
        if not authorization:
            return None
        scheme, _, token = authorization.partition(" ")
        if scheme.strip().lower() != "bearer":
            return None
        return self.keys.verify(token.strip())

    # -- model info -----------------------------------------------------
    def installed(self) -> dict | None:
        """The installed model config, only when its file is really there.
        $MONICO_MODEL_FILE overrides it with any GGUF already on disk."""
        return installed_model(self.model_config)

    def _upstream_models(self) -> list[dict]:
        """Models an external OpenAI-compatible server advertises."""
        try:
            req = urllib.request.Request(self._upstream + "/v1/models")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status != 200:
                    return []
                data = json.loads(resp.read() or b"{}")
        except Exception:
            return []
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        out = []
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                out.append({"id": str(item["id"]), "object": "model",
                            "created": int(item.get("created") or 0),
                            "owned_by": str(item.get("owned_by") or "local")})
        return out

    def models(self) -> list[dict]:
        """Our installed model plus whatever the upstream server offers,
        deduped by id. An external engine (Ollama, LM Studio, vLLM, a
        hand-started llama.cpp) keeps its real model list visible."""
        out: list[dict] = []
        seen: set[str] = set()
        cfg = self.installed()
        if cfg:
            name = cfg.get("model") or MODEL_ALIAS
            ids = [name] if name == MODEL_ALIAS else [name, MODEL_ALIAS]
            for i in ids:
                out.append({"id": i, "object": "model", "created": 0,
                            "owned_by": "monico-labs"})
                seen.add(i)
        if self._explicit or self._healthy(self._upstream):
            for m in self._upstream_models():
                if m["id"] not in seen:
                    out.append(m)
                    seen.add(m["id"])
        return out

    # -- upstream -------------------------------------------------------
    def target(self) -> str:
        """Base URL to forward to, starting the engine when this gateway
        owns it. Raises GatewayError with a 503 when it cannot."""
        if self._explicit:
            return self._upstream
        with self._lock:
            if self._healthy(self._upstream):
                return self._upstream
            self._start_engine_locked()
            return self._upstream

    @staticmethod
    def _healthy(url: str) -> bool:
        """Real readiness, old and new servers alike: /health (recent
        llama.cpp) or /v1/models (older llama.cpp, Ollama, LM Studio,
        vLLM). A bare open socket is not enough: it may be any service."""
        for path in ("/health", "/v1/models"):
            try:
                req = urllib.request.Request(url + path)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                continue
        return False

    def _server_binary(self) -> str | None:
        """llama-server: on PATH first, then the install-time record."""
        found = shutil.which("llama-server")
        if found:
            return found
        cfg = self.installed() or {}
        recorded = str(cfg.get("runner") or "")
        if Path(recorded).name == "llama-server" and Path(recorded).exists():
            return recorded
        return None

    def _spawn_locked(self) -> subprocess.Popen:
        """Start llama-server if it is not already running. Returns at
        once; readiness is `_wait_ready_locked`'s job."""
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        cfg = self.installed()
        if not cfg:
            raise GatewayError(503, openai_error(
                "no local model on this device: run `monico-model install` "
                "first, or set MONICO_LLAMA_URL to an OpenAI-compatible "
                "llama.cpp server", "service_unavailable",
                "model_not_installed"))
        runner = self._server_binary()
        if not runner:
            raise GatewayError(503, openai_error(
                "llama-server is not installed on this device: build it from "
                "llama.cpp (see README), or set MONICO_LLAMA_URL to a "
                "running OpenAI-compatible server", "service_unavailable",
                "llama_server_missing"))
        parts = urlparse(self._upstream)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 8080
        cmd = [runner, "-m", cfg["file"], "--host", host,
               "--port", str(port), "-c", "4096"]
        try:
            stream = self._log_path.open("ab")
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=stream,
                stderr=subprocess.STDOUT, start_new_session=True)
        except Exception as exc:
            raise GatewayError(502, openai_error(
                f"could not start llama-server: {exc}", "api_error",
                "engine_start_failed"))
        self._proc = proc
        self._started_at = time.time()
        log(f"llama-server starting on {self._upstream} (pid {proc.pid})")
        return proc

    def _wait_ready_locked(self, proc: subprocess.Popen) -> None:
        deadline = time.time() + START_TIMEOUT_S
        while time.time() < deadline:
            if proc.poll() is not None:
                self._proc = None
                self._started_at = None
                raise GatewayError(503, openai_error(
                    "llama-server exited while loading the model: "
                    + self._log_tail(), "service_unavailable",
                    "engine_exited"))
            if self._healthy(self._upstream):
                log(f"llama-server ready on {self._upstream}")
                return
            time.sleep(1)
        try:
            proc.terminate()
        except Exception:
            pass
        self._proc = None
        self._started_at = None
        raise GatewayError(503, openai_error(
            f"llama-server did not become ready in {START_TIMEOUT_S}s: "
            + self._log_tail(), "service_unavailable", "engine_timeout"))

    def _start_engine_locked(self) -> None:
        proc = self._spawn_locked()
        self._wait_ready_locked(proc)

    def _log_tail(self, limit: int = 600) -> str:
        try:
            data = self._log_path.read_bytes()[-limit:]
            return data.decode("utf-8", "replace").strip() or "no output logged"
        except Exception:
            return "no log available"

    # -- explicit control (dashboard) ------------------------------------
    def status(self) -> dict:
        """Where the engine stands, for the dashboard panel.

        `state` is one of running / starting / stopped / external /
        external-unreachable / running-external. "running-external" is
        an answering llama-server this bridge did not start (an earlier
        bridge run, or a hand-started one): honest, and not stoppable
        from here."""
        cfg = self.installed()
        reachable = self._healthy(self._upstream)
        proc = self._proc
        owned = proc is not None and proc.poll() is None
        if self._explicit:
            state = "external" if reachable else "external-unreachable"
        elif reachable:
            state = "running" if owned else "running-external"
        elif owned:
            state = "starting"
        else:
            state = "stopped"
        has_server = self._server_binary() is not None
        hint = ""
        if self._explicit:
            hint = ("MONICO_LLAMA_URL is set: this bridge forwards to that "
                    "server and does not start or stop it.")
        elif not cfg:
            hint = "No local model installed: run `monico-model install`."
        elif not has_server and not reachable:
            hint = ("llama-server is not on PATH: build it from llama.cpp "
                    "(see README), or set MONICO_LLAMA_URL.")
        elif state == "running-external":
            hint = ("A llama-server this bridge did not start is answering; "
                    "it will not be stopped from here.")
        elif state == "starting":
            hint = "Loading the model. The first start can take a minute or more."
        return {
            "state": state,
            "managed": not self._explicit,
            "upstream": self._upstream,
            "reachable": reachable,
            "owned": owned,
            "pid": proc.pid if owned else None,
            "started_at": (int(self._started_at)
                           if owned and self._started_at else None),
            "installed": bool(cfg),
            "model": (cfg or {}).get("model"),
            "quant": (cfg or {}).get("quant"),
            "size_gb": (cfg or {}).get("size_gb"),
            "has_llama_server": has_server,
            "log": str(self._log_path) if self._log_path.exists() else None,
            "can_start": (not self._explicit and not reachable
                          and bool(cfg) and has_server),
            "can_stop": (not self._explicit and owned),
            "hint": hint,
        }

    def start(self) -> dict:
        """Dashboard Start: spawn llama-server and return at once.
        The caller polls status() while the model loads."""
        if self._explicit:
            return {"error": "MONICO_LLAMA_URL is set: the engine is "
                             "external, start it where it runs",
                    "code": "engine_external", "engine": self.status()}
        with self._lock:
            if self._healthy(self._upstream):
                return {"ok": True, "engine": self.status()}
            try:
                self._spawn_locked()
            except GatewayError as exc:
                err = exc.payload.get("error", {})
                return {"error": err.get("message", "could not start the engine"),
                        "code": err.get("code"),
                        "engine": self.status()}
            return {"ok": True, "engine": self.status()}

    def stop(self) -> dict:
        """Dashboard Stop: stop only a llama-server this bridge started."""
        if self._explicit:
            return {"error": "MONICO_LLAMA_URL is set: the engine is "
                             "external, stop it where it runs",
                    "code": "engine_external", "engine": self.status()}
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._proc = None
                self._started_at = None
                if self._healthy(self._upstream):
                    return {"error": "a llama-server this bridge did not "
                                     "start is answering; stop it where it runs",
                            "code": "not_owned", "engine": self.status()}
                return {"ok": True, "stopped": False, "engine": self.status()}
            self._proc = None
            self._started_at = None
            proc.terminate()
            deadline = time.time() + 10
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.1)
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
        log("llama-server stopped from the dashboard")
        return {"ok": True, "stopped": True, "engine": self.status()}

    def post(self, path: str, payload: dict) -> UpstreamResponse:
        """Send one OpenAI request upstream. Raises GatewayError."""
        base = self.target()
        parts = urlparse(base)
        cls = (http.client.HTTPSConnection if parts.scheme == "https"
               else http.client.HTTPConnection)
        conn = cls(parts.hostname, parts.port, timeout=REQUEST_TIMEOUT_S)
        try:
            conn.request("POST", path, body=json.dumps(payload).encode(),
                         headers={"Content-Type": "application/json",
                                  "Accept": ("text/event-stream"
                                             if payload.get("stream")
                                             else "application/json")})
            resp = conn.getresponse()
        except Exception as exc:
            conn.close()
            raise GatewayError(502, openai_error(
                f"local AI upstream at {base} is not reachable: {exc}",
                "api_error", "upstream_unreachable"))
        if resp.status >= 400:
            body = resp.read()
            resp.close()
            conn.close()
            try:
                payload_err = json.loads(body)
            except Exception:
                payload_err = openai_error(
                    body.decode("utf-8", "replace")[:600] or "upstream error",
                    "api_error")
            raise GatewayError(resp.status, payload_err)
        return UpstreamResponse(resp.status,
                                resp.getheader("Content-Type", "application/json"),
                                resp, conn)
