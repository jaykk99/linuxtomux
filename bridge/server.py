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
from . import pools as poolmod
from .hwdetect import detect
from .specs import clean_specs, live_specs

UI_FILE = Path(__file__).resolve().parent.parent / "ui" / "homescreen.html"
DASHBOARD_FILE = UI_FILE.parent / "dashboard.html"
LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Monico Labs — Bridge</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet" />
<style>
  :root{--sans:"Inter Tight",ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
        --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
  *{box-sizing:border-box} ::selection{background:#fff;color:#000}
  body{margin:0;background:#070707;color:#e5e5e5;font-family:var(--sans);display:grid;place-items:center;min-height:100vh;padding:24px 16px}
  .wrap{display:flex;flex-direction:column;align-items:center;gap:28px;width:100%;max-width:380px}
  .brand{display:flex;flex-direction:column;align-items:center;gap:14px;text-align:center}
  .markwrap{position:relative}
  .markwrap:before{content:"";position:absolute;inset:-16px;border-radius:9999px;background:rgba(255,255,255,.05);filter:blur(24px)}
  .mark{position:relative;display:grid;place-items:center;width:80px;height:80px;border:1px solid #262626;border-radius:24px;background:#171717;box-shadow:0 25px 50px rgba(0,0,0,.6)}
  .mark i{display:block;width:30px;height:30px;border-radius:6px;background:#fff;transform:rotate(45deg)}
  h1{margin:0;font-size:24px;font-weight:900;letter-spacing:-.01em;text-transform:uppercase;color:#fff}
  .sub{font-family:var(--mono);font-size:9px;font-weight:600;letter-spacing:.18em;color:#737373;text-transform:uppercase}
  form{width:100%;border:1px solid #171717;border-radius:12px;background:rgba(10,10,10,.6);padding:20px}
  label{display:block;font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:.18em;text-transform:uppercase;color:#737373}
  input{margin-top:6px;height:40px;width:100%;border:1px solid #262626;border-radius:8px;background:#0a0a0a;padding:0 12px;font-family:var(--mono);font-size:12px;color:#f5f5f5}
  input::placeholder{color:#525252}
  input:focus{border-color:#737373;outline:none}
  .hint{margin-top:6px;font-size:11px;color:#525252}
  button{margin-top:16px;height:40px;width:100%;border:0;border-radius:8px;background:#fff;color:#0a0a0a;font-family:var(--mono);font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.5)}
  button:hover{background:#e5e5e5}
</style></head>
<body>
<div class="wrap">
  <div class="brand">
    <div class="markwrap"><div class="mark"><i></i></div></div>
    <div><h1>Monico Labs</h1><div class="sub">Console Management Suite</div></div>
  </div>
  <form method="POST" action="/login">
    <label for="key">Pairing key</label>
    <input id="key" name="key" type="password" placeholder="Pairing key" autocomplete="off">
    <div class="hint">Owner access. The key was shown once when this server was set up.</div>
    <button type="submit">Enter</button>
  </form>
</div>
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
            out.append({k: d.get(k) for k in
                        ("device_id", "name", "platform", "kind", "online",
                         "via", "self", "hw", "specs", "specs_age_s")}
                       | {"tight": poolmod.is_tight(d) if d.get("online") else None,
                          "busy": self.inflight.get(did, 0)})
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
                if p in ("/", "/dashboard", "/dashboard/"):
                    login = LOGIN_HTML
                    if p != "/":
                        login = login.replace('action="/login"',
                                              'action="/login?next=/dashboard"')
                    return self._send(200, login.encode(), "text/html; charset=utf-8")
                return self._json(401, {"error": "owner key required"})
            if p == "/":
                html = UI_FILE.read_text() if UI_FILE.exists() else "<h1>Monico Labs</h1>"
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if p in ("/dashboard", "/dashboard/"):
                html = (DASHBOARD_FILE.read_text() if DASHBOARD_FILE.exists()
                        else "<h1>Monico Labs</h1><p>dashboard.html missing</p>")
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if p == "/api/devices":
                return self._json(200, {"devices": BRIDGE.device_list()})
            if p == "/api/pools":
                return self._json(200, {"pools": BRIDGE.pool_list()})
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
                    nxt = (parse_qs(u.query).get("next") or ["/"])[0]
                    if nxt != "/dashboard":   # fixed allow-list, no open redirect
                        nxt = "/"
                    return self._send(303, b"", "text/plain",
                                      {"Location": nxt,
                                       "Set-Cookie": f"monico_key={key}; Path=/; HttpOnly; SameSite=Lax"})
                return self._send(403, b"bad key", "text/plain")
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
            if p == "/api/pools":
                res = BRIDGE.pool_create(body.get("name", ""),
                                         body.get("members") or [])
                return self._json(400 if "error" in res else 200, res)
            if p == "/api/pools/delete":
                res = BRIDGE.pool_delete(body.get("id", ""))
                return self._json(404 if "error" in res else 200, res)
            if p == "/api/pool-exec":
                cmds = body.get("commands")
                if not isinstance(cmds, list):
                    cmds = [body.get("command", "")]
                res = BRIDGE.pool_exec(body.get("pool", ""),
                                       [str(c) for c in cmds],
                                       mode=body.get("mode", "split"),
                                       timeout_s=float(body.get("timeout_s", 60)))
                return self._json(400 if "error" in res else 200, res)
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
