"""Monico Labs bridge, Linux side.

Run it:  monico-server start [--join http://<other-linux>:8765] [--public]

Add --public to expose the dashboard on the internet through a free
Cloudflare quick tunnel (no account, no card): the public URL is
printed once cloudflared reports it. Without --public the server is
LAN-only; the phone on the same WiFi opens http://<linux-lan-ip>:8765/
directly, no internet needed anywhere. The dashboard itself is behind
a PIN gate (local URL and public URL alike).

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

import base64
import hashlib
import hmac
import http.client
import json
import queue
import secrets
import select
import socket
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .apps import APPS, AppProbe
from .common import (DATA_DIR, KEY_FILE, VERSION, blocked_reason,
                     check_token, ensure_data_dir, get_or_create_key,
                     get_or_create_server_id, log, model_status,
                     run_command)
from . import pools as poolmod
from .ai_gateway import MODEL_ALIAS, AIGateway, GatewayError, openai_error
from .hwdetect import detect
from .settings import AIKeyStore, LocalAppStore
from .specs import clean_specs, live_specs

UI_FILE = Path(__file__).resolve().parent.parent / "ui" / "homescreen.html"
DASHBOARD_FILE = UI_FILE.parent / "dashboard.html"
SETTINGS_FILE = UI_FILE.parent / "settings.html"

# PIN gate for the dashboard (/dashboard), which is also the public
# URL when --public is on. Only the SHA-256 hash of the PIN is stored
# here; the PIN itself is never in the repo, never printed, never
# logged, and never appears in the dashboard HTML/JS. A successful PIN
# login mints a random session token (12h cookie); the token, not the
# PIN, is what the browser presents afterwards.
PIN_HASH = "5daa4478509ac22f36329f620ac7e22dd140d5fad7ff710b6670f7329ac217f7"
PIN_SESSION_TTL_S = 12 * 3600
PIN_COOKIE = "monico_pin"

# OpenAI-compatible /v1 responses are usable from browser-based AI
# clients too: CORS is open, while the mk-ai bearer key is still
# required for every real request (PROPFIND-style preflights send no
# credentials, so OPTIONS carries no data and no auth).
AI_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
}
PIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Monico Labs - PIN gate</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;600;700&family=Inter+Tight:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet" />
<link rel="stylesheet" href="/console.css">
</head>
<body class="auth-page">
<main class="auth">
  <div class="auth-brand">
    <span class="mark auth-mark"><i></i></span>
    <div class="auth-title"><h1>PIN <em>Gate</em></h1><div class="sub">Dashboard access</div></div>
  </div>
  <form class="auth-form" method="POST" action="/pin-login">
    <div class="form-top"><span class="pulse-dot"></span> Restricted channel <span class="auth-clock" id="hud-clock">--:--:--</span></div>
    <label for="pin">PIN</label>
    <input id="pin" name="pin" type="password" inputmode="numeric" autocomplete="off" placeholder="...." autofocus>
    <p class="auth-lead">Enter PIN to open the live dashboard.</p>
    <button type="submit">Unlock deck</button>
    <div class="auth-foot"><span>Dashboard only</span><span>Pairing key = owner</span></div>
  </form>
  <p class="auth-legal" style="font-size:10px;letter-spacing:.06em;text-transform:none">Pairing key instead? <a href="/login?next=/dashboard" style="color:var(--acid)">Sign in on the home page</a></p>
</main>
<script>(function(){var c=document.getElementById("hud-clock");if(!c)return;function t(){c.textContent=new Date().toISOString().slice(11,19)+" UTC"}t();setInterval(t,1000)})();</script>
</body></html>"""
LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Monico Labs — Bridge</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;600;700&family=Inter+Tight:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet" />
<link rel="stylesheet" href="/console.css">
</head>
<body class="auth-page">
<main class="auth">
  <div class="auth-brand">
    <span class="mark auth-mark"><i></i></span>
    <div class="auth-title"><h1>Monico <em>Labs</em></h1><div class="sub">Console management suite</div></div>
  </div>
  <form class="auth-form" method="POST" action="/login">
    <div class="form-top"><span class="pulse-dot"></span> Secure channel <span class="auth-clock" id="hud-clock">--:--:--</span></div>
    <label for="key">Pairing key</label>
    <input id="key" name="key" type="password" placeholder="..........." autocomplete="off" autofocus>
    <div class="hint">Owner access. The key was shown once when this server was set up.</div>
    <button type="submit">Enter the deck</button>
    <div class="auth-foot"><span>Local-first</span><span>Zero cloud</span><span>2026</span></div>
  </form>
  <div class="auth-legal">Monico Labs // bridge console // owner access</div>
</main>
<script>(function(){var c=document.getElementById("hud-clock");if(!c)return;function t(){c.textContent=new Date().toISOString().slice(11,19)+" UTC"}t();setInterval(t,1000)})();</script>
</body></html>"""

ONLINE_WINDOW_S = 40     # direct device is online if seen this recently
REMOTE_FRESH_S = 45      # a peer's claims are trusted this long
SYNC_INTERVAL_S = 5      # seconds between peer registry syncs
HW_REFRESH_S = 30        # re-detect own hardware at most this often,
                         # so a USB drive plugged in after startup
                         # shows up without a server restart
SPECS_CACHE_S = 4        # this server's own live specs are re-read
                         # at most this often (the dashboard polls)


class Bridge:
    def __init__(self, name: str | None = None, server_id: str | None = None) -> None:
        self.server_id = server_id or get_or_create_server_id()
        self.hw = detect()
        self._hw_at = time.time()
        self._specs: dict | None = None
        self._specs_at = 0.0
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
        # Pools: devices paired to act as one (see bridge/pools.py).
        self.pools_file = DATA_DIR / "pools.json"
        self.pools: dict[str, dict] = poolmod.load_pools(self.pools_file)
        self.inflight: dict[str, int] = {}
        self.last_used: dict[str, int] = {}
        self._deal_seq = 0
        # PIN sessions for the dashboard: token -> expiry epoch.
        # In-memory only; a server restart logs everyone out.
        self.pin_sessions: dict[str, float] = {}
        # Owner settings: AI API keys (stored hashed) and the local app
        # shelf (repos/apps/software on this box).
        self.ai_keys = AIKeyStore()
        self.local_apps = LocalAppStore()
        # OpenAI-compatible endpoint (keys above), forwarded to the
        # installed llama.cpp model.
        self.ai_gateway = AIGateway(self.ai_keys)

    # -- device registry -------------------------------------------------
    def register(self, info: dict) -> dict:
        did = info["device_id"]
        with self.lock:
            dev = self.devices.get(did, {})
            info = dict(info)
            specs = clean_specs(info.pop("specs", None))
            dev.update(info)
            dev["last_seen"] = time.time()
            if specs:
                dev["specs"] = specs
                dev["specs_at"] = dev["last_seen"]
            self.devices[did] = dev
            self.queues.setdefault(did, queue.Queue())
        log(f"register {did} ({info.get('name')})")
        return {"ok": True, "device_id": did}

    def set_specs(self, did: str, specs) -> bool:
        """A directly connected device reporting its live specs (RAM,
        storage, CPU, uptime, battery). Counts as a sign of life."""
        specs = clean_specs(specs)
        with self.lock:
            dev = self.devices.get(did)
            if dev is None or specs is None:
                return False
            now = time.time()
            dev["specs"] = specs
            dev["specs_at"] = now
            dev["last_seen"] = now
        return True

    def own_specs(self) -> dict:
        """This server's own live specs, briefly cached so a busy
        dashboard does not re-read /proc on every request."""
        now = time.time()
        if self._specs is None or now - self._specs_at > SPECS_CACHE_S:
            try:
                self._specs = live_specs()
            except Exception:
                self._specs = self._specs or {}
            self._specs_at = now
        return self._specs

    def _self_record(self) -> dict:
        specs = self.own_specs()
        return {
            "device_id": self.server_id, "name": self.name,
            "platform": "linux", "kind": "server", "role": "Linux server",
            "online": True, "hw": self.hw, "model": model_status(),
            "hosted": ["Monico Bridge homescreen", "Bridge command channel"],
            "specs": specs,
            "specs_age_s": int(max(0, time.time() - self._specs_at)),
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
            "specs": dev.get("specs"),
            "specs_age_s": (int(max(0, now - dev["specs_at"]))
                            if dev.get("specs_at") else None),
            "origin": self.server_id, "origin_name": self.name,
        }

    def _refresh_hw(self) -> None:
        """Re-run hardware detection at most every HW_REFRESH_S, so
        hot-plugged storage (a USB drive) appears in device info
        without restarting the server."""
        now = time.time()
        if now - self._hw_at < HW_REFRESH_S:
            return
        self._hw_at = now
        try:
            self.hw = detect()
        except Exception:
            pass

    def snapshot(self) -> list[dict]:
        """Records for devices hosted HERE (this server + direct clients)."""
        self._refresh_hw()
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
            age = rec.get("specs_age_s")
            if isinstance(age, (int, float)):
                # the peer measured this age when it sent the claim
                age = int(age + max(0, now - claim["at"]))
            out[did] = {**rec, "local": False, "self": False,
                        "via": claim.get("via_name"),
                        "specs_age_s": age,
                        "online": bool(rec.get("online")) and fresh}
        return list(out.values())

    def device_list(self) -> list[dict]:
        return self.known()

    def app_list(self) -> list[dict]:
        """Hosted-app entries for the homescreen, each with a live
        (briefly cached) probe status. Apps are the same on every
        server in the mesh: they are web properties, not devices."""
        return self.app_probe.statuses(APPS)

    # -- pools: paired devices that act as one ---------------------------
    def pool_create(self, name: str, members: list) -> dict:
        name = poolmod.clean_name(name)
        if not name:
            return {"error": "pool needs a name (letters, numbers, - _ .)"}
        if name in ("linux", "server", "this", "phone"):
            return {"error": f"{name!r} is a reserved target name"}
        known = {d["device_id"]: d for d in self.known()}
        members = list(dict.fromkeys(str(m) for m in members or []))
        if not 2 <= len(members) <= poolmod.MAX_MEMBERS:
            return {"error": f"a pool pairs 2 to {poolmod.MAX_MEMBERS} devices"}
        unknown = [m for m in members if m not in known]
        if unknown:
            return {"error": f"unknown device(s): {', '.join(unknown)}"}
        if any(d.get("name") == name for d in known.values()):
            return {"error": f"a device is already named {name!r}"}
        with self.lock:
            if any(p["name"] == name for p in self.pools.values()):
                return {"error": f"a pool named {name!r} already exists"}
            pid = poolmod.new_pool_id()
            self.pools[pid] = {"name": name, "members": members,
                               "created": time.time()}
            poolmod.save_pools(self.pools_file, self.pools)
        log(f"pool create {pid} {name} members={members}")
        return {"ok": True, "pool": self.pool_view(pid)}

    def pool_delete(self, pid: str) -> dict:
        with self.lock:
            if pid not in self.pools:
                return {"error": "unknown pool"}
            del self.pools[pid]
            poolmod.save_pools(self.pools_file, self.pools)
        log(f"pool delete {pid}")
        return {"ok": True}

    def pool_find(self, ref: str) -> str | None:
        with self.lock:
            if ref in self.pools:
                return ref
            for pid, p in self.pools.items():
                if p["name"] == ref:
                    return pid
        return None

    def _pool_members(self, pool: dict, known: dict[str, dict]) -> list[dict]:
        out = []
        for did in pool["members"]:
            d = known.get(did)
            if d is None:   # its server left the mesh, or it was renamed away
                out.append({"device_id": did, "name": did, "online": False,
                            "missing": True, "specs": None})
                continue
            member = {k: d.get(k) for k in
                      ("device_id", "name", "platform", "kind", "online",
                       "via", "self", "hw", "specs", "specs_age_s")}
            member["tight"] = (poolmod.is_tight(d)
                               if d.get("online") else None)
            member["busy"] = self.inflight.get(did, 0)
            out.append(member)
        return out

    def pool_view(self, pid: str, known: dict[str, dict] | None = None) -> dict | None:
        with self.lock:
            pool = self.pools.get(pid)
        if pool is None:
            return None
        if known is None:
            known = {d["device_id"]: d for d in self.known()}
        members = self._pool_members(pool, known)
        return {"id": pid, "name": pool["name"], "members": members,
                "online": sum(1 for m in members if m["online"]),
                "combined": poolmod.combine(members)}

    def pool_list(self) -> list[dict]:
        known = {d["device_id"]: d for d in self.known()}
        with self.lock:
            ids = list(self.pools)
        return [v for v in (self.pool_view(pid, known) for pid in ids) if v]

    def pool_exec(self, ref: str, commands: list[str], mode: str = "split",
                  timeout_s: float = 60) -> dict:
        """The pool's one terminal. "split": each command goes to one
        member, dealt out so the load is shared (half and half on two
        healthy devices). "all": every command on every online member.
        Everything runs at the same time; results come back in order."""
        pid = self.pool_find(ref)
        view = self.pool_view(pid) if pid else None
        if view is None:
            return {"error": f"unknown pool {ref!r}"}
        commands = [c.strip() for c in commands or [] if str(c).strip()]
        if not commands:
            return {"error": "command required"}
        if len(commands) > poolmod.MAX_BATCH:
            return {"error": f"at most {poolmod.MAX_BATCH} commands at once"}
        online = [m for m in view["members"] if m["online"]]
        if not online:
            return {"error": f"no device in pool {view['name']!r} is online"}
        names = {m["device_id"]: m["name"] for m in view["members"]}
        with self.lock:
            if mode == "all":
                plan = [(cmd, m["device_id"]) for cmd in commands for m in online]
                for _cmd, did in plan:
                    self.inflight[did] = self.inflight.get(did, 0) + 1
            else:
                mode = "split"
                picks = poolmod.deal(view["members"], len(commands),
                                     self.inflight, self.last_used,
                                     self._deal_seq)
                self._deal_seq += len(commands)
                plan = list(zip(commands, picks))
        results: list[dict | None] = [None] * len(plan)

        def run(i: int, cmd: str, did: str) -> None:
            try:
                res = self.submit(did, {"type": "exec", "command": cmd,
                                        "timeout_s": timeout_s},
                                  wait_s=float(timeout_s) + 15)
            except Exception as exc:
                res = {"error": f"bridge error: {exc}"}
            finally:
                with self.lock:
                    self.inflight[did] = max(0, self.inflight.get(did, 1) - 1)
            results[i] = {"device_id": did, "device": names.get(did, did),
                          "command": cmd, **res}
            log(f"pool-exec pool={view['name']} on={did} cmd={cmd!r} "
                f"-> exit={res.get('exit_code')}")

        threads = [threading.Thread(target=run, args=(i, cmd, did), daemon=True)
                   for i, (cmd, did) in enumerate(plan)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(float(timeout_s) + 40)
        return {"pool": view["name"], "mode": mode,
                "results": [r or {"error": "no answer in time",
                                  "device_id": plan[i][1],
                                  "device": names.get(plan[i][1], plan[i][1]),
                                  "command": plan[i][0]}
                            for i, r in enumerate(results)]}

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
        # The sender's record about ITSELF carries its live specs. A
        # peer that joined us is also in our direct device list, and
        # the direct entry wins when rendering, so refresh it here.
        own = next((r for r in records or []
                    if r.get("device_id") == via_id), None)
        own_specs = clean_specs(own.get("specs")) if own else None
        with self.lock:
            peer_dev = self.devices.get(via_id)
            if peer_dev is not None and own_specs:
                age = own.get("specs_age_s")
                age = age if isinstance(age, (int, float)) else 0
                peer_dev["specs"] = own_specs
                peer_dev["specs_at"] = now - max(0, age)
                if isinstance(own.get("hw"), dict) and own["hw"]:
                    peer_dev["hw"] = own["hw"]
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

    # -- PIN gate (dashboard) --------------------------------------------
    def pin_login(self, pin: str) -> str | None:
        """Verify a PIN against the stored hash. Returns a random
        session token on success, None on failure. The PIN is never
        stored or logged; only its hash is compared, in constant time."""
        try:
            digest = hashlib.sha256(pin.encode()).hexdigest()
        except Exception:
            return None
        if not hmac.compare_digest(digest, PIN_HASH):
            return None
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.pin_sessions[token] = time.time() + PIN_SESSION_TTL_S
        return token

    def pin_authed(self, token: str | None) -> bool:
        if not token:
            return False
        with self.lock:
            exp = self.pin_sessions.get(token)
            if exp is None:
                return False
            if exp < time.time():
                del self.pin_sessions[token]
                return False
        return True

    # -- shared terminal ---------------------------------------------------
    def exec_all(self, command: str, timeout_s: float = 30) -> list[dict]:
        """Run one command on every online device at once: this server
        plus every online device in the mesh view, in parallel. Each
        result carries its device id/name so the dashboard can label
        the outputs. Reuses submit()/run_command(), so the
        destructive-command guard applies exactly as for /api/exec. A
        device that does not answer in time reports a timeout error
        instead of holding up the rest."""
        reason = blocked_reason(command)
        if reason:
            return [{"device_id": self.server_id, "name": self.name,
                     "error": reason, "blocked": True}]
        timeout_s = max(1, min(float(timeout_s or 30), 300))
        targets: list[tuple[str, str, str]] = [("self", self.server_id, self.name)]
        seen = {self.server_id}
        for rec in self.device_list():
            if not rec.get("online") or rec["device_id"] in seen:
                continue
            seen.add(rec["device_id"])
            targets.append(("device", rec["device_id"],
                            rec.get("name", rec["device_id"])))

        def _run(kind: str, did: str, name: str) -> dict:
            if kind == "self":
                return {"device_id": did, "name": name,
                        **run_command(command, timeout_s)}
            res = self.submit(did, {"type": "exec", "command": command,
                                    "timeout_s": timeout_s},
                              wait_s=timeout_s + 15)
            return {"device_id": did, "name": name, **res}

        with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
            return list(pool.map(lambda t: _run(*t), targets))


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
                "model": model_status(), "specs": b.own_specs(),
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

    def _start_sync_loop(self) -> None:
        """Registry sync on its own timer. It used to share the loop
        with the long-poll below, which holds for up to 25s, so a
        peer's devices (and now their live specs) could lag by that
        much. On its own thread the dashboard stays a few seconds
        fresh across servers. Errors are left to the main loop, which
        owns reconnecting."""
        if getattr(self, "_sync_thread", None):
            return

        def loop() -> None:
            while True:
                time.sleep(SYNC_INTERVAL_S)
                try:
                    self._sync()
                except Exception:
                    pass

        self._sync_thread = threading.Thread(target=loop, daemon=True)
        self._sync_thread.start()

    def run(self) -> None:
        backoff = 2
        while True:
            try:
                self._register()
                print(f"Monico bridge: joined mesh at {self.peer} "
                      f"(this server: {self.bridge.name})", flush=True)
                log(f"peer link up: {self.peer}")
                backoff = 2
                self._sync()
                self._start_sync_loop()
                while True:
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

        def _ai_authed(self) -> bool:
            """Auth for /v1/*: an mk-ai key from the settings page."""
            return BRIDGE.ai_gateway.authenticate(
                self.headers.get("Authorization")) is not None

        def _ai_headers(self, extra: dict | None = None) -> dict:
            headers = dict(AI_CORS)
            headers.update(extra or {})
            return headers

        def _ai_unauthorized(self):
            """401 in OpenAI's error shape. Closes the connection so an
            unread request body cannot desync a keep-alive socket."""
            self.close_connection = True
            return self._json(401, openai_error(
                "missing or invalid AI API key; create one on /settings",
                "invalid_request_error", "invalid_api_key"),
                self._ai_headers({"WWW-Authenticate": "Bearer",
                                  "Connection": "close"}))

        def _stream_ai(self, up) -> None:
            """Pass an upstream SSE stream through as chunked SSE."""
            self.send_response(up.status)
            self.send_header("Content-Type",
                             up.content_type or "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            for k, v in AI_CORS.items():
                self.send_header(k, v)
            self.end_headers()
            self.close_connection = True
            try:
                for line in up.iter_lines():
                    if not line:
                        continue
                    self.wfile.write(f"{len(line):X}\r\n".encode("ascii"))
                    self.wfile.write(line)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                up.close()

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

        def _pin_authed(self) -> bool:
            cookie = self.headers.get("Cookie", "")
            for part in cookie.split(";"):
                part = part.strip()
                if part.startswith(PIN_COOKIE + "="):
                    return BRIDGE.pin_authed(part.split("=", 1)[1])
            return False

        def _api_authed(self) -> bool:
            """Dashboard API auth: the pairing key (full owner access)
            or a valid PIN session (dashboard-only access)."""
            return self._authed() or self._pin_authed()

        def _proxy_authed(self) -> bool:
            """Auth for the internet-sharing proxy: Proxy-Authorization
            Basic (any username, pairing key as password) or the
            X-Pair-Key header. The proxy never serves unauthenticated
            strangers on the LAN."""
            auth = self.headers.get("Proxy-Authorization", "")
            if auth.startswith("Basic "):
                try:
                    decoded = base64.b64decode(auth[6:]).decode("utf-8", "replace")
                    _user, _, password = decoded.partition(":")
                    if check_token(password):
                        return True
                except Exception:
                    pass
            if check_token(self.headers.get("X-Pair-Key", "")):
                return True
            return False

        def _relay(self, client, upstream) -> None:
            """Blind byte relay between two sockets until either side
            closes (CONNECT tunnel). Runs in the handler thread; the
            server is threaded, so one tunnel does not block others."""
            self.close_connection = True
            for s in (client, upstream):
                try:
                    s.settimeout(300)
                except Exception:
                    pass

            def _copy(src, dst):
                try:
                    while True:
                        data = src.recv(65536)
                        if not data:
                            break
                        dst.sendall(data)
                except Exception:
                    pass
                finally:
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except Exception:
                        pass

            t1 = threading.Thread(target=_copy, args=(client, upstream), daemon=True)
            t2 = threading.Thread(target=_copy, args=(upstream, client), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        def _proxy_http(self, method: str):
            """Forward a plain-http absolute-URI request (phone's curl /
            tools via the Linux box). HTTPS goes through CONNECT, not
            here. Body capped at 50MB."""
            if not self._proxy_authed():
                return self._json(407, {"error": "proxy authentication required"},
                                  {"Proxy-Authenticate": 'Basic realm="Monico Labs"'})
            u = urlparse(self.path)
            if u.scheme != "http" or not u.hostname:
                return self._json(400, {"error": "proxy forwards http:// URLs; use CONNECT for https"})
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in ("host", "proxy-authorization",
                                            "proxy-connection", "connection")}
            try:
                conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=20)
                path = u.path or "/"
                if u.query:
                    path += "?" + u.query
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read(50 * 1024 * 1024)
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("connection", "transfer-encoding"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
            except Exception as exc:
                return self._json(502, {"error": f"proxy fetch failed: {exc}"})

        def do_CONNECT(self):
            """CONNECT tunnel for the internet-sharing proxy: the phone's
            browser/tools open https (or any TCP) through the Linux box.
            Pairing-key auth only; loopback targets are pointless
            through a proxy but harmless, so nothing is special-cased."""
            if not self._proxy_authed():
                body = b"Proxy authentication required"
                self.send_response(407)
                self.send_header("Proxy-Authenticate", 'Basic realm="Monico Labs"')
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            hostport = self.path
            if ":" in hostport:
                host, _, port_s = hostport.rpartition(":")
            else:
                host, port_s = hostport, "443"
            try:
                port = int(port_s)
            except ValueError:
                return self._json(400, {"error": "bad CONNECT target"})
            try:
                upstream = socket.create_connection((host, port), timeout=15)
            except Exception as exc:
                return self._json(502, {"error": f"cannot connect to {host}:{port}: {exc}"})
            try:
                self.send_response(200, "Connection Established")
                self.end_headers()
                self._relay(self.connection, upstream)
            finally:
                try:
                    upstream.close()
                except Exception:
                    pass

        # -- routes ---------------------------------------------------------
        def do_OPTIONS(self):
            """CORS preflight for /v1/* (no credentials, no data)."""
            p = urlparse(self.path).path
            if p.startswith("/v1/"):
                self.send_response(204)
                for k, v in AI_CORS.items():
                    self.send_header(k, v)
                self.send_header("Access-Control-Max-Age", "86400")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

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
            if p == "/console.css":
                # Shared visual layer for every console page, including
                # the signed-out login/PIN gates. It carries no data and
                # no secret, so it stays public like the installer.
                css = UI_FILE.parent / "console.css"
                if css.exists():
                    return self._send(200, css.read_bytes(),
                                      "text/css; charset=utf-8",
                                      {"Cache-Control": "no-cache"})
                return self._send(404, b"", "text/plain")
            if p == "/linuxtomux.tar.gz":
                # Public: the tarball carries no secret. The phone
                # downloads the exact code this server runs, no GitHub
                # and no internet on the phone needed.
                try:
                    data = _repo_tarball_bytes()
                except Exception as exc:
                    return self._json(500, {"error": f"tarball build failed: {exc}"})
                return self._send(200, data, "application/gzip",
                                  {"Content-Disposition": 'attachment; filename="linuxtomux.tar.gz"'})
            # Proxy-style absolute-URI request (phone via the Linux box):
            # handled before any bridge route, normal requests never
            # look like this.
            if self.path.startswith("http://"):
                return self._proxy_http("GET")
            if p == "/bridge/poll":
                if not self._authed():
                    return self._json(401, {"error": "bad pairing key"})
                qs = parse_qs(u.query)
                did = (qs.get("device_id") or [""])[0]
                return self._json(200, BRIDGE.poll(did))
            if p in ("/dashboard", "/dashboard/"):
                # Dashboard: PIN gate (public URL) or pairing key.
                if self._pin_authed() or self._authed():
                    html = (DASHBOARD_FILE.read_text() if DASHBOARD_FILE.exists()
                            else "<h1>Monico Labs</h1><p>dashboard.html missing</p>")
                    return self._send(200, html.encode(), "text/html; charset=utf-8")
                return self._send(200, PIN_HTML.encode(), "text/html; charset=utf-8")
            if p == "/specs":
                # Old dashboard URL: redirect, don't break bookmarks.
                return self._send(302, b"", "text/plain", {"Location": "/dashboard"})
            if p == "/api/devices":
                # Dashboard API: pairing key or PIN session.
                if not self._api_authed():
                    return self._json(401, {"error": "owner key or PIN required"})
                return self._json(200, {"devices": BRIDGE.device_list()})
            if p == "/api/pools":
                # Dashboard API: pairing key or PIN session, like
                # /api/devices above (the dashboard calls it).
                if not self._api_authed():
                    return self._json(401, {"error": "owner key or PIN required"})
                return self._json(200, {"pools": BRIDGE.pool_list()})
            if p in ("/settings", "/settings/"):
                # Owner settings page. Pairing key only: a PIN session is
                # dashboard-only access, too weak to mint credentials.
                if not self._authed():
                    login = LOGIN_HTML.replace(
                        'action="/login"', 'action="/login?next=/settings"')
                    return self._send(200, login.encode(),
                                      "text/html; charset=utf-8")
                html = (SETTINGS_FILE.read_text() if SETTINGS_FILE.exists()
                        else "<h1>Monico Labs</h1><p>settings.html missing</p>")
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if p == "/api/ai-keys":
                # AI API keys: owner key only, never a PIN session.
                if not self._authed():
                    return self._json(401, {"error": "owner key required"})
                return self._json(200, {"keys": BRIDGE.ai_keys.list()})
            if p == "/api/local-apps":
                # The local app shelf: owner key or PIN session.
                if not self._api_authed():
                    return self._json(401, {"error": "owner key or PIN required"})
                return self._json(200, {"apps": BRIDGE.local_apps.list()})
            if p == "/v1/models":
                # OpenAI-compatible: authenticated by an mk-ai key from
                # the settings page, NOT the pairing key.
                if not self._ai_authed():
                    return self._ai_unauthorized()
                return self._json(200, {"object": "list",
                                        "data": BRIDGE.ai_gateway.models()},
                                  self._ai_headers())
            if p == "/api/ai-engine":
                # Dashboard AI engine status (owner key or PIN session).
                if not self._api_authed():
                    return self._json(401, {"error": "owner key or PIN required"})
                return self._json(200, {"engine": BRIDGE.ai_gateway.status()})
            if not self._authed():
                if p == "/":
                    return self._send(200, LOGIN_HTML.encode(), "text/html; charset=utf-8")
                return self._json(401, {"error": "owner key required"})
            if p == "/":
                html = UI_FILE.read_text() if UI_FILE.exists() else "<h1>Monico Labs</h1>"
                return self._send(200, html.encode(), "text/html; charset=utf-8")
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
            # Proxy-style absolute-URI request (phone via the Linux box):
            # the proxy does its own auth (407), so this runs first.
            if self.path.startswith("http://"):
                return self._proxy_http("POST")
            if p == "/login":
                n = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(n).decode() if n else "")
                key = (form.get("key") or [""])[0]
                if check_token(key):
                    nxt = (parse_qs(u.query).get("next") or ["/"])[0]
                    if nxt not in ("/dashboard", "/settings"):   # fixed allow-list, no open redirect
                        nxt = "/"
                    return self._send(303, b"", "text/plain",
                                      {"Location": nxt,
                                       "Set-Cookie": f"monico_key={key}; Path=/; HttpOnly; SameSite=Lax"})
                return self._send(403, b"bad key", "text/plain")
            if p == "/pin-login":
                # Dashboard PIN gate: no pairing key needed, the PIN
                # itself is the credential. Never logs the PIN.
                n = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(n).decode() if n else "")
                pin = (form.get("pin") or [""])[0]
                token = BRIDGE.pin_login(pin)
                if token:
                    return self._send(303, b"", "text/plain",
                                      {"Location": "/dashboard",
                                       "Set-Cookie": f"{PIN_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"})
                err = PIN_HTML.replace("</form>", "<p style='color:#ff5a5a'>Wrong PIN. Try again.</p></form>")
                return self._send(403, err.encode(), "text/html; charset=utf-8")
            if p == "/api/exec-all":
                # Shared terminal: pairing key or PIN session.
                if not self._api_authed():
                    return self._json(401, {"error": "owner key or PIN required"})
                body = self._body()
                cmd = body.get("command", "")
                if not cmd:
                    return self._json(400, {"error": "command required"})
                results = BRIDGE.exec_all(cmd, body.get("timeout_s", 30))
                log(f"exec-all cmd={cmd!r} -> {len(results)} devices")
                return self._json(200, {"results": results})
            if p == "/api/pools":
                # Dashboard pools: pairing key or PIN session, like
                # /api/exec-all (the dashboard calls these).
                if not self._api_authed():
                    return self._json(401, {"error": "owner key or PIN required"})
                body = self._body()
                res = BRIDGE.pool_create(body.get("name", ""),
                                         body.get("members") or [])
                return self._json(400 if "error" in res else 200, res)
            if p == "/api/pools/delete":
                if not self._api_authed():
                    return self._json(401, {"error": "owner key or PIN required"})
                body = self._body()
                res = BRIDGE.pool_delete(body.get("id", ""))
                return self._json(404 if "error" in res else 200, res)
            if p == "/api/pool-exec":
                if not self._api_authed():
                    return self._json(401, {"error": "owner key or PIN required"})
                body = self._body()
                cmds = body.get("commands")
                if not isinstance(cmds, list):
                    cmds = [body.get("command", "")]
                res = BRIDGE.pool_exec(body.get("pool", ""),
                                       [str(c) for c in cmds],
                                       mode=body.get("mode", "split"),
                                       timeout_s=float(body.get("timeout_s", 60)))
                return self._json(400 if "error" in res else 200, res)
            if p == "/api/ai-keys":
                # Create an AI API key. The plaintext key is returned by
                # this one response and never again: only its hash is kept.
                if not self._authed():
                    return self._json(401, {"error": "owner key required"})
                res = BRIDGE.ai_keys.create(self._body().get("name", ""))
                return self._json(400 if "error" in res else 200, res)
            if p == "/api/ai-keys/delete":
                if not self._authed():
                    return self._json(401, {"error": "owner key required"})
                res = BRIDGE.ai_keys.revoke(self._body().get("id", ""))
                return self._json(404 if "error" in res else 200, res)
            if p == "/api/local-apps":
                # Add a repo, app or piece of software that runs locally.
                if not self._api_authed():
                    return self._json(401, {"error": "owner key or PIN required"})
                b = self._body()
                res = BRIDGE.local_apps.add(b.get("name", ""), b.get("kind", ""),
                                            b.get("path", ""), b.get("url", ""))
                return self._json(400 if "error" in res else 200, res)
            if p == "/api/local-apps/delete":
                if not self._api_authed():
                    return self._json(401, {"error": "owner key or PIN required"})
                res = BRIDGE.local_apps.remove(self._body().get("id", ""))
                return self._json(404 if "error" in res else 200, res)
            if p == "/api/ai-engine/start":
                # Explicit dashboard start: spawns llama-server and returns;
                # the panel polls /api/ai-engine while the model loads.
                if not self._api_authed():
                    return self._json(401, {"error": "owner key or PIN required"})
                res = BRIDGE.ai_gateway.start()
                if "error" in res:
                    code = 409 if res.get("code") == "engine_external" else 503
                    return self._json(code, res)
                return self._json(200, res)
            if p == "/api/ai-engine/stop":
                # Only stops a llama-server this bridge started.
                if not self._api_authed():
                    return self._json(401, {"error": "owner key or PIN required"})
                res = BRIDGE.ai_gateway.stop()
                if "error" in res:
                    return self._json(409, res)
                return self._json(200, res)
            if p in ("/v1/chat/completions", "/v1/completions"):
                # OpenAI-compatible chat/completion endpoint. mk-ai key
                # auth, forwarded to the installed llama.cpp model.
                if not self._ai_authed():
                    return self._ai_unauthorized()
                body = self._body()
                if p == "/v1/chat/completions" and not isinstance(
                        body.get("messages"), list):
                    return self._json(400, openai_error(
                        "messages must be a list of chat messages"),
                        self._ai_headers())
                if p == "/v1/completions" and not body.get("prompt"):
                    return self._json(400, openai_error(
                        "prompt is required"), self._ai_headers())
                body.setdefault("model", MODEL_ALIAS)
                try:
                    up = BRIDGE.ai_gateway.post(p, body)
                except GatewayError as exc:
                    log(f"ai {p} -> {exc.status}: {exc}")
                    return self._json(exc.status, exc.payload,
                                      self._ai_headers())
                if body.get("stream"):
                    return self._stream_ai(up)
                try:
                    data = up.read()
                finally:
                    up.close()
                return self._send(up.status, data,
                                  up.content_type or "application/json",
                                  self._ai_headers())
            if not self._authed():
                return self._json(401, {"error": "bad pairing key"})
            body = self._body()
            if p == "/bridge/register":
                if not body.get("device_id"):
                    return self._json(400, {"error": "device_id required"})
                return self._json(200, BRIDGE.register(body))
            if p == "/bridge/specs":
                did = body.get("device_id", "")
                if not did:
                    return self._json(400, {"error": "device_id required"})
                if not BRIDGE.set_specs(did, body.get("specs")):
                    # unknown device (server restarted) or junk payload
                    return self._json(404, {"error": "unknown device or bad specs",
                                            "reregister": True})
                return self._json(200, {"ok": True})
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


def _lan_ip() -> str | None:
    """This box's LAN address (for the phone on the same WiFi). The
    UDP-connect trick discovers the outbound interface without
    sending any traffic."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip if not ip.startswith("127.") else None
    except Exception:
        return None


def _termux_connect_block(lan_ip: str | None, port: int, key: str) -> str:
    """The ready-to-paste Termux block printed at startup: the real LAN
    IP and the real pairing key, no placeholders. Empty when there is
    no LAN address (nothing to paste yet). The curl line fetches the
    installer from this box itself, so it works with no internet on
    the phone as long as it is on the same WiFi."""
    if not lan_ip:
        return ""
    base = f"http://{lan_ip}:{port}"
    return (
        "Run this on your phone (Termux):\n"
        f"  curl -fsSL {base}/install-termux.sh | "
        f"MONICO_SERVER={base} MONICO_TOKEN={key} bash\n"
        "  monico-connect"
    )


TARBALL_FILES = ("monico-connect", "monico-cmd", "monico-model",
                 "install-termux.sh")
_TARBALL_CACHE: dict = {"sig": None, "data": b""}
_TARBALL_LOCK = threading.Lock()


def _repo_tarball_signature(root: Path) -> tuple:
    """Fingerprint of the files that go into the install tarball.
    Checking it is a directory walk; rebuilding is tar+gzip of the
    whole tree, so an unchanged checkout is served from cache."""
    sig = []
    for rel in ("bridge", "ui"):
        for f in sorted((root / rel).rglob("*")):
            if not f.is_file() or "__pycache__" in f.parts:
                continue
            st = f.stat()
            sig.append((str(f.relative_to(root)), st.st_mtime_ns,
                        st.st_size))
    for name in TARBALL_FILES:
        f = root / name
        if f.exists():
            st = f.stat()
            sig.append((name, st.st_mtime_ns, st.st_size))
    return tuple(sig)


def _repo_tarball_bytes() -> bytes:
    """Gzipped tar of the files install-termux.sh needs (bridge/, ui/,
    the launchers, the installer itself), built from this checkout so
    the phone gets the exact code this server runs. No GitHub, no
    internet on the phone.

    Cached until a source file changes; __pycache__/.pyc are excluded
    (the phone has its own interpreter, and bytecode for this box is
    dead weight over the LAN)."""
    import io
    import tarfile
    root = Path(__file__).resolve().parent.parent
    sig = _repo_tarball_signature(root)
    if _TARBALL_CACHE["sig"] == sig:
        return _TARBALL_CACHE["data"]
    with _TARBALL_LOCK:
        if _TARBALL_CACHE["sig"] == sig:
            return _TARBALL_CACHE["data"]

        def keep(info):
            return None if ("__pycache__" in Path(info.name).parts
                            or info.name.endswith(".pyc")) else info

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for rel in ("bridge", "ui"):
                tf.add(root / rel, arcname=rel, filter=keep)
            for name in TARBALL_FILES:
                tf.add(root / name, arcname=name, filter=keep)
        _TARBALL_CACHE["sig"] = sig
        _TARBALL_CACHE["data"] = buf.getvalue()
        return _TARBALL_CACHE["data"]


def serve(port: int, join_url: str | None = None, join_key: str | None = None,
          name: str | None = None, public: bool = False) -> None:
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
    print(f"Dashboard:   http://127.0.0.1:{port}/dashboard", flush=True)
    if is_new:
        print(f"Pairing key (shown once, saved 0600): {key}", flush=True)
    else:
        print(f"Pairing key: saved in {DATA_DIR}/pairing-key (0600)", flush=True)
    lan = _lan_ip()
    if lan:
        print(f"LAN dashboard: http://{lan}:{port}/dashboard (same WiFi, no internet needed)", flush=True)
        print(_termux_connect_block(lan, port, key), flush=True)
    else:
        print("No LAN address found: connect this box to WiFi to get the phone command.", flush=True)
    tunnel_proc = None
    if public:
        from .tunnel import (CLOUDFLARED_INSTALL_NOTE, find_cloudflared,
                             start_quick_tunnel)
        if not find_cloudflared():
            print(CLOUDFLARED_INSTALL_NOTE, flush=True)
        else:
            print("Starting Cloudflare quick tunnel (free, no account)...", flush=True)

            def _got(url: str) -> None:
                print(f"Public dashboard: {url}/dashboard", flush=True)
                print(f"Public home:   {url}/", flush=True)
                log(f"public tunnel up: {url}")

            tunnel_proc = start_quick_tunnel(port, _got)
    if join_url:
        token = join_key or key
        link = PeerLink(BRIDGE, join_url, token, port)
        threading.Thread(target=link.run, daemon=True).start()
        print(f"Joining mesh at {join_url} ...", flush=True)
    else:
        print("Waiting for devices to connect with monico-connect.", flush=True)
    print("=" * 60, flush=True)
    log(f"server start on :{port} id={BRIDGE.server_id} join={join_url or '-'} public={public}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if tunnel_proc is not None:
            from .tunnel import stop_tunnel
            stop_tunnel(tunnel_proc)


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
