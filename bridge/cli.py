"""Command line entry points.

Linux side:   monico-server start [--port N] [--join URL] [--key KEY]
                                  [--name NAME] [--public] | token-add <token> | key
              monico-pair        print a fresh 4-digit code for a running server
Termux side:  monico-connect [CODE] [--server URL] [--key KEY] [--name NAME]
Both sides:   monico-cmd <linux|phone|device-name|device-id> <command...>
              monico-model install [--device this|both] [--dry-run] [--profile NAME]
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from .common import (CLIENT_CONFIG, DATA_DIR, DEFAULT_PORT, KEY_FILE,
                     get_or_create_key, load_json)
from .hwdetect import PROFILES, detect
from . import model_installer


def _post(url: str, key: str, payload: dict, timeout: int = 90) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 method="POST",
                                 headers={"X-Pair-Key": key,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def _local_server_port() -> int:
    """Resolve the Linux server port across separate terminal sessions.

    An explicit MONICO_PORT wins. Otherwise use the last successfully
    bound server port so monico-pair and monico-cmd still work after
    monico-server start --port 8767 (e.g. alongside Agent Mail on 8765).
    """
    env = os.environ.get("MONICO_PORT", "").strip()
    if env:
        return int(env)
    saved = load_json(DATA_DIR / "server-port.json").get("port")
    if type(saved) is int and 1 <= saved <= 65535:
        return saved
    return DEFAULT_PORT


def _bridge_endpoint() -> tuple[str, str]:
    """Where monico-cmd should talk: MONICO_SERVER env if set (aim at
    any mesh server), else my own server if I am the Linux side (key
    file present), else the server saved by monico-connect."""
    env_url = os.environ.get("MONICO_SERVER", "").strip()
    if env_url:
        saved = load_json(CLIENT_CONFIG)
        key = os.environ.get("MONICO_TOKEN", "").strip() or saved.get("key", "")
        if not key and KEY_FILE.exists():
            key, _ = get_or_create_key()
        return env_url, key
    saved = load_json(CLIENT_CONFIG)
    if KEY_FILE.exists() and not saved.get("server_url"):
        key, _ = get_or_create_key()
        return f"http://127.0.0.1:{_local_server_port()}", key
    if saved.get("server_url"):
        return saved["server_url"], saved.get("key", "")
    key, _ = get_or_create_key()
    return f"http://127.0.0.1:{_local_server_port()}", key


def resolve_from_list(devices: list[dict], name: str) -> str:
    """Resolve a target name against a device list (local + mesh).

    "linux"/"server"/"this" mean the server monico-cmd is talking to.
    Otherwise an exact device_id wins, then a unique device name (if a
    name is shared, the one online device wins; still tied is an error
    that lists the ids). "phone" is a shortcut for a client device:
    Android devices first, then any device that joined as a client;
    it never matches a mesh server. With several phones, target by
    name or id."""
    if name in ("linux", "server", "this"):
        return "linux"
    for dev in devices:
        if dev.get("device_id") == name:
            return dev["device_id"]
    named = [d for d in devices if d.get("name") == name]
    if len(named) == 1:
        return named[0]["device_id"]
    if len(named) > 1:
        online = [d for d in named if d.get("online")]
        if len(online) == 1:
            return online[0]["device_id"]
        ids = ", ".join(d["device_id"] for d in named)
        raise SystemExit(f"{name!r} matches {len(named)} devices ({ids}); "
                         f"target one by device id instead")
    if name == "phone":
        clients = [d for d in devices if d.get("kind") != "server"]
        android = [d for d in clients
                   if str(d.get("platform", "")).startswith("android")]
        pool = android or clients
        online = [d for d in pool if d.get("online")]
        pick = online or pool
        if pick:
            return pick[0]["device_id"]
    raise SystemExit(f"no connected device matches {name!r}; "
                     f"online: {[d['name'] for d in devices]}")


def _resolve_target(name: str) -> str:
    if name in ("linux", "server", "this"):
        return "linux"
    base, key = _bridge_endpoint()
    req = urllib.request.Request(base + "/api/devices",
                                 headers={"X-Pair-Key": key})
    with urllib.request.urlopen(req, timeout=15) as resp:
        devices = json.loads(resp.read())["devices"]
    return resolve_from_list(devices, name)


def _find_pool(base: str, key: str, name: str) -> dict | None:
    """A pool (devices paired on the dashboard) can be targeted by
    name exactly like a device. Old servers have no /api/pools: that
    just means no pools."""
    if name in ("linux", "server", "this", "phone"):
        return None
    try:
        req = urllib.request.Request(base + "/api/pools",
                                     headers={"X-Pair-Key": key})
        with urllib.request.urlopen(req, timeout=15) as resp:
            pools = json.loads(resp.read()).get("pools", [])
    except Exception:
        return None
    for p in pools:
        if name in (p.get("name"), p.get("id")):
            return p
    return None


def _pool_cmd(base: str, key: str, pool: dict, command: str) -> int:
    """monico-cmd <pool> [--all] <command>: the pool picks which
    member runs it (or every member with --all)."""
    mode = "split"
    if command.startswith("--all "):
        mode, command = "all", command[len("--all "):]
    try:
        res = _post(base + "/api/pool-exec", key,
                    {"pool": pool["id"], "command": command, "mode": mode,
                     "timeout_s": 60}, timeout=130)
    except urllib.error.HTTPError as exc:
        res = json.loads(exc.read() or b"{}")
    if "error" in res:
        print(f"[bridge] {res['error']}", file=sys.stderr)
        return 1
    worst = 0
    for r in res.get("results", []):
        print(f"[{pool['name']} -> {r.get('device')}]", file=sys.stderr)
        if r.get("stdout"):
            sys.stdout.write(r["stdout"])
        if r.get("stderr"):
            sys.stderr.write(r["stderr"])
        if "error" in r:
            print(f"[bridge] {r['error']}", file=sys.stderr)
            worst = worst or 1
        worst = worst or int(r.get("exit_code", 0) or 0)
    return worst


def cmd_main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: monico-cmd <linux|phone|device-name|device-id|pool-name> <command...>")
        return 2
    target_name, command = argv[0], " ".join(argv[1:])
    base, key = _bridge_endpoint()
    pool = _find_pool(base, key, target_name)
    if pool:
        return _pool_cmd(base, key, pool, command)
    target = _resolve_target(target_name)
    res = _post(base + "/api/exec", key,
                {"target": target, "command": command, "timeout_s": 60})
    if res.get("stdout"):
        sys.stdout.write(res["stdout"])
    if res.get("stderr"):
        sys.stderr.write(res["stderr"])
    if "error" in res:
        print(f"[bridge] {res['error']}", file=sys.stderr)
        return 1
    return int(res.get("exit_code", 0))



def usb_main(argv: list[str]) -> int:
    """List USB drives attached to Linux from Linux OR paired Termux.

    This reads the authenticated bridge device registry rather than
    assuming the Android OS can directly mount a drive plugged into
    the Linux computer. It never mounts, formats, or writes USB files.
    """
    if argv and argv not in (["list"], ["--json"], ["list", "--json"]):
        print("usage: monico-usb [list] [--json]", file=sys.stderr)
        return 2
    base, key = _bridge_endpoint()
    try:
        req = urllib.request.Request(base.rstrip("/") + "/api/devices",
                                     headers={"X-Pair-Key": key})
        with urllib.request.urlopen(req, timeout=10) as resp:
            devices = json.loads(resp.read()).get("devices", [])
    except (OSError, urllib.error.URLError, ValueError, KeyError) as exc:
        print("Cannot read Linux USB drives through the bridge. "
              "Start monico-server on Linux and pair Termux first. "
              f"Details: {exc}", file=sys.stderr)
        return 1
    servers = [d for d in devices if d.get("kind") == "server"]
    result = []
    for d in servers:
        specs = d.get("specs") or {}
        hw = d.get("hw") or {}
        drives = specs.get("storage") or hw.get("storage") or []
        if not isinstance(drives, list):
            drives = []
        result.append({
            "linux": d.get("name") or d.get("device_id") or "linux",
            "online": bool(d.get("online")),
            "usb": [x for x in drives if isinstance(x, dict)
                    and x.get("kind") == "usb"],
        })
    if "--json" in argv:
        print(json.dumps({"servers": result}, indent=2))
        return 0
    if not servers:
        print("No Linux server connected. Pair your phone with monico-connect.")
        return 1
    found = 0
    for server in result:
        title = server["linux"] + ("" if server["online"] else " [offline]")
        print(title + ":")
        for drive in server["usb"]:
            found += 1
            label = drive.get("label") or "USB storage"
            path = drive.get("path") or "(not mounted)"
            status = "mounted" if drive.get("mounted") else "not mounted"
            free = drive.get("free_gb")
            capacity = f", {free} GB free" if isinstance(free, (int, float)) else ""
            print(f"  {label}: {path} [{status}{capacity}]")
        if not server["usb"]:
            print("  No USB drive detected by this Linux server.")
    if not found:
        print("Plug a drive into Linux and wait up to 30 seconds; "
              "unmounted drives may appear but cannot store files yet.")
    return 0


def model_main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("install", "status"):
        print("usage: monico-model install [--dry-run] [--profile NAME] "
              "[--device this|both] [--small]")
        print("       monico-model status")
        return 2
    if argv[0] == "status":
        from .common import model_status
        print(json.dumps(model_status(), indent=2))
        return 0
    dry = "--dry-run" in argv
    small = "--small" in argv
    device = "this"
    profile = None
    for i, a in enumerate(argv):
        if a == "--device" and i + 1 < len(argv):
            device = argv[i + 1]
        if a == "--profile" and i + 1 < len(argv):
            profile = argv[i + 1]
    if profile and profile not in PROFILES:
        print(f"unknown profile: {profile} "
              f"(available: {', '.join(sorted(PROFILES))})", file=sys.stderr)
        return 2
    hw = PROFILES[profile] if profile else detect()
    choice = model_installer.pick(hw)
    if small:  # smoke-test override: smallest catalogue entry
        name, repo, params, quants, lineage = model_installer.CATALOG[0]
        fname, size = quants["Q4_K_M"]
        choice = {"model": name, "repo": repo, "quant": "Q4_K_M", "file": fname,
                  "size_gb": size, "params_b": params, "lineage": lineage,
                  "usable_ram_gb": choice["usable_ram_gb"],
                  "budget_gb": choice["budget_gb"],
                  "url": f"https://huggingface.co/{repo}/resolve/main/{fname}",
                  "hardware": hw, "override": "--small smoke-test pick",
                  "models_dir": choice["models_dir"],
                  "storage": choice["storage"]}
    print(json.dumps({"device_profile": hw, "pick": choice}, indent=2))
    if device == "both":
        print("[both] The other device gets its own pick from its own "
              "hardware when you run `monico-model install` there, or from "
              "the homescreen's model tile while the bridge is connected. "
              "This run installs on THIS device only.", file=sys.stderr)
    if dry:
        print("[dry-run] nothing downloaded, nothing installed.", file=sys.stderr)
        return 0
    res = model_installer.install(hw, choice)
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 1


def server_main(argv: list[str]) -> int:
    from . import server as srv
    if not argv or argv[0] == "start":
        def opt(flag: str) -> str | None:
            return argv[argv.index(flag) + 1] if flag in argv else None
        port = int(opt("--port") or _local_server_port())
        srv.serve(port, join_url=opt("--join"), join_key=opt("--key"),
                  name=opt("--name"), public=("--public" in argv))
        return 0
    if argv[0] == "key":
        key, is_new = get_or_create_key()
        print(key if is_new else f"(saved) {key}")
        return 0
    if argv[0] == "token-add" and len(argv) == 2:
        srv.token_add(argv[1])
        return 0
    print("usage: monico-server start [--port N] [--join URL] [--key KEY] "
          "[--name NAME] [--public] | key | token-add <token>")
    return 2


def pair_main(argv: list[str]) -> int:
    """Print a fresh 4-digit code for the already-running server,
    without restarting it. Talks to POST /pair-new on loopback with
    the pairing key; if the server is not running, say so plainly."""
    def opt(flag: str) -> str | None:
        return argv[argv.index(flag) + 1] if flag in argv else None
    port = int(opt("--port") or _local_server_port())
    key, _ = get_or_create_key()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/pair-new", data=b"{}", method="POST",
            headers={"X-Pair-Key": key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            code = json.loads(resp.read() or b"{}").get("code") or ""
    except Exception:
        print("ERROR: the server is not running on this box. "
              "Start it first with: monico-server start", file=sys.stderr)
        return 1
    if not code:
        print("ERROR: the server did not return a code.", file=sys.stderr)
        return 1
    print()
    print("  ===============================")
    print(f"   YOUR 4-DIGIT CODE:  {code}")
    print("  ===============================")
    print("  (valid 10 minutes, one use)")
    print()
    print("  On the phone (Termux): monico-connect, then type this code.")
    return 0


def connect_main(argv: list[str]) -> int:
    from .client import connect
    opts = {"server_url": "", "key": "", "name": "", "code": ""}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--server", "--key", "--name") and i + 1 < len(argv):
            opts[{"--server": "server_url", "--key": "key",
                  "--name": "name"}[a]] = argv[i + 1]
            i += 2
        elif a.startswith("-"):
            print(f"unknown option: {a}", file=sys.stderr)
            return 2
        else:
            opts["code"] = a  # bare 4-digit code from the Linux screen
            i += 1
    # connect() reports 1 when the pairing is dead and could not be
    # repaired, so a script (or the installer's smoke check) can tell.
    return connect(**opts) or 0
