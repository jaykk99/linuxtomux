"""Monico Labs bridge, Linux side.

Run it:  monico-server start

The Linux box listens; the phone connects OUT to it (phones sit behind
carrier NAT, so the phone always initiates). The phone keeps a
long-poll channel open; commands for the phone are queued and handed
down that channel, results come back up it. Commands for Linux run
locally. The homescreen is served here, owner-key only.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .common import (DATA_DIR, VERSION, accepted_tokens, check_token,
                     get_or_create_key, log, model_status, run_command)
from .hwdetect import detect

UI_FILE = Path(__file__).resolve().parent.parent / "ui" / "homescreen.html"
LOGIN_HTML = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Monico Labs</title><body style="font-family:system-ui;background:#0b0b0d;color:#f2f2f2;display:grid;place-items:center;min-height:100vh;margin:0">
<form method="POST" action="/login" style="text-align:center">
<h1 style="letter-spacing:.2em">MONICO LABS</h1><p>Owner access</p>
<input name="key" type="password" placeholder="Pairing key" style="padding:12px;border-radius:10px;border:1px solid #333;background:#151517;color:#fff">
<button style="padding:12px 18px;border-radius:10px;border:0;background:#e8e8e8;font-weight:700">Enter</button>
</form></body>"""


class Bridge:
    def __init__(self) -> None:
        self.devices: dict[str, dict] = {}
        self.queues: dict[str, queue.Queue] = {}
        self.results: dict[str, dict] = {}
        self.events: dict[str, threading.Event] = {}
        self.lock = threading.Lock()
        self.started = time.time()
        self.hw = detect()

    # -- device registry -------------------------------------------------
    def register(self, info: dict) -> dict:
        did = info["device_id"]
        with self.lock:
            dev = self.devices.get(did, {})
            dev.update(info)
            dev["last_seen"] = time.time()
            self.devices[did] = dev
            self.queues.setdefault(did, queue.Queue())
        log(f"register {did} ({info.get('name')})")
        return {"ok": True, "device_id": did}

    def device_list(self) -> list[dict]:
        now = time.time()
        out = [{
            "device_id": "linux", "name": self.hw.get("hostname") or "linux",
            "platform": "linux", "online": True, "role": "server (this machine)",
            "hw": self.hw, "model": model_status(),
            "hosted": ["Monico Bridge homescreen", "Bridge command channel"],
        }]
        with self.lock:
            for did, dev in self.devices.items():
                out.append({
                    "device_id": did, "name": dev.get("name", did),
                    "platform": dev.get("platform", "?"),
                    "online": now - dev.get("last_seen", 0) < 40,
                    "last_seen_s": int(now - dev.get("last_seen", now)),
                    "role": "Termux device server",
                    "hw": dev.get("hw", {}), "model": dev.get("model", {"installed": False}),
                    "hosted": dev.get("services", []),
                })
        return out

    # -- job plumbing ------------------------------------------------------
    def submit(self, target: str, job: dict, wait_s: float) -> dict:
        job = dict(job)
        job["id"] = uuid.uuid4().hex[:12]
        if target == "linux":
            if job["type"] == "exec":
                res = run_command(job["command"], job.get("timeout_s", 60))
                return {"id": job["id"], "target": "linux", **res}
            return {"id": job["id"], "error": "linux side has no proxied services"}
        with self.lock:
            q = self.queues.get(target)
            if q is None:
                return {"id": job["id"], "error": f"unknown device {target}"}
            ev = threading.Event()
            self.events[job["id"]] = ev
        q.put(job)
        if ev.wait(timeout=wait_s):
            with self.lock:
                return self.results.pop(job["id"], {"id": job["id"], "error": "no result"})
        return {"id": job["id"], "error": "device did not answer in time",
                "timeout": True}

    def poll(self, device_id: str, hold_s: float = 25.0) -> dict:
        with self.lock:
            q = self.queues.setdefault(device_id, queue.Queue())
            dev = self.devices.get(device_id)
            if dev:
                dev["last_seen"] = time.time()
        try:
            job = q.get(timeout=hold_s)
            return {"job": job}
        except queue.Empty:
            return {"job": None}

    def complete(self, job_id: str, payload: dict) -> None:
        with self.lock:
            self.results[job_id] = payload
            ev = self.events.pop(job_id, None)
        if ev:
            ev.set()


BRIDGE = Bridge()


def make_handler():
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # quiet
            pass

        # -- helpers ------------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str = "application/json",
                  extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj: dict, extra: dict | None = None) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json", extra)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                return json.loads(raw or b"{}")
            except Exception:
                return {}

        def _authed(self) -> bool:
            token = self.headers.get("X-Pair-Key", "")
            if check_token(token):
                return True
            cookie = self.headers.get("Cookie", "")
            for part in cookie.split(";"):
                part = part.strip()
                if part.startswith("monico_key=") and check_token(part.split("=", 1)[1]):
                    return True
            qs = parse_qs(urlparse(self.path).query)
            if "key" in qs and check_token(qs["key"][0]):
                return True
            return False

        # -- routes ---------------------------------------------------------
        def do_GET(self):
            u = urlparse(self.path)
            p = u.path
            if p == "/health":
                return self._json(200, {"ok": True, "service": "monico-bridge",
                                        "version": VERSION,
                                        "uptime_s": int(time.time() - BRIDGE.started)})
            if p == "/install-termux.sh":
                # Public, like termux-code's install.sh: the script itself
                # carries no secret; the pairing key arrives via env.
                script = UI_FILE.parent.parent / "install-termux.sh"
                if script.exists():
                    return self._send(200, script.read_bytes(), "text/x-shellscript")
                return self._json(404, {"error": "installer not found"})
            if p == "/bridge/poll":
                if not self._authed():
                    return self._json(401, {"error": "bad pairing key"})
                qs = parse_qs(u.query)
                did = (qs.get("device_id") or [""])[0]
                return self._json(200, BRIDGE.poll(did))
            if not self._authed():
                if p == "/":
                    return self._send(200, LOGIN_HTML.encode(), "text/html; charset=utf-8")
                return self._json(401, {"error": "owner key required"})
            if p == "/":
                html = UI_FILE.read_text() if UI_FILE.exists() else "<h1>Monico Labs</h1>"
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if p == "/api/devices":
                return self._json(200, {"devices": BRIDGE.device_list()})
            if p.startswith("/device/"):
                # /device/<id>/hosted/<path> -> proxied to the device's own service
                parts = p.split("/", 4)
                if len(parts) == 5 and parts[3] == "hosted":
                    did, rest = parts[2], unquote(parts[4])
                    res = BRIDGE.submit(did, {"type": "http", "method": "GET",
                                              "path": "/" + rest}, wait_s=30)
                    if "error" in res:
                        return self._json(502, res)
                    body = res.get("body", "")
                    raw = body.encode() if isinstance(body, str) else bytes(body)
                    return self._send(res.get("status", 200), raw,
                                      res.get("content_type", "text/html; charset=utf-8"))
                return self._json(404, {"error": "not found"})
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            p = u.path
            if p == "/login":
                n = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(n).decode() if n else "")
                key = (form.get("key") or [""])[0]
                if check_token(key):
                    return self._send(303, b"", "text/plain",
                                      {"Location": "/",
                                       "Set-Cookie": f"monico_key={key}; Path=/; HttpOnly; SameSite=Lax"})
                return self._send(403, b"bad key", "text/plain")
            if not self._authed():
                return self._json(401, {"error": "bad pairing key"})
            body = self._body()
            if p == "/bridge/register":
                if not body.get("device_id"):
                    return self._json(400, {"error": "device_id required"})
                return self._json(200, BRIDGE.register(body))
            if p == "/bridge/result":
                BRIDGE.complete(body.get("id", ""), body.get("result", {}))
                return self._json(200, {"ok": True})
            if p == "/api/exec":
                target = body.get("target", "linux")
                cmd = body.get("command", "")
                if not cmd:
                    return self._json(400, {"error": "command required"})
                wait = float(body.get("timeout_s", 60)) + 15
                res = BRIDGE.submit(target, {"type": "exec", "command": cmd,
                                             "timeout_s": body.get("timeout_s", 60)},
                                    wait_s=wait)
                log(f"exec target={target} cmd={cmd!r} -> exit={res.get('exit_code')}")
                return self._json(200, res)
            return self._json(404, {"error": "not found"})

    return H


def serve(port: int) -> None:
    key, is_new = get_or_create_key()
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler())
    print("=" * 60, flush=True)
    print(f"MONICO LABS BRIDGE server v{VERSION}", flush=True)
    print(f"Homescreen:  http://127.0.0.1:{port}/", flush=True)
    if is_new:
        print(f"Pairing key (shown once, saved 0600): {key}", flush=True)
    else:
        print(f"Pairing key: saved in {DATA_DIR}/pairing-key (0600)", flush=True)
    print("Waiting for a Termux device to connect with monico-connect.", flush=True)
    print("=" * 60, flush=True)
    log(f"server start on :{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def token_add(token: str) -> None:
    from .common import DATA_DIR as dd  # local alias
    dd.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = dd / "accepted_tokens"
    existing = path.read_text() if path.exists() else ""
    if token not in existing:
        with path.open("a") as fh:
            fh.write(token + "\n")
        path.chmod(0o600)
    print("token accepted for this server (stored 0600).")
