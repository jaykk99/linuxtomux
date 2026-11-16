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

import hashlib
import hmac
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

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
def start_hosted_service(port: int, name: str,
                         avoid: set[int] | None = None) -> ThreadingHTTPServer:
    """Bind the phone's hosted service, falling back to the next free
    port when the requested one is already taken (e.g. another local
    service on 8899). Returns the server; the port actually bound is
    server.server_address[1]. Raises the last OSError only if every
    port in the fallback range is busy."""
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

    last_exc = None
    skip = avoid or set()
    for candidate in range(port, port + 20):
        if candidate in skip:
            # The server we talk to is on this machine and owns that port.
            continue
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), S)
            break
        except OSError as exc:
            last_exc = exc
    else:
        raise last_exc
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# ----------------------------------------------------------------------
# Bridge channel
# ----------------------------------------------------------------------
class Client:
    def __init__(self, server_url: str, key: str, name: str,
                 server_id: str = ""):
        self.server = server_url.rstrip("/")
        self.key = key
        self.name = name
        # Which server this phone paired with, so a beacon from another
        # bridge on the same LAN can never hijack the connection.
        self.server_id = server_id
        # Port the hosted service actually ends up on; run() updates
        # this if start_hosted_service falls back to a free port.
        self.service_port = CLIENT_SERVICE_PORT
        # Set by stop(). The retry loop used to be an infinite `while True`
        # with no way out short of killing the process, so a caller that
        # closed the server (every test, and the phone app when it shuts
        # down) left a thread printing "link down" forever. Nothing spins
        # after stop(): the loop checks the flag and the backoff sleeps on
        # it, so a stop is immediate instead of up to 30s late.
        self._stop = threading.Event()
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
        # Keep the server id: it is how this phone recognises the server's
        # beacon after the server's address changes (and it must survive
        # every reconnect, not just the first pairing).
        save_json(CLIENT_CONFIG, {"server_url": self.server, "key": self.key,
                                  "server_id": self.server_id,
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
        while not self._stop.is_set():
            if not self._pause(max(2.0, SPECS_INTERVAL_S)):
                return
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

    def _repair_pairing(self) -> bool:
        """The server rejected our key. Trade a fresh 4-digit code for a
        new one (the code is on the Linux screen: `monico-pair`), or give
        up with an instruction a person can follow."""
        print(stale_key_message(self.server), flush=True)
        try:
            code = _prompt_pair_code()
        except SystemExit as exc:
            print(str(exc), file=sys.stderr, flush=True)
            forget_stale_key()
            return False
        try:
            key, server_id = _exchange_code(self.server, code)
        except SystemExit as exc:
            print(str(exc), file=sys.stderr, flush=True)
            forget_stale_key()
            return False
        self.key = key
        if server_id:
            self.server_id = server_id
        saved = load_json(CLIENT_CONFIG) or {}
        saved.update({"server_url": self.server, "key": self.key,
                      "server_id": self.server_id, "name": self.name,
                      "device_id": self.device_id})
        save_json(CLIENT_CONFIG, saved)
        print("Monico bridge: re-paired. Continuing.", flush=True)
        return True

    def _follow_server(self) -> bool:
        """The saved address stopped answering. Ask the network where the
        server is now (authenticated with our key), then fall back to
        listening for its beacon; take the address it gives us and
        reconnect, instead of looping on a stale IP."""
        if self._stop.is_set():
            return False
        stopped = self._stop.is_set
        found = _probe_for_server(self.key, should_stop=stopped)
        if not _beacon_matches(found, self.server_id):
            found = _listen_for_beacon(self.server_id, should_stop=stopped)
        if not _beacon_matches(found, self.server_id):
            return False
        url = found["server"]
        if url == self.server:
            return False
        print(f"Monico bridge: server found at {url} (the address changed); reconnecting.",
              flush=True)
        self.server = url
        saved = load_json(CLIENT_CONFIG) or {}
        saved.update({"server_url": url, "server_id": self.server_id})
        save_json(CLIENT_CONFIG, saved)
        return True

    def _handle_job(self, job: dict) -> dict:
        if job["type"] == "exec":
            return run_command(job["command"], job.get("timeout_s", 60))
        if job["type"] == "http":
            url = f"http://127.0.0.1:{self.service_port}{job.get('path', '/')}"
            try:
                with urllib.request.urlopen(url, timeout=15) as resp:
                    return {"status": resp.status,
                            "content_type": resp.headers.get("Content-Type", "text/html"),
                            "body": resp.read().decode("utf-8", "replace")}
            except Exception as exc:
                return {"status": 502, "content_type": "text/plain",
                        "body": f"device service error: {exc}"}
        return {"error": f"unknown job type {job.get('type')}"}

    def _reserved_ports(self) -> set[int]:
        """Ports the hosted service must not take: when the server is on
        this same machine (Chromebook container, adb reverse), its port is
        ours too, and binding it would answer our own requests."""
        ports = set()
        try:
            u = urlparse(self.server)
            if u.hostname in ("127.0.0.1", "localhost", "::1"):
                ports.add(u.port or 80)
        except Exception:
            pass
        return ports

    def stop(self) -> None:
        """Ask run() to finish. Safe from any thread, and safe twice."""
        self._stop.set()

    def _pause(self, seconds: float) -> bool:
        """Sleep unless stop() arrives first. True means "keep going"."""
        return not self._stop.wait(seconds)

    def run(self) -> int:
        httpd = start_hosted_service(CLIENT_SERVICE_PORT, self.name,
                                     avoid=self._reserved_ports())
        self.service_port = httpd.server_address[1]
        note = "" if self.service_port == CLIENT_SERVICE_PORT else \
            f" (port {CLIENT_SERVICE_PORT} was busy)"
        print(f"Monico bridge: hosted service on 127.0.0.1:{self.service_port}{note}",
              flush=True)
        threading.Thread(target=self._specs_loop, daemon=True).start()
        backoff = 2
        failures = 0
        while not self._stop.is_set():
            try:
                self.register()
                print(f"Monico bridge: connected to {self.server} as {self.name} "
                      f"({self.device_id}). This device is now a server.", flush=True)
                backoff = 2
                failures = 0
                while not self._stop.is_set():
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
                return 0
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    # The server does not honour this key any more (fresh
                    # install, regenerated pairing key). Retrying with the
                    # same key can only fail: repair the pairing, or stop
                    # with instructions instead of spinning forever.
                    if self._repair_pairing():
                        backoff = 2
                        continue
                    return 1
                if self._stop.is_set():
                    return 0
                print(f"Monico bridge: link down ({exc}); retrying in {backoff}s", flush=True)
                if not self._pause(backoff):
                    return 0
                backoff = min(backoff * 2, 30)
            except Exception as exc:
                # A dead address is not always a dead server: DHCP may have
                # moved it. Listen for its beacon before backing off.
                if self._stop.is_set():
                    return 0
                print(f"Monico bridge: link down ({exc}); retrying in {backoff}s", flush=True)
                if self._follow_server():
                    backoff = 2
                    continue
                if failures == 0:
                    print(LINK_HINT, flush=True)
                failures += 1
                if not self._pause(backoff):
                    return 0
                backoff = min(backoff * 2, 30)


DISCOVERY_UDP_PORT = 18766  # same port the server listens on
DISCOVERY_ROUNDS = 2        # broadcast rounds before giving up
DISCOVERY_WAIT_S = 2.5      # seconds listening per round
BEACON_WAIT_S = 6.0         # seconds to listen for a server's beacon
PROBE_WAIT_S = 2.5          # seconds to wait for an answer to our probe


def _prompt_pair_code() -> str:
    """Ask for the 4-digit code from the Linux screen. Reads from
    /dev/tty when stdin is not a terminal (piped runs), else stdin,
    so the prompt works however monico-connect was started."""
    tty = None
    try:
        tty = open("/dev/tty", "r")
    except OSError:
        tty = None
    src = tty or sys.stdin
    try:
        print("Enter the 4-digit code: ", end="", flush=True, file=sys.stderr)
        code = src.readline().strip()
    finally:
        if tty is not None:
            tty.close()
    if len(code) != 4 or not code.isdigit():
        raise SystemExit("That is not a 4-digit code. "
                         "Run monico-connect again with the code from "
                         "the Linux screen (monico-pair for a fresh one).")
    return code


def _discovery_payload(code: str) -> bytes:
    """The broadcast packet: a type tag and the code, nothing else.
    Split out so tests can prove the wire format without a socket."""
    return json.dumps({"type": "monico-discover", "code": code}).encode()


def _parse_discovery_reply(data: bytes) -> str | None:
    """A discovery reply in, the server URL out (or None for anything
    that is not a well-formed monico-here reply)."""
    try:
        msg = json.loads(data.decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(msg, dict) or msg.get("type") != "monico-here":
        return None
    return str(msg.get("server") or "") or None


def _discover_server(code: str) -> str | None:
    """Find a server on the local network that knows this code. A UDP
    broadcast to every interface plus a loopback unicast (adb-reverse
    setups reach the server at 127.0.0.1); the first server that
    answers wins. A wrong code gets silence, so no reply means the
    code is wrong/expired, the server is not running, or the phone
    is not on the same network. Returns the server URL or None."""
    payload = _discovery_payload(code)
    targets = [("255.255.255.255", DISCOVERY_UDP_PORT),
               ("127.0.0.1", DISCOVERY_UDP_PORT)]
    for _round in range(DISCOVERY_ROUNDS):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(DISCOVERY_WAIT_S)
            for target in targets:
                try:
                    sock.sendto(payload, target)
                except OSError:
                    pass
            deadline = time.time() + DISCOVERY_WAIT_S
            while time.time() < deadline:
                try:
                    data, _addr = sock.recvfrom(1024)
                except socket.timeout:
                    break
                except OSError:
                    break
                server_url = _parse_discovery_reply(data)
                if server_url:
                    return server_url
        except OSError:
            return None
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    return None


LINK_HINT = ("Monico bridge: still off the network. If the Linux box was "
             "reinstalled or moved, run monico-pair there and start this "
             "again with the new code: monico-connect <code>")


def stale_key_message(server: str) -> str:
    """What to tell someone whose saved key the server refuses."""
    return (f"Monico bridge: {server} rejected this phone's pairing key "
            "(it was regenerated, or the server was reinstalled). "
            "Pair again with a fresh code.")


def _parse_beacon(data: bytes) -> dict | None:
    """One beacon (or discovery reply) in, a normalised record out.

    Both packets carry the same shape: {"type": "monico-here", "server":
    "http://ip:port", "name": ..., "id": ...}. Anything else is None."""
    try:
        msg = json.loads(data.decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(msg, dict) or msg.get("type") != "monico-here":
        return None
    url = str(msg.get("server") or "").strip().rstrip("/")
    if not url.startswith("http://") and not url.startswith("https://"):
        return None
    return {"server": url, "id": str(msg.get("id") or ""),
            "name": str(msg.get("name") or "")}


def _beacon_matches(found: dict | None, want_id: str) -> bool:
    """Only the server this phone paired with may replace the saved URL.
    No saved id means no matching: guessing is how a phone ends up on
    somebody else's bridge."""
    if not found or not want_id:
        return False
    return found.get("id") == want_id


def _probe_payload(key: str, nonce: str) -> bytes:
    """The "where are you now?" packet: a nonce and its HMAC under the
    saved pairing key. The key never travels, and only the server that
    knows it can answer."""
    mac = hmac.new(key.encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return json.dumps({"type": "monico-who", "nonce": nonce, "mac": mac}).encode()


DISCOVERY_SLICE_S = 0.2  # recv timeout while waiting: small enough that a
                         # stop is noticed in a fraction of a second, large
                         # enough not to spin


def _probe_for_server(key: str, seconds: float | None = None,
                      should_stop=None) -> dict | None:
    """Ask the local network where our server lives, authenticated with
    the pairing key we already hold. Sent from an ephemeral port, so it
    never fights the server's own listener for the discovery port, and
    it works even where a broadcast beacon cannot be received."""
    if not key:
        return None
    if seconds is None:
        seconds = PROBE_WAIT_S
    payload = _probe_payload(key, secrets.token_hex(16))
    targets = [("255.255.255.255", DISCOVERY_UDP_PORT),
               ("127.0.0.1", DISCOVERY_UDP_PORT)]
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(DISCOVERY_SLICE_S)
        for target in targets:
            try:
                sock.sendto(payload, target)
            except OSError:
                continue
        deadline = time.time() + seconds
        while time.time() < deadline:
            if should_stop is not None and should_stop():
                return None
            try:
                data, _addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            found = _parse_beacon(data)
            if found:
                return found
    except OSError:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
    return None


def _listen_for_beacon(want_id: str, seconds: float | None = None,
                       should_stop=None) -> dict | None:
    """Listen on the discovery port for a while for a server announcing
    itself. Returns the first beacon from the server we paired with (or
    the first one seen, which the caller then matches)."""
    if seconds is None:
        seconds = BEACON_WAIT_S
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", DISCOVERY_UDP_PORT))
        except OSError:
            return None
        sock.settimeout(DISCOVERY_SLICE_S)
        deadline = time.time() + seconds
        while time.time() < deadline:
            if should_stop is not None and should_stop():
                return None
            try:
                data, _addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            found = _parse_beacon(data)
            if found and _beacon_matches(found, want_id):
                return found
    except OSError:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
    return None


def forget_stale_key() -> None:
    """Drop a key the server rejected, keep the rest of the pairing record
    (name, device id, server, server id) so the next monico-connect pairs
    with a fresh code instead of retrying a dead key."""
    saved = load_json(CLIENT_CONFIG)
    if not saved or "key" not in saved:
        return
    saved.pop("key", None)
    save_json(CLIENT_CONFIG, saved)


def _exchange_code(server_url: str, code: str) -> tuple[str, str]:
    """Trade the 4-digit code for the pairing key at the discovered
    server. Returns (key, server_id); the id is what lets this phone
    follow the server to a new address later. Loud failure, never
    silent: a bad code or a dead server is an error message and exit 1,
    not an empty install."""
    url = server_url.rstrip("/") + f"/pair?code={code}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError:
        raise SystemExit("ERROR: bad or expired pair code. Run monico-pair "
                         "on the Linux box for a fresh code (codes last "
                         "10 minutes, one use).")
    except Exception:
        raise SystemExit(f"ERROR: could not reach {server_url}. Is the "
                         "server still running, and is the phone on the "
                         "same WiFi?")
    key = body.get("key") or ""
    if not key:
        raise SystemExit("ERROR: bad or expired pair code. Run monico-pair "
                         "on the Linux box for a fresh code.")
    return key, str(body.get("server_id") or "")


def _pair_with_code(code: str) -> tuple[str, str, str]:
    """Full code flow: find the server by broadcast, exchange the code
    for the pairing key, save both (0600) for next time."""
    server_url = _discover_server(code)
    if not server_url:
        raise SystemExit("ERROR: No server found with that code. Same WiFi? "
                         "Server running? Code right? (monico-pair on the "
                         "Linux box prints a fresh code.)")
    key, server_id = _exchange_code(server_url, code)
    save_json(CLIENT_CONFIG, {"server_url": server_url, "key": key,
                              "server_id": server_id})
    print(f"Paired with {server_url}.", flush=True)
    return server_url, key, server_id


def connect(server_url: str = "", key: str = "", name: str = "",
            code: str = "") -> int:
    saved = load_json(CLIENT_CONFIG)
    server_url = server_url or os.environ.get("MONICO_SERVER", "") or saved.get("server_url", "")
    key = key or os.environ.get("MONICO_TOKEN", "") or saved.get("key", "")
    server_id = saved.get("server_id", "")
    name = name or saved.get("name", "") or os.environ.get("MONICO_NAME", "") \
        or (detect().get("device_model") or "termux-device")
    if not server_url or not key:
        # No saved pairing yet: the 4-digit code from the Linux
        # screen is the whole flow. A --server URL skips discovery
        # (public tunnel / adb-reverse); otherwise the phone finds
        # the server on the local network by itself.
        code = (code or "").strip()
        if not code:
            code = _prompt_pair_code()
        if len(code) != 4 or not code.isdigit():
            raise SystemExit("That is not a 4-digit code. Run monico-connect "
                             "again with the code from the Linux screen.")
        if server_url and not key:
            key, server_id = _exchange_code(server_url, code)
            save_json(CLIENT_CONFIG, {"server_url": server_url, "key": key,
                                      "server_id": server_id, "name": name})
        else:
            server_url, key, server_id = _pair_with_code(code)
    return Client(server_url, key, name, server_id).run()
