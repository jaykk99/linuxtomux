"""Unit tests: the two-step connect flow.

1. `monico-server start` prints one short ready-to-paste Termux command
   with the real LAN IP and a 6-digit pair code, no key, no placeholders.
2. The printed command actually runs: install-termux.sh exchanges the
   code for the pairing key, fetches the app tarball from the Linux box
   itself (no GitHub, no internet on the phone), installs the launchers,
   saves the server + key, and connects by itself.
Run: python3 -m pytest tests/ -q"""
import io
import json
import os
import shlex
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
    block = _termux_connect_block("192.168.1.23", 8765, "123456")
    assert "curl -s http://192.168.1.23:8765/i | bash -s 192.168.1.23:8765 123456" in block
    assert "MONICO_TOKEN" not in block and "MONICO_SERVER" not in block
    assert "install-termux.sh" not in block
    assert "<" not in block and ">" not in block
    assert "linux-host" not in block and "pairing key" not in block


def test_termux_block_empty_without_lan_ip():
    assert _termux_connect_block(None, 8765, "123456") == ""


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
        env["MONICO_HOME"] = str(tmp_path / "phone-data")
        env["PATH"] = str(bindir) + ":" + env["PATH"]

        # The exact one-line command the server prints (LAN IP is
        # loopback here; the shape is what is under test). It ends by
        # connecting, so it runs until the timeout kills it: return
        # code 124 with the "connected" line in the output means the
        # install finished and the phone is on the server. Run from an
        # empty directory so the installer cannot copy from a local
        # checkout and must take the tarball branch.
        code = server_mod._new_pair_code(key)
        block = _termux_connect_block("127.0.0.1", port, code)
        assert "<" not in block
        curl_line = block.splitlines()[1].strip()
        empty_cwd = tmp_path / "empty-cwd"
        empty_cwd.mkdir()
        # timeout wraps the whole pipeline (curl | bash), not just
        # curl: the installer ends by connecting, which runs until
        # the timeout kills it.
        cmd = "timeout 15 bash -c " + shlex.quote(curl_line)
        proc = subprocess.run(cmd, shell=True, env=env,
                              capture_output=True, text=True, timeout=60,
                              cwd=str(empty_cwd))
        assert proc.returncode == 124, proc.stdout + proc.stderr
        assert f"connected to http://127.0.0.1:{port}" in proc.stdout, \
            proc.stdout + proc.stderr
        assert not (bindir / "git.marker").exists(), \
            "installer fell back to a git clone; the offline path is broken"
        app = home / ".monico-bridge" / "app"
        assert (app / "bridge" / "server.py").is_file()
        assert (app / "bridge" / "tunnel.py").is_file()
        assert (app / "ui" / "dashboard.html").is_file()
        launcher = prefix / "bin" / "monico-connect"
        assert launcher.is_file() and os.access(launcher, os.X_OK)
        cfg = json.loads((tmp_path / "phone-data" / "client.json").read_text())
        assert cfg["server_url"] == f"http://127.0.0.1:{port}"
        assert cfg["key"] == key
        mode = stat.S_IMODE((tmp_path / "phone-data" / "client.json").stat().st_mode)
        assert mode == 0o600, oct(mode)
    finally:
        httpd.shutdown()
        server_mod.BRIDGE = prev
