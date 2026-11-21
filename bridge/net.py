"""Opt-in Termux internet access through the paired Linux machine.

Local bridge control and USB inventory need NO internet on either device.
With Linux online, this helper configures standard HTTP(S) proxy variables
for proxy-aware Termux CLI programs. It is NOT Android-wide tethering.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import urllib.parse
import urllib.request

from .common import CLIENT_CONFIG, DATA_DIR, load_json

FLAG_FILE = DATA_DIR / "network-proxy-enabled"
BASH_HOOK = '# monico-net: Linux internet proxy (enabled only while reachable)'
BASH_SOURCE = 'if command -v monico-net >/dev/null 2>&1; then eval "$(monico-net env 2>/dev/null)"; fi'


def _network_url() -> str | None:
    saved = load_json(CLIENT_CONFIG)
    url = str(saved.get("server_url") or "").rstrip("/")
    key = str(saved.get("key") or "")
    if not url or not key:
        return None
    parsed = urllib.parse.urlparse(url)
    # HTTPS tunnel URLs cannot reliably forward CONNECT: only use a
    # direct HTTP bridge on a trusted LAN/ADB loopback.
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if not port or parsed.path or parsed.query or parsed.fragment:
        return None
    try:
        # Never try an existing proxy when checking the bridge itself.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + "/health", timeout=0.6) as response:
            value = json.load(response)
        if not (value.get("ok") and value.get("service") == "monico-bridge"):
            return None
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return url


def _vars() -> dict[str, str]:
    url = _network_url()
    if not url:
        return {}
    parsed = urllib.parse.urlparse(url)
    saved = load_json(CLIENT_CONFIG)
    key = urllib.parse.quote(str(saved.get("key")), safe="")
    userinfo = f"monico:{key}@"
    host = parsed.netloc
    proxy = f"http://{userinfo}{host}"
    bypass = "localhost,127.0.0.1,::1," + str(parsed.hostname)
    return {
        "http_proxy": proxy, "https_proxy": proxy,
        "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
        "no_proxy": bypass, "NO_PROXY": bypass,
    }


def _set_hook(path: Path) -> None:
    try:
        content = path.read_text() if path.exists() else ""
    except OSError as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc
    if BASH_HOOK not in content:
        with path.open("a") as fd:
            fd.write("\n" + BASH_HOOK + "\n" + BASH_SOURCE + "\n")


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "status"
    if command == "env":
        if FLAG_FILE.is_file():
            for key, value in _vars().items():
                print("export " + key + "=" + shlex.quote(value))
        return 0
    if command == "on":
        data = load_json(CLIENT_CONFIG)
        if not data.get("key") or not data.get("server_url"):
            print("Pair with Linux first: monico-connect", file=sys.stderr)
            return 1
        if not _vars():
            print("Linux bridge is unreachable over a direct LAN or USB "
                  "connection. Keep the bridge running; don't use a public "
                  "tunnel for this proxy.", file=sys.stderr)
            return 1
        DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        FLAG_FILE.write_text("enabled\n")
        FLAG_FILE.chmod(0o600)
        home = Path.home()
        for path in (home / ".bashrc", home / ".profile"):
            _set_hook(path)
        print("Linux internet sharing ENABLED for proxy-aware Termux tools "
              "in new terminal sessions.")
        print("Open a new Termux shell, or run: eval \"$(monico-net env)\"")
        print("To turn this off: monico-net off (and open a new shell).")
        return 0
    if command == "off":
        try:
            FLAG_FILE.unlink()
        except FileNotFoundError:
            pass
        print("Linux proxy sharing disabled for NEW Termux shells. "
              "Close/reopen this shell to clear the old proxy variables.")
        return 0
    if command == "status":
        status = "enabled" if FLAG_FILE.is_file() else "disabled"
        link = "reachable" if _network_url() else "not connected / unsupported proxy transport"
        print(f"Termux via Linux internet proxy: {status}; Linux bridge: {link}")
        print("Local Linux commands, USB inventory and pairing never "
              "need an internet connection.")
        print("This is a Termux HTTP(S) proxy, NOT Android system-wide internet.")
        return 0
    if command == "run":
        if len(argv) < 2:
            print("usage: monico-net run <program> [args...]", file=sys.stderr)
            return 2
        values = _vars()
        if not values:
            print("Linux bridge unreachable or not paired.", file=sys.stderr)
            return 1
        env = os.environ.copy()
        env.update(values)
        try:
            return subprocess.call(argv[1:], env=env)
        except OSError as exc:
            print(f"Cannot run command: {exc}", file=sys.stderr)
            return 1
    print("usage: monico-net [status|on|off|env|run <command...>]", file=sys.stderr)
    return 2
