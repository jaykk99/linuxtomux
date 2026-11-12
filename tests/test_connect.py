"""Unit tests: the two-step connect flow.

1. `monico-server start` prints a ready-to-paste Termux block with the
   real LAN IP and the real pairing key, no placeholders.
2. The printed curl line actually runs: install-termux.sh fetches the
   app tarball from the Linux box itself (no GitHub, no internet on
   the phone), installs the launchers, and saves the server + key.
Run: python3 -m pytest tests/ -q"""
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONICO_HOME", "/tmp/monico-bridge-test-home")
import bridge.server as server_mod  # noqa: E402
from bridge.common import get_or_create_key  # noqa: E402
from bridge.server import (  # noqa: E402
    Bridge,
    _repo_tarball_bytes,
    _termux_connect_block,
    make_handler,
)

REPO = Path(__file__).resolve().parent.parent


def test_termux_block_has_real_values_no_placeholders():
    block = _termux_connect_block("192.168.1.23", 8765, "abc123-real-key")
    assert "192.168.1.23:8765" in block
    assert "MONICO_SERVER=http://192.168.1.23:8765" in block
    assert "MONICO_TOKEN=abc123-real-key" in block
    assert "curl -fsSL http://192.168.1.23:8765/install-termux.sh" in block
    assert "monico-connect" in block
    assert "<" not in block and ">" not in block
    assert "linux-host" not in block and "pairing key" not in block


def test_termux_block_empty_without_lan_ip():
    assert _termux_connect_block(None, 8765, "abc123-real-key") == ""


def test_repo_tarball_has_everything_installer_needs():
    data = _repo_tarball_bytes()
    names = tarfile.open(fileobj=io.BytesIO(data)).getnames()
    for want in ("bridge/server.py", "bridge/client.py", "bridge/tunnel.py",
                 "bridge/specs.py", "ui/dashboard.html", "ui/homescreen.html",
                 "monico-connect", "monico-cmd", "monico-model",
                 "install-termux.sh"):
        assert want in names, want


def _serve(bridge):
    prev = server_mod.BRIDGE
    server_mod.BRIDGE = bridge
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, prev


def test_printed_curl_line_installs_offline(tmp_path):
    """Run the exact command shape the server prints, against a live
    local server, with `git` stubbed to fail loudly: if the installer
    ever reaches for GitHub, the test fails. The phone side needs no
    internet, only the LAN link to the box."""
    key, _ = get_or_create_key()
    bridge = Bridge(name="linux-a", server_id="srv-aaaa")
    httpd, prev = _serve(bridge)
    try:
        port = httpd.server_address[1]
        # The installer script itself is public on the box.
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/install-termux.sh",
                timeout=10) as resp:
            assert resp.status == 200
            assert b"linuxtomux.tar.gz" in resp.read()
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/linuxtomux.tar.gz",
                timeout=10) as resp:
            assert resp.status == 200
            assert resp.headers.get_content_type() == "application/gzip"

        home = tmp_path / "phone-home"
        prefix = tmp_path / "phone-usr"
        bindir = tmp_path / "stubbin"
        bindir.mkdir()
        # A git that must never run: touch a marker and die.
        (bindir / "git").write_text(
            "#!/bin/sh\ntouch \"$0.marker\"\necho 'git must not run in the offline install' >&2\nexit 1\n")
        (bindir / "git").chmod(0o755)
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["PREFIX"] = str(prefix)
        env["PATH"] = str(bindir) + ":" + env["PATH"]

        # The exact two lines the server prints (LAN IP is loopback
        # here; the shape is what is under test). Run from an empty
        # directory so the installer cannot copy from a local checkout
        # and must take the tarball branch.
        block = _termux_connect_block("127.0.0.1", port, key)
        assert "<" not in block
        curl_line, connect_line = [ln.strip() for ln in block.splitlines()[1:]]
        assert connect_line == "monico-connect"
        empty_cwd = tmp_path / "empty-cwd"
        empty_cwd.mkdir()
        proc = subprocess.run(curl_line, shell=True, env=env,
                              capture_output=True, text=True, timeout=120,
                              cwd=str(empty_cwd))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not (bindir / "git.marker").exists(), \
            "installer fell back to a git clone; the offline path is broken"
        app = home / ".monico-bridge" / "app"
        assert (app / "bridge" / "server.py").is_file()
        assert (app / "bridge" / "tunnel.py").is_file()
        assert (app / "ui" / "dashboard.html").is_file()
        launcher = prefix / "bin" / "monico-connect"
        assert launcher.is_file() and os.access(launcher, os.X_OK)
        cfg = json.loads((home / ".monico-bridge" / "client.json").read_text())
        assert cfg["server_url"] == f"http://127.0.0.1:{port}"
        assert cfg["key"] == key
        mode = stat.S_IMODE((home / ".monico-bridge" / "client.json").stat().st_mode)
        assert mode == 0o600, oct(mode)
    finally:
        httpd.shutdown()
        server_mod.BRIDGE = prev
