"""Command line entry points.

Linux side:   monico-server start [--port N] [--join URL] [--key KEY]
                                  [--name NAME] | token-add <token> | key
Termux side:  monico-connect [--server URL] [--key KEY] [--name NAME]
Both sides:   monico-cmd <linux|phone|device-name|device-id> <command...>
              monico-model install [--device this|both] [--dry-run] [--profile NAME]
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

from .common import (CLIENT_CONFIG, DEFAULT_PORT, KEY_FILE, accepted_tokens,
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
        return f"http://127.0.0.1:{DEFAULT_PORT}", key
    if saved.get("server_url"):
        return saved["server_url"], saved.get("key", "")
    key, _ = get_or_create_key()
    return f"http://127.0.0.1:{DEFAULT_PORT}", key


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


def cmd_main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: monico-cmd <linux|phone|device-name|device-id> <command...>")
        return 2
    target_name, command = argv[0], " ".join(argv[1:])
    base, key = _bridge_endpoint()
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
        port = int(opt("--port") or DEFAULT_PORT)
        srv.serve(port, join_url=opt("--join"), join_key=opt("--key"),
                  name=opt("--name"))
        return 0
    if argv[0] == "key":
        key, is_new = get_or_create_key()
        print(key if is_new else f"(saved) {key}")
        return 0
    if argv[0] == "token-add" and len(argv) == 2:
        srv.token_add(argv[1])
        return 0
    print("usage: monico-server start [--port N] [--join URL] [--key KEY] "
          "[--name NAME] | key | token-add <token>")
    return 2


def connect_main(argv: list[str]) -> int:
    from .client import connect
    opts = {"server_url": "", "key": "", "name": ""}
    for i, a in enumerate(argv):
        if a == "--server":
            opts["server_url"] = argv[i + 1]
        elif a == "--key":
            opts["key"] = argv[i + 1]
        elif a == "--name":
            opts["name"] = argv[i + 1]
    connect(**opts)
    return 0
