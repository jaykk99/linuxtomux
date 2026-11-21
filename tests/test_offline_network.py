"""Regression checks for offline USB pairing and Linux internet sharing.

An HTTP service on 127.0.0.1 stands in for the port exposed on Android
by an authorized 'adb reverse' command. No external internet, mobile data,
real USB phone or tunnel is used in this test suite.
"""
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from bridge import client, net, server
from bridge.common import get_or_create_key

ROOT = Path(__file__).resolve().parents[1]


def _server():
    previous = server.BRIDGE
    bridge = server.Bridge(name="linux-offline", server_id="srv-offline")
    server.BRIDGE = bridge
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, previous


def test_usb_pairing_ignores_blocked_udp(monkeypatch, tmp_path):
    httpd, previous = _server()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        monkeypatch.setattr(client, "USB_REVERSE_PORTS",
                            (httpd.server_address[1],))
        monkeypatch.setattr(client, "_discover_server",
                            lambda code: pytest.fail("USB fallback used UDP"))
        monkeypatch.setattr(client, "_prompt_server_url",
                            lambda: pytest.fail("USB asked for a LAN address"))
        monkeypatch.setattr(client, "CLIENT_CONFIG", tmp_path / "client.json")
        key, _ = get_or_create_key()
        code = server._new_pair_code(key)
        found, actual_key, sid = client._pair_with_code(code)
        assert found == url
        assert actual_key == key
        assert sid == "srv-offline"
        assert json.loads((tmp_path / "client.json").read_text())["key"] == key
        # A valid one-use code cannot be re-used on the direct route.
        with pytest.raises(SystemExit, match="bad or expired"):
            client._exchange_code(url, code)
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.BRIDGE = previous


def test_network_proxy_env_uses_paired_linux_and_does_not_log_secret(
        monkeypatch, tmp_path, capsys):
    httpd, previous = _server()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        cfg = tmp_path / "client.json"
        secret = "sample-bridge-key"
        cfg.write_text(json.dumps({"server_url": url, "key": secret}))
        monkeypatch.setattr(net, "CLIENT_CONFIG", cfg)
        monkeypatch.setattr(net, "DATA_DIR", tmp_path)
        monkeypatch.setattr(net, "FLAG_FILE", tmp_path / "network-proxy-enabled")
        # Environment should contain the proxy only while explicitly enabled.
        assert net.main(["env"]) == 0
        assert capsys.readouterr().out == ""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(net.Path, "home", lambda: tmp_path)
        assert net.main(["on"]) == 0
        assert secret not in capsys.readouterr().out
        assert (tmp_path / ".bashrc").is_file()
        assert (tmp_path / ".profile").is_file()
        assert net.FLAG_FILE.stat().st_mode & 0o077 == 0
        assert net.main(["on"]) == 0
        lines = (tmp_path / ".bashrc").read_text()
        assert lines.count(net.BASH_HOOK) == 1
        assert net.main(["env"]) == 0
        proxy_exports = capsys.readouterr().out
        assert "http_proxy=" in proxy_exports
        assert secret in proxy_exports  # generated script is private, not logged
        assert "NO_PROXY=" in proxy_exports
        assert net.main(["off"]) == 0
        capsys.readouterr()
        assert net.main(["env"]) == 0
        assert capsys.readouterr().out == ""
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.BRIDGE = previous


def test_network_command_uses_proxy_variables(monkeypatch, tmp_path):
    httpd, previous = _server()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        cfg = tmp_path / "client.json"
        cfg.write_text(json.dumps({"server_url": url, "key": "offline-secret"}))
        monkeypatch.setattr(net, "CLIENT_CONFIG", cfg)
        ret = net.main(["run", sys.executable, "-c",
                        "import os,sys; sys.exit(0 if "
                        "'offline-secret' in os.environ.get('http_proxy','') "
                        "else 1)"])
        assert ret == 0
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.BRIDGE = previous


def test_offline_termux_install_fetches_linux_archive(monkeypatch, tmp_path):
    """The phone installer runs from Linux tarball over local TCP only.

    Simulates a clean Termux HOME/PREFIX, and makes the GitHub command fail
    if invoked so no internet dependency can slip into this path.
    """
    httpd, previous = _server()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        home = tmp_path / "android-home"
        home.mkdir()
        prefix = tmp_path / "usr"
        env = dict(os.environ)
        env.update({"HOME": str(home), "PREFIX": str(prefix),
                    "MONICO_HOME": str(home / ".monico-bridge")})
        for k in ("MONICO_SERVER", "MONICO_TOKEN", "http_proxy",
                  "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            env.pop(k, None)
        fakebin = tmp_path / "no-git"
        fakebin.mkdir()
        # The installer should never invoke git when Linux serves the app.
        fake_git = fakebin / "git"
        fake_git.write_text("#!/bin/sh\necho 'network forbidden' >&2; exit 97\n")
        fake_git.chmod(0o755)
        env["PATH"] = str(fakebin) + os.pathsep + env.get("PATH", "")
        key, _ = get_or_create_key()
        code = server._new_pair_code(key)
        proc = subprocess.run(
            ["timeout", "9", "bash", str(ROOT / "install-termux.sh"),
             url, code], env=env, cwd=str(ROOT), capture_output=True,
            text=True, timeout=15)
        assert proc.returncode == 124, proc.stdout + proc.stderr
        assert "connected to " + url in proc.stdout, proc.stdout + proc.stderr
        assert (prefix / "bin" / "monico-net").is_file()
        assert (prefix / "bin" / "monico-usb").is_file()
        assert (home / ".monico-bridge" / "client.json").is_file()
        assert "network forbidden" not in proc.stderr
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.BRIDGE = previous
