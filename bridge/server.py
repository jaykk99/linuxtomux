"""Monico Labs bridge, Linux side.

Run it:  monico-server start [--join http://<other-linux>:8765]

The Linux box listens; phones connect OUT to it (phones sit behind
carrier NAT, so the phone always initiates). The phone keeps a
long-poll channel open; commands for the phone are queued and handed
down that channel, results come back up it. Commands for Linux run
locally. The homescreen is served here, owner-key only.

Mesh: any number of servers can federate into one mesh. Start a
server with --join <url> (same pairing key) and it registers on that
peer, syncs device registries both ways, and relays commands: a
command aimed at a device on the far server is handed to that server
down its poll channel and executed there. Every device record carries
the id of the server that hosts it directly (its "origin"), so merged
lists never duplicate or loop. If a peer link drops, the far side's
devices age to offline and everything local keeps working; when the
link returns, the next sync heals the list.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .apps import APPS, AppProbe
from .common import (DATA_DIR, KEY_FILE, VERSION, check_token,
                     ensure_data_dir, get_or_create_key,
                     get_or_create_server_id, log, model_status,
                     run_command)
from .hwdetect import detect

UI_FILE = Path(__file__).resolve().parent.parent / "ui" / "homescreen.html"
LOGIN_HTML = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Monico Labs</title><body style="font-family:system-ui;background:#0b0b0d;color:#f2f2f2;display:grid;place-items:center;min-height:100vh;margin:0">
<form method="POST" action="/login" style="text-align:center">
<h1 style="letter-spacing:.2em">MONICO LABS</h1><p>Owner access</p>
<input name="key" type="password" placeholder="Pairing key" style="padding:12px;border-radius:10px;border:1px solid #333;background:#151517;color:#fff">
<button style="padding:12px 18px;border-radius:10px;border:0;background:#e8e8e8;font-weight:700">Enter</button>
</form></body>"""

ONLINE_WINDOW_S = 40     # direct device is online if seen this recently
REMOTE_FRESH_S = 45      # a peer's claims are trusted this long
SYNC_INTERVAL_S = 8      # seconds between peer registry syncs


class Bridge:
    def __init__(self, name: str | None = None, server_id: str | None = None) -> None:
        self.server_id = server_id or get_or_create_server_id()
        self.hw = detect()
        self.name = name or self.hw.get("hostname") or "linux"
        self.devices: dict[str, dict] = {}
        self.queues: dict[str, queue.Queue] = {}
        self.results: dict[str, dict] = {}
        self.events: dict[str, threading.Event] = {}
        # Registry claims learned from peers:
        # (via_server_id, origin_server_id) -> {"devices", "at", "via_name"}
        self.claims: dict[tuple[str, str], dict] = {}
        self.lock = threading.Lock()
        self.started = time.time()
        self.port: int | None = None
        self.app_probe = AppProbe()

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

    def _self_record(self) -> dict:
        return {
            "device_id": self.server_id, "name": self.name,
            "platform": "linux", "kind": "server", "role": "Linux server",
            "online": True, "hw": self.hw, "model": model_status(),
            "hosted": ["Monico Bridge homescreen", "Bridge command channel"],
            "origin": self.server_id, "origin_name": self.name,
        }

    def _local_record(self, did: str, dev: dict, now: float) -> dict:
        platform = dev.get("platform", "?")
        kind = dev.get("kind", "device")
        if kind == "server":
            role = "Linux server (mesh peer)"
        elif platform.startswith("android"):
            role = "Termux device server"
        else:
            role = "Device server"
        return {
            "device_id": did, "name": dev.get("name", did),
            "platform": platform, "kind": kind, "role": role,
            "online": now - dev.get("last_seen", 0) < ONLINE_WINDOW_S,
            "last_seen_s": int(now - dev.get("last_seen", now)),
            "hw": dev.get("hw", {}), "model": dev.get("model", {"installed": False}),
            "hosted": dev.get("services", []),
            "origin": self.server_id, "origin_name": self.name,
        }

    def snapshot(self) -> list[dict]:
        """Records for devices hosted HERE (this server + direct clients)."""
        now = time.time()
        out = [self._self_record()]
        with self.lock:
            items = list(self.devices.items())
        for did, dev in items:
            out.append(self._local_record(did, dev, now))
        return out

    def known(self) -> list[dict]:
        """The full mesh view: local devices plus everything peers
        have told us, deduped by device_id. Local knowledge wins; for
        remote copies the direct claim (via == origin) wins, then the
        freshest claim. A remote device shows online only while the
        claim carrying it is fresh, so a dead peer's devices age out
        to offline instead of vanishing or lying."""
        out: dict[str, dict] = {}
        for rec in self.snapshot():
            out[rec["device_id"]] = {
                **rec, "local": True,
                "self": rec["device_id"] == self.server_id, "via": None,
            }
        now = time.time()
        best: dict[str, tuple] = {}
        with self.lock:
            claims = list(self.claims.items())
        for (via, origin), claim in claims:
            for did, rec in claim["devices"].items():
                if did in out:
                    continue
                cand = (via == origin, claim["at"])
                if did not in best or cand > best[did][0]:
                    best[did] = (cand, claim, rec)
        for did, (_cand, claim, rec) in best.items():
            fresh = now - claim["at"] < REMOTE_FRESH_S
            out[did] = {**rec, "local": False, "self": False,
                        "via": claim.get("via_name"),
                        "online": bool(rec.get("online")) and fresh}
        return list(out.values())

    def device_list(self) -> list[dict]:
        return self.known()

    def app_list(self) -> list[dict]:
        """Hosted-app entries for the homescreen, each with a live
        (briefly cached) probe status. Apps are the same on every
        server in the mesh: they are web properties, not devices."""
        return self.app_probe.statuses(APPS)

    def apply_sync(self, via_id: str, via_name: str, records: list[dict]) -> list[dict]:
        """Store a peer's full device list as claims, keyed by the
        origin server each record belongs to. Claims from this peer
        are replaced wholesale, so devices the peer no longer knows
        drop out of its claims. Records whose origin is THIS server
        are ignored (our own devices coming back around). Returns our
        full merged view, which the peer merges the same way."""
        grouped: dict[str, dict] = {}
        for rec in records or []:
            origin = rec.get("origin") or via_id
            did = rec.get("device_id")
            if origin == self.server_id or not did or did == self.server_id:
                continue
            grouped.setdefault(origin, {})[did] = rec
        now = time.time()
        with self.lock:
            stale = [k for k in self.claims
                     if k[0] == via_id and k[1] not in grouped]
            for key in stale:
                del self.claims[key]
            for origin, devs in grouped.items():
                self.claims[(via_id, origin)] = {
                    "devices": devs, "at": now, "via_name": via_name}
        return self.known()

    # -- job plumbing ------------------------------------------------------
    def route(self, target: str) -> tuple[str | None, str | None]:
        """Where does a job for `target` go? ("self"|"local"|"relay", via)"""
        if target in ("linux", "server", "this") or target == self.server_id:
            return ("self", None)
        with self.lock:
            if target in self.devices:
                return ("local", None)
            claims = list(self.claims.items())
        best = None
        for (via, origin), claim in claims:
            if target in claim["devices"]:
                cand = (via == origin, claim["at"])
                if best is None or cand > best[0]:
                    best = (cand, via)
        if best:
            return ("relay", best[1])
        return (None, None)

    def submit(self, target: str, job: dict, wait_s: float) -> dict:
        job = dict(job)
        job["id"] = uuid.uuid4().hex[:12]
        kind, via = self.route(target)
        if kind == "self":
            if job["type"] == "exec":
                res = run_command(job["command"], job.get("timeout_s", 60))
                return {"id": job["id"], "target": target, **res}
            return {"id": job["id"], "error": "this server has no proxied services"}
        if kind is None:
            return {"id": job["id"], "error": f"unknown device {target}"}
        if kind == "local":
            dest, payload, wait = target, job, wait_s
        else:  # relay: hand the job to the peer that hosts the target
            dest = via
            payload = {"type": "relay", "id": job["id"],
                       "target": target, "job": job}
            wait = wait_s + 15
        with self.lock:
            q = self.queues.get(dest)
            if q is None:
                return {"id": job["id"],
                        "error": f"device {dest} is not connected"}
            ev = threading.Event()
            self.events[job["id"]] = ev
        q.put(payload)
        if ev.wait(timeout=wait):
            with self.lock:
                return self.results.pop(job["id"], {"id": job["id"], "error": "no result"})
        with self.lock:
            self.events.pop(job["id"], None)
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


# ----------------------------------------------------------------------
# Peer link: the client half of a federation, run inside a server that
# started with --join. Registers on the peer, syncs registries both
# ways, and executes relayed jobs through this server's own routing.
# ----------------------------------------------------------------------
class PeerLink:
    def __init__(self, bridge: Bridge, peer_url: str, token: str, my_port: int) -> None:
        self.bridge = bridge
        self.peer = peer_url.rstrip("/")
        self.token = token
        self.my_port = my_port

    def _req(self, method: str, path: str, payload: dict | None = None,
             timeout: int = 35) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.peer + path, data=data, method=method,
                                     headers={"X-Pair-Key": self.token,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")

    def _register(self) -> None:
        b = self.bridge
        info = {"device_id": b.server_id, "name": b.name,
                "platform": "linux", "kind": "server", "hw": b.hw,
                "model": model_status(),
                "services": ["Monico Bridge homescreen", "Bridge command channel"]}
        self._req("POST", "/bridge/register", info, timeout=20)

    def _sync(self) -> None:
        b = self.bridge
        resp = self._req("POST", "/bridge/peer-sync",
                         {"server_id": b.server_id, "name": b.name,
                          "devices": b.known()}, timeout=20)
        peer_id = resp.get("server_id")
        if peer_id:
            b.apply_sync(peer_id, resp.get("name", peer_id),
                         resp.get("devices", []))

    def _handle(self, job: dict) -> None:
        b = self.bridge
        jtype = job.get("type")
        if jtype == "relay":
            inner = job.get("job", {})
            wait = float(inner.get("timeout_s", 60)) + 10
            res = b.submit(job.get("target", ""), inner, wait_s=wait)
            self._req("POST", "/bridge/result",
                      {"id": job["id"], "result": res}, timeout=20)
            return
        if jtype == "exec":
            res = run_command(job["command"], job.get("timeout_s", 60))
            self._req("POST", "/bridge/result",
                      {"id": job["id"], "result": {"id": job["id"], **res}},
                      timeout=20)
            return
        if jtype == "http":
            # Someone on the peer opened this server's hosted pages:
            # answer from our own homescreen service over loopback.
            path = job.get("path", "/")
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{self.my_port}{path}",
                    headers={"X-Pair-Key": self.token})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    result = {"status": resp.status,
                              "content_type": resp.headers.get("Content-Type", "text/html"),
                              "body": resp.read().decode("utf-8", "replace")}
            except Exception as exc:
                result = {"status": 502, "content_type": "text/plain",
                          "body": f"peer server http error: {exc}"}
            self._req("POST", "/bridge/result",
                      {"id": job["id"], "result": result}, timeout=20)
            return
        self._req("POST", "/bridge/result",
                  {"id": job["id"],
                   "result": {"error": f"unknown job type {jtype}"}},
                  timeout=20)

    def run(self) -> None:
        backoff = 2
        while True:
            try:
                self._register()
                print(f"Monico bridge: joined mesh at {self.peer} "
                      f"(this server: {self.bridge.name})", flush=True)
                log(f"peer link up: {self.peer}")
                backoff = 2
                last_sync = 0.0
                while True:
                    if time.time() - last_sync > SYNC_INTERVAL_S:
                        self._sync()
                        last_sync = time.time()
                    resp = self._req(
                        "GET",
                        f"/bridge/poll?device_id={self.bridge.server_id}",
                        timeout=35)
                    job = resp.get("job")
                    if job:
                        self._handle(job)
            except Exception as exc:
                print(f"Monico bridge: peer link down ({exc}); retrying in "
                      f"{backoff}s. Local devices keep working.", flush=True)
                log(f"peer link down: {exc}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)


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
                                        "server_id": BRIDGE.server_id,
                                        "name": BRIDGE.name,
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
            if p == "/api/apps":
                return self._json(200, {"apps": BRIDGE.app_list()})
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
            if p == "/bridge/peer-sync":
                sid = body.get("server_id", "")
                if not sid:
                    return self._json(400, {"error": "server_id required"})
                devices = BRIDGE.apply_sync(sid, body.get("name", sid),
                                            body.get("devices", []))
                return self._json(200, {"ok": True,
                                        "server_id": BRIDGE.server_id,
                                        "name": BRIDGE.name,
                                        "devices": devices})
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


def serve(port: int, join_url: str | None = None, join_key: str | None = None,
          name: str | None = None) -> None:
    global BRIDGE
    if join_key and not KEY_FILE.exists():
        # First run on this box: adopt the mesh pairing key as our own
        # (0600), so later starts need no --key flag.
        ensure_data_dir()
        KEY_FILE.write_text(join_key + "\n")
        try:
            KEY_FILE.chmod(0o600)
        except OSError:
            pass
    key, is_new = get_or_create_key()
    BRIDGE = Bridge(name=name)
    BRIDGE.port = port
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler())
    print("=" * 60, flush=True)
    print(f"MONICO LABS BRIDGE server v{VERSION}", flush=True)
    print(f"Server:      {BRIDGE.name} ({BRIDGE.server_id})", flush=True)
    print(f"Homescreen:  http://127.0.0.1:{port}/", flush=True)
    if is_new:
        print(f"Pairing key (shown once, saved 0600): {key}", flush=True)
    else:
        print(f"Pairing key: saved in {DATA_DIR}/pairing-key (0600)", flush=True)
    if join_url:
        token = join_key or key
        link = PeerLink(BRIDGE, join_url, token, port)
        threading.Thread(target=link.run, daemon=True).start()
        print(f"Joining mesh at {join_url} ...", flush=True)
    else:
        print("Waiting for devices to connect with monico-connect.", flush=True)
    print("=" * 60, flush=True)
    log(f"server start on :{port} id={BRIDGE.server_id} join={join_url or '-'}")
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
