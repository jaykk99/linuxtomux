"""Linux integration tests for coexistence with a service already on 8765.

Proves the bridge can run on an alternate port and that CLI commands
launched in fresh shells discover it without inheriting MONICO_PORT.
This is an Ubuntu/Linux test, NOT a physical Android/Termux device test.
"""
import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bridge import cli

ROOT = Path(__file__).resolve().parents[1]


def test_saved_port_priority_and_validation(monkeypatch, tmp_path):
    monkeypatch.delenv("MONICO_PORT", raising=False)
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    assert cli._local_server_port() == 8765
    path = tmp_path / "server-port.json"
    path.write_text('{"port":8767}')
    assert cli._local_server_port() == 8767
    path.write_text('{"port":true}')
    assert cli._local_server_port() == 8765
    path.write_text('{"port":70000}')
    assert cli._local_server_port() == 8765
    path.write_text('not json')
    assert cli._local_server_port() == 8765
    monkeypatch.setenv("MONICO_PORT", "8788")
    assert cli._local_server_port() == 8788


def test_two_services_on_one_linux_host_and_separate_cli_shells(tmp_path):
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["MONICO_HOME"] = str(tmp_path / ".monico-bridge")
    for var in ("MONICO_PORT", "MONICO_SERVER", "MONICO_TOKEN"):
        env.pop(var, None)
    installer = subprocess.run(["bash", str(ROOT / "install-linux.sh")],
                               cwd=str(ROOT), env=env, capture_output=True,
                               text=True, timeout=30)
    assert installer.returncode == 0, installer.stdout + installer.stderr
    bin_dir = tmp_path / ".local" / "bin"

    # Another service occupies Agent Mail's documented local dashboard port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            occupied.bind(("127.0.0.1", 8765))
        except OSError:
            pytest.skip("Port 8765 is already occupied on this test runner")
        occupied.listen()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as free:
            free.bind(("127.0.0.1", 0))
            bridge_port = free.getsockname()[1]
        proc = subprocess.Popen(
            [str(bin_dir / "monico-server"), "start", "--port", str(bridge_port)],
            cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                assert proc.poll() is None, "Linux bridge process exited"
                try:
                    with socket.create_connection(("127.0.0.1", bridge_port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                pytest.fail("Linux bridge did not open its alternate port")
            config = tmp_path / ".monico-bridge" / "server-port.json"
            assert json.loads(config.read_text()) == {"port": bridge_port}
            assert stat.S_IMODE(config.stat().st_mode) == 0o600

            # Separate CLI processes, with no MONICO_PORT, must target the
            # bridge port rather than the Agent Mail listener on 8765.
            pair = subprocess.run([str(bin_dir / "monico-pair")],
                                  cwd=str(ROOT), env=env, capture_output=True,
                                  text=True, timeout=10)
            assert pair.returncode == 0, pair.stderr
            assert "4-DIGIT CODE" in pair.stdout
            cmd = subprocess.run(
                [str(bin_dir / "monico-cmd"), "linux", "printf cohost-works"],
                cwd=str(ROOT), env=env, capture_output=True,
                text=True, timeout=15)
            assert cmd.returncode == 0, cmd.stderr
            assert cmd.stdout == "cohost-works"
            # Agent Mail's port was never displaced by the bridge.
            with socket.create_connection(("127.0.0.1", 8765), timeout=1):
                pass
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
