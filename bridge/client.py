"""Monico Labs bridge, Termux side.

Run it:  monico-connect

The phone connects OUT to the Linux server (carrier NAT means the
server can never dial the phone first), registers, and then holds a
long-poll channel open. Down that channel the server can:
  - run commands on the phone (the phone is now a server too), and
  - fetch pages from the phone's own hosted service (below).

The phone's hosted service is a tiny local web service on
127.0.0.1:<service port>: a status page, a hosted-files index, and the
files themselves from ~/monico-host/. It is only reachable through the
bridge, with the pairing key, so hosting on the phone stays owner-only.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .common import (CLIENT_CONFIG, CLIENT_SERVICE_PORT, load_json,
                     log, model_status, run_command, save_json)
from .hwdetect import detect
from .specs import live_specs

HOST_DIR = Path.home() / "monico-host"
# How often this device reports its live specs (RAM, storage, CPU,
# uptime, battery) to the server for the dashboard.
SPECS_INTERVAL_S = float(os.environ.get("MONICO_SPECS_INTERVAL", "10"))


# ----------------------------------------------------------------------
# The phone as a server: its own tiny hosted service
# ----------------------------------------------------------------------
def start_hosted_service(port: int, name: str) -> ThreadingHTTPServer:
    HOST_DIR.mkdir(parents=True, exist_ok=True)

    class S(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                files = sorted(p.name for p in HOST_DIR.iterdir() if p.is_file())
                items = "".join(
                    f'<li><a href="/files/{f}">{f}</a></li>' for f in files) or "<li>(nothing hosted yet)</li>"
                html = (f"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
                        f"<title>{name} - Monico Labs hosting</title>"
                        f"<body style='font-family:system-ui;background:#0b0b0d;color:#f2f2f2;margin:24px'>"
                        f"<h1>{name}</h1><p>Hosted on this device, served through the Monico Labs bridge.</p>"
                        f"<h2>Files</h2><ul>{items}</ul>"
                        f"<p><a href='/status.json' style='color:#9fd'>Device status (JSON)</a></p></body>")
                return self._send(200, html.encode())
            if self.path == "/status.json":
                body = json.dumps({"device": name, "hw": detect(),
                                   "specs": live_specs(),
                                   "model": model_status(),
                                   "hosted_files": sorted(p.name for p in HOST_DIR.iterdir() if p.is_file())},
                                  indent=2).encode()
                return self._send(200, body, "application/json")
            if self.path.startswith("/files/"):
                fname = Path(self.path[len("/files/"):]).name
                fpath = HOST_DIR / fname
                if fpath.is_file():
                    return self._send(200, fpath.read_bytes(), "application/octet-stream")
                return self._send(404, b"not hosted here")
            return self._send(404, b"not found")

    httpd = ThreadingHTTPServer(("127.0.0.1", port), S)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# ----------------------------------------------------------------------
# Bridge channel
# ----------------------------------------------------------------------
class Client:
    def __init__(self, server_url: str, key: str, name: str):
        self.server = server_url.rstrip("/")
        self.key = key
        self.name = name
        self.device_id = "dev-" + uuid.uuid4().hex[:8]
        saved = load_json(CLIENT_CONFIG)
        if saved.get("device_id"):
            self.device_id = saved["device_id"]
        self.hw = detect()

    def _req(self, method: str, path: str, payload: dict | None = None,
             timeout: int = 40) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.server + path, data=data, method=method,
                                     headers={"X-Pair-Key": self.key,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")

    def register(self) -> None:
        # Fresh detection on every (re)register, so storage plugged
        # in since the last connect (USB drive) is reported truthfully.
        try:
            self.hw = detect()
        except Exception:
            pass
        save_json(CLIENT_CONFIG, {"server_url": self.server, "key": self.key,
                                  "name": self.name, "device_id": self.device_id})
        info = {"device_id": self.device_id, "name": self.name,
                "platform": self.hw["platform"], "kind": "device", "hw": self.hw,
                "model": model_status(), "specs": live_specs(),
                "services": ["Device status page", "Hosted files (~/monico-host)",
                             "Remote commands"]}
        self._req("POST", "/bridge/register", info, timeout=20)
        log(f"client registered as {self.device_id}")

    def _specs_loop(self) -> None:
        """Report live specs on an interval, on its own short request
        so it never waits behind the long-poll. Failures are silent:
        the main loop owns reconnecting. If the server says it does
        not know this device (it restarted), register again."""
        while True:
            time.sleep(max(2.0, SPECS_INTERVAL_S))
            try:
                self._req("POST", "/bridge/specs",
                          {"device_id": self.device_id, "specs": live_specs()},
                          timeout=15)
            except urllib.error.HTTPError as exc:
                try:
                    again = json.loads(exc.read() or b"{}").get("reregister")
                    if exc.code == 404 and again:
                        self.register()
                except Exception:
                    pass
            except Exception:
                pass

    def _handle_job(self, job: dict) -> dict:
        if job["type"] == "exec":
            return run_command(job["command"], job.get("timeout_s", 60))
        if job["type"] == "http":
            url = f"http://127.0.0.1:{CLIENT_SERVICE_PORT}{job.get('path', '/')}"
            try:
                with urllib.request.urlopen(url, timeout=15) as resp:
                    return {"status": resp.status,
                            "content_type": resp.headers.get("Content-Type", "text/html"),
                            "body": resp.read().decode("utf-8", "replace")}
            except Exception as exc:
                return {"status": 502, "content_type": "text/plain",
                        "body": f"device service error: {exc}"}
        return {"error": f"unknown job type {job.get('type')}"}

    def run(self) -> None:
        start_hosted_service(CLIENT_SERVICE_PORT, self.name)
        print(f"Monico bridge: hosted service on 127.0.0.1:{CLIENT_SERVICE_PORT}", flush=True)
        threading.Thread(target=self._specs_loop, daemon=True).start()
        backoff = 2
        while True:
            try:
                self.register()
                print(f"Monico bridge: connected to {self.server} as {self.name} "
                      f"({self.device_id}). This device is now a server.", flush=True)
                backoff = 2
                while True:
                    resp = self._req("GET", f"/bridge/poll?device_id={self.device_id}",
                                     timeout=35)
                    job = resp.get("job")
                    if not job:
                        continue
                    result = self._handle_job(job)
                    self._req("POST", "/bridge/result",
                              {"id": job["id"], "result": result}, timeout=20)
            except KeyboardInterrupt:
                print("Monico bridge: disconnected by user.")
                return
            except Exception as exc:
                print(f"Monico bridge: link down ({exc}); retrying in {backoff}s", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)


def connect(server_url: str = "", key: str = "", name: str = "") -> None:
    saved = load_json(CLIENT_CONFIG)
    server_url = server_url or os.environ.get("MONICO_SERVER", "") or saved.get("server_url", "")
    key = key or os.environ.get("MONICO_TOKEN", "") or saved.get("key", "")
    name = name or saved.get("name", "") or os.environ.get("MONICO_NAME", "") \
        or (detect().get("device_model") or "termux-device")
    if not server_url or not key:
        raise SystemExit("Need a server URL and pairing key. "
                         "Run install-termux.sh first, or set MONICO_SERVER and MONICO_TOKEN.")
    Client(server_url, key, name).run()
