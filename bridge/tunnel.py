"""Cloudflare Tunnel (quick tunnel) for the bridge server.

`monico-server start --public` exposes the dashboard to the internet
through a free cloudflared quick tunnel: no Cloudflare account, no
card, no config. The tunnel binary is an external tool, not a code
dependency; when it is missing the server says so plainly and prints
the one install command instead of failing silently.

A durable URL on Jay's own Cloudflare account (named tunnel) is a
documented follow-up, not built here: it needs `cloudflared tunnel
login` + a tunnel + DNS record, which only Jay can do on his account.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading

TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

CLOUDFLARED_INSTALL = (
    "sudo curl -fsSL "
    "https://github.com/cloudflare/cloudflared/releases/latest/download/"
    "cloudflared-linux-amd64 -o /usr/local/bin/cloudflared "
    "&& sudo chmod +x /usr/local/bin/cloudflared"
)
CLOUDFLARED_INSTALL_NOTE = (
    "cloudflared is not installed. Install it with:\n"
    f"  {CLOUDFLARED_INSTALL}\n"
    "then run `monico-server start --public` again. "
    "(No account or card needed for a quick tunnel.)"
)


def find_cloudflared() -> str | None:
    return shutil.which("cloudflared")


def start_quick_tunnel(port: int, on_url, log=print) -> subprocess.Popen | None:
    """Launch `cloudflared tunnel --url http://127.0.0.1:<port>` and
    call on_url(public_url) once cloudflared prints the
    *.trycloudflare.com address (it logs to stderr). Returns the
    process, or None when cloudflared is missing (caller prints the
    install note). The watcher thread is a daemon: it only reads
    stderr, the server owns the process lifetime."""
    binary = find_cloudflared()
    if not binary:
        return None
    proc = subprocess.Popen(
        [binary, "tunnel", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    found = threading.Event()

    def _watch() -> None:
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                m = TUNNEL_URL_RE.search(line)
                if m and not found.is_set():
                    found.set()
                    on_url(m.group(0))
        except Exception as exc:  # tunnel died; server keeps serving locally
            log(f"cloudflared tunnel ended ({exc})")

    threading.Thread(target=_watch, daemon=True).start()
    return proc


def stop_tunnel(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
