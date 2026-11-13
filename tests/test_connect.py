"""Unit tests: the 4-digit-code connect flow.

1. `monico-server start` prints a 4-digit code, big and clear, plus
   the phone install commands. No IP, no key, no pasted command.
2. The phone installs from a repo checkout (no server needed), then
   `monico-connect` prompts for the code (or takes it as an argument),
   finds the server on the local network by UDP broadcast, exchanges
   the code for the pairing key at GET /pair, saves server + key, and
   connects by itself.
3. `monico-pair` mints a fresh code for a running server.
Run: python3 -m pytest tests/ -q"""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONICO_HOME", "/tmp/monico-bridge-test-home")
import bridge.server as server_mod  # noqa: E402
import bridge.client as client_mod  # noqa: E402
from bridge.cli import pair_main  # noqa: E402
from bridge.common import get_or_create_key  # noqa: E402
from bridge.server import (  # noqa: E402
    Bridge,
    _pair_code_block,
    _repo_tarball_bytes,
    _start_discovery_listener,
    make_handler,
)

REPO = Path(__file__).resolve().parent.parent


def test_pair_code_is_four_digits_and_single_use():
    key, _ = get_or_create_key()
    code = server_mod._new_pair_code(key)
    assert len(code) == 4 and code.isdigit(), code
    assert server_mod._check_pair_code(code) is True  # check does not spend it
    assert server_mod._check_pair_code(code) is True
    assert server_mod._redeem_pair_code(code) == key
    assert server_mod._redeem_pair_code(code) is None  # spent
    assert server_mod._check_pair_code(code) is False


def test_pair_code_wrong_guesses_lock_out():
    key, _ = get_or_create_key()
    code = server_mod._new_pair_code(key)
    wrong = "9999" if code != "9999" else "8888"
    for _ in range(5):
        assert server_mod._redeem_pair_code(wrong) is None
    assert server_mod._check_pair_code(code) is False  # locked out


def test_pair_code_expired_is_dead():
    key, _ = get_or_create_key()
    code = server_mod._new_pair_code(key)
    server_mod.PAIR_STATE["expires"] = time.time() - 1
    assert server_mod._redeem_pair_code(code) is None
    assert server_mod._check_pair_code(code) is False


def test_pair_code_block_has_code_and_install_no_ip():
    block = _pair_code_block(8765, "0427")
    assert "0427" in block
    assert "git clone https://github.com/jaykk99/linuxtomux.git" in block
    assert "./install-termux.sh" in block
    assert "monico-connect" in block
    assert "curl" not in block and "MONICO_TOKEN" not in block
    assert "MONICO_SERVER" not in block
    assert "192.168" not in block and "pairing key" not in block


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


def _http_json(method, url, payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read() or b"{}")


def test_pair_new_endpoint_mints_redeemable_code():
    key, _ = get_or_create_key()
    bridge = Bridge(name="linux-a", server_id="srv-aaaa")
    httpd, prev = _serve(bridge)
    try:
        port = httpd.server_address[1]
        # No key -> 401.
        with urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{port}/pair-new", data=b"{}",
                    method="POST"), timeout=10) as resp:
            assert False, "pair-new without a key must not succeed"
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
    try:
        status, body = _http_json(
            "POST", f"http://127.0.0.1:{port}/pair-new", {},
            {"X-Pair-Key": key, "Content-Type": "application/json"})
        assert status == 200
        code = body["code"]
        assert len(code) == 4 and code.isdigit(), code
        # The fresh code redeems for the pairing key.
        status, body = _http_json(
            "GET", f"http://127.0.0.1:{port}/pair?code={code}")
        assert status == 200 and body["key"] == key
    finally:
        httpd.shutdown()
        server_mod.BRIDGE = prev


def test_pair_main_prints_fresh_code_and_fails_without_server(capsys):
    key, _ = get_or_create_key()
    bridge = Bridge(name="linux-a", server_id="srv-aaaa")
    httpd, prev = _serve(bridge)
    try:
        port = httpd.server_address[1]
        assert pair_main(["--port", str(port)]) == 0
        out = capsys.readouterr().out
        code = [w for w in out.split() if len(w) == 4 and w.isdigit()]
        assert code, out
        status, body = _http_json(
            "GET", f"http://127.0.0.1:{port}/pair?code={code[-1]}")
        assert status == 200 and body["key"] == key
    finally:
        httpd.shutdown()
        server_mod.BRIDGE = prev
    # Server down: loud error, exit 1.
    assert pair_main(["--port", "1"]) == 1
    assert "not running" in capsys.readouterr().err


def test_discovery_handler_replies_only_for_valid_code():
    """The discovery packet logic, proven without a socket: this VM's
    sandbox blocks UDP sends entirely (EPERM on sendto, even to
    127.0.0.1), so no socket test can run here. The handler is pure
    packet logic: a live code gets a reply, a wrong code, a wrong
    type, and garbage all get silence. The real broadcast still
    needs a real phone run to prove on a LAN."""
    key, _ = get_or_create_key()
    bridge = Bridge(name="linux-a", server_id="srv-aaaa")
    prev = server_mod.BRIDGE
    server_mod.BRIDGE = bridge
    try:
        port = 18765
        code = server_mod._new_pair_code(key)
        reply = server_mod._handle_discovery_packet(
            client_mod._discovery_payload(code), ("127.0.0.1", 54321), port)
        assert reply is not None
        msg = json.loads(reply.decode())
        assert msg["type"] == "monico-here"
        assert msg["server"] == f"http://127.0.0.1:{port}"
        assert msg["name"] == "linux-a"
        assert msg["id"] == "srv-aaaa"
        # Checking does not spend the code: it still redeems after.
        assert server_mod._check_pair_code(code) is True
        wrong = "9999" if code != "9999" else "8888"
        assert server_mod._handle_discovery_packet(
            client_mod._discovery_payload(wrong),
            ("127.0.0.1", 54321), port) is None
        assert server_mod._handle_discovery_packet(
            json.dumps({"type": "other", "code": code}).encode(),
            ("127.0.0.1", 54321), port) is None
        assert server_mod._handle_discovery_packet(
            b"not json at all", ("127.0.0.1", 54321), port) is None
    finally:
        server_mod.BRIDGE = prev


def test_discovery_wire_format_round_trips():
    """Client and server agree on the packet shape: the client's
    payload parses as a monico-discover, and a monico-here reply
    parses back to its server URL; anything else parses to None."""
    payload = client_mod._discovery_payload("0427")
    assert json.loads(payload.decode()) == {"type": "monico-discover",
                                            "code": "0427"}
    good = json.dumps({"type": "monico-here",
                       "server": "http://192.168.1.23:8765",
                       "name": "linux-a", "id": "srv-aaaa"}).encode()
    assert client_mod._parse_discovery_reply(good) == "http://192.168.1.23:8765"
    assert client_mod._parse_discovery_reply(
        json.dumps({"type": "monico-discover", "code": "0427"}).encode()) is None
    assert client_mod._parse_discovery_reply(
        json.dumps({"type": "monico-here"}).encode()) is None
    assert client_mod._parse_discovery_reply(b"garbage") is None


def test_prompt_pair_code_reads_stdin_and_rejects_short(monkeypatch):
    real_open = open

    def fake_open(path, *a, **k):
        if str(path) == "/dev/tty":
            raise OSError("no tty in test")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr(client_mod.sys, "stdin", io.StringIO("0427\n"))
    assert client_mod._prompt_pair_code() == "0427"
    monkeypatch.setattr(client_mod.sys, "stdin", io.StringIO("42\n"))
    try:
        client_mod._prompt_pair_code()
        assert False, "a short code must fail"
    except SystemExit as exc:
        assert "4-digit" in str(exc)


def _sandbox_env(tmp_path, home, prefix):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PREFIX"] = str(prefix)
    env["MONICO_HOME"] = str(tmp_path / "phone-data")
    return env


def _copy_repo_checkout(tmp_path):
    """A throwaway copy of this repo (the phone's git clone)."""
    dest = tmp_path / "linuxtomux"
    shutil.copytree(REPO / "bridge", dest / "bridge")
    shutil.copytree(REPO / "ui", dest / "ui")
    for name in ("monico-connect", "monico-cmd", "monico-model",
                 "monico-pair", "install-termux.sh", "install-linux.sh"):
        shutil.copy2(REPO / name, dest / name)
    return dest


def test_checkout_install_then_connect_with_code(tmp_path):
    """The whole new flow, end to end, except the UDP hop: install
    from a repo checkout (git stubbed to fail loudly, so any clone
    attempt fails the test), then `monico-connect --server <url>
    <code>` exchanges the code, saves server + key, and connects.
    Return code 124 from the timeout wrapper with the 'connected'
    line means the phone is on the server. The broadcast hop itself
    cannot run in this sandbox (UDP sends are blocked with EPERM,
    even to loopback), so it is proven by the handler + wire-format
    tests above, and the --server flag is the same code path the
    phone takes once discovery answers. Real-LAN broadcast still
    needs a real phone run."""
    key, _ = get_or_create_key()
    bridge = Bridge(name="linux-a", server_id="srv-aaaa")
    httpd, prev = _serve(bridge)
    try:
        port = httpd.server_address[1]

        home = tmp_path / "phone-home"
        prefix = tmp_path / "phone-usr"
        bindir = tmp_path / "stubbin"
        bindir.mkdir()
        (bindir / "git").write_text(
            "#!/bin/sh\ntouch \"$0.marker\"\necho 'git must not run in the offline install' >&2\nexit 1\n")
        (bindir / "git").chmod(0o755)
        env = _sandbox_env(tmp_path, home, prefix)
        env["PATH"] = str(bindir) + ":" + env["PATH"]

        repo = _copy_repo_checkout(tmp_path)
        proc = subprocess.run(["bash", "./install-termux.sh"], env=env,
                              capture_output=True, text=True, timeout=60,
                              cwd=str(repo))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "run monico-connect" in proc.stdout, proc.stdout + proc.stderr
        assert not (bindir / "git.marker").exists(), \
            "installer fell back to a git clone; the checkout path is broken"
        launcher = prefix / "bin" / "monico-connect"
        assert launcher.is_file() and os.access(launcher, os.X_OK)

        code = server_mod._new_pair_code(key)
        proc = subprocess.run(
            ["timeout", "15", str(launcher), "--server",
             f"http://127.0.0.1:{port}", code], env=env,
            capture_output=True, text=True, timeout=60,
            cwd=str(tmp_path))
        assert proc.returncode == 124, proc.stdout + proc.stderr
        assert f"connected to http://127.0.0.1:{port}" in proc.stdout, \
            proc.stdout + proc.stderr
        cfg = json.loads((tmp_path / "phone-data" / "client.json").read_text())
        assert cfg["server_url"] == f"http://127.0.0.1:{port}"
        assert cfg["key"] == key
        mode = stat.S_IMODE((tmp_path / "phone-data" / "client.json").stat().st_mode)
        assert mode == 0o600, oct(mode)
        assert (tmp_path / "phone-data" / "client.json").exists()
    finally:
        httpd.shutdown()
        server_mod.BRIDGE = prev


def test_connect_wrong_code_fails_loud(tmp_path):
    """A wrong code finds no server (discovery stays silent) and the
    command exits 1 with a clear error, never silently."""
    key, _ = get_or_create_key()
    bridge = Bridge(name="linux-a", server_id="srv-aaaa")
    httpd, prev = _serve(bridge)
    listener = None
    try:
        port = httpd.server_address[1]
        listener = _start_discovery_listener(port)
        assert listener is not None
        home = tmp_path / "phone-home"
        prefix = tmp_path / "phone-usr"
        repo = _copy_repo_checkout(tmp_path)
        env = _sandbox_env(tmp_path, home, prefix)
        proc = subprocess.run(["bash", "./install-termux.sh"], env=env,
                              capture_output=True, text=True, timeout=60,
                              cwd=str(repo))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        code = server_mod._new_pair_code(key)
        wrong = "9999" if code != "9999" else "8888"
        proc = subprocess.run(
            [str(prefix / "bin" / "monico-connect"), wrong], env=env,
            capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "No server found" in proc.stderr, proc.stdout + proc.stderr
    finally:
        if listener is not None:
            listener.close()
        httpd.shutdown()
        server_mod.BRIDGE = prev


def test_connect_no_server_fails_loud(tmp_path):
    """No server at all: same loud exit 1, in about 5 seconds (2
    broadcast rounds), not a hang."""
    home = tmp_path / "phone-home"
    prefix = tmp_path / "phone-usr"
    repo = _copy_repo_checkout(tmp_path)
    env = _sandbox_env(tmp_path, home, prefix)
    proc = subprocess.run(["bash", "./install-termux.sh"], env=env,
                          capture_output=True, text=True, timeout=60,
                          cwd=str(repo))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    start = time.time()
    proc = subprocess.run(
        [str(prefix / "bin" / "monico-connect"), "1234"], env=env,
        capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "No server found" in proc.stderr, proc.stdout + proc.stderr
    assert time.time() - start < 20


def test_connect_saved_config_skips_prompt(tmp_path):
    """A phone that already paired connects directly: no code, no
    prompt, no discovery."""
    key, _ = get_or_create_key()
    bridge = Bridge(name="linux-a", server_id="srv-aaaa")
    httpd, prev = _serve(bridge)
    try:
        port = httpd.server_address[1]
        home = tmp_path / "phone-home"
        prefix = tmp_path / "phone-usr"
        repo = _copy_repo_checkout(tmp_path)
        env = _sandbox_env(tmp_path, home, prefix)
        proc = subprocess.run(["bash", "./install-termux.sh"], env=env,
                              capture_output=True, text=True, timeout=60,
                              cwd=str(repo))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        (tmp_path / "phone-data").mkdir(parents=True, exist_ok=True)
        (tmp_path / "phone-data" / "client.json").write_text(json.dumps(
            {"server_url": f"http://127.0.0.1:{port}", "key": key}))
        proc = subprocess.run(
            ["timeout", "10", str(prefix / "bin" / "monico-connect")],
            env=env, capture_output=True, text=True, timeout=60,
            stdin=subprocess.DEVNULL, cwd=str(tmp_path))
        assert proc.returncode == 124, proc.stdout + proc.stderr
        assert f"connected to http://127.0.0.1:{port}" in proc.stdout, \
            proc.stdout + proc.stderr
        assert "4-digit code" not in proc.stdout + proc.stderr
    finally:
        httpd.shutdown()
        server_mod.BRIDGE = prev
