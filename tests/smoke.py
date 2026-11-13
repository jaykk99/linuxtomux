#!/usr/bin/env python3
"""Live smoke test for the real bridge server.

The unit suites cover internals. This starts the server from this
checkout against a throwaway MONICO_HOME and an ephemeral port, then
walks the routes the homescreen, dashboard, settings page, OpenAI
endpoint and phone installer actually use.

It needs no model, no network and no llama.cpp: /v1/models is checked
without starting an engine.

    python3 tests/smoke.py

CI runs it after the unit suites.
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START_TIMEOUT_S = 20


class SmokeError(Exception):
    pass


class Client:
    def __init__(self, port):
        self.base = "http://127.0.0.1:%d" % port

    def request(self, method, path, body=None, headers=None):
        data = None
        headers = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until_ready(client, proc):
    deadline = time.time() + START_TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SmokeError("server exited with status %s" % proc.returncode)
        try:
            status, body, _ = client.request("GET", "/health")
            if status == 200:
                return json.loads(body.decode("utf-8"))
        except Exception:
            pass
        time.sleep(0.25)
    raise SmokeError("/health did not answer within %ss" % START_TIMEOUT_S)


def read_key(home):
    path = Path(home) / "pairing-key"
    deadline = time.time() + 5
    while time.time() < deadline:
        if path.exists():
            key = path.read_text().strip()
            if key:
                return key
        time.sleep(0.05)
    raise SmokeError("pairing key was not written to %s" % path)


def run():
    home = tempfile.mkdtemp(prefix="monico-smoke-")
    log_path = Path(home) / "server.log"
    log = log_path.open("wb")
    port = free_port()
    proc = None
    passed = []
    try:
        env = dict(os.environ)
        env["MONICO_HOME"] = home
        env["MONICO_PORT"] = str(port)
        env.pop("MONICO_LLAMA_URL", None)
        env.pop("MONICO_MODEL_FILE", None)
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "monico-server"), "start"],
            cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
        client = Client(port)
        health = wait_until_ready(client, proc)
        key = read_key(home)

        def check(name, ok, detail=""):
            if not ok:
                raise SmokeError("%s%s" % (name, ": " + detail if detail else ""))
            passed.append(name)
            print("PASS %s" % name)

        owner = {"X-Pair-Key": key}

        check("health",
              health.get("ok") is True
              and health.get("service") == "monico-bridge",
              health)

        status, body, headers = client.request("GET", "/console.css")
        check("console stylesheet",
              status == 200
              and headers.get("Content-Type", "").startswith("text/css")
              and b"--acid" in body,
              "status %s" % status)

        status, body, _ = client.request("GET", "/", headers=owner)
        check("homescreen", status == 200 and b"Monico" in body,
              "status %s" % status)

        status, body, _ = client.request("GET", "/settings", headers=owner)
        check("settings page",
              status == 200 and b"AI API keys" in body
              and b"/console.css" in body,
              "status %s" % status)

        status, body, _ = client.request("GET", "/settings")
        check("settings needs the owner key",
              status == 200 and b"next=/settings" in body
              and b"AI API keys" not in body
              and b"/console.css" in body,
              "status %s" % status)

        status, body, _ = client.request("GET", "/dashboard", headers=owner)
        check("dashboard", status == 200 and b"AI engine" in body,
              "status %s" % status)

        status, body, _ = client.request("GET", "/api/ai-keys", headers=owner)
        keys = json.loads(body.decode("utf-8")) if status == 200 else {}
        check("ai key list starts empty",
              status == 200 and keys.get("keys") == [],
              "status %s %s" % (status, body[:120]))

        status, body, _ = client.request("GET", "/api/ai-keys")
        check("ai keys need the owner key", status == 401,
              "status %s" % status)

        status, body, _ = client.request("POST", "/api/ai-keys",
                                         {"name": "ci smoke"}, owner)
        created = json.loads(body.decode("utf-8")) if status == 200 else {}
        ai_key = created.get("key", "")
        check("create ai key",
              status == 200 and ai_key.startswith("mk-ai-"),
              "status %s %s" % (status, body[:160]))

        status, body, _ = client.request("GET", "/api/ai-keys", headers=owner)
        listed = json.loads(body.decode("utf-8")).get("keys", []) \
            if status == 200 else []
        check("list ai key by name",
              status == 200 and [k.get("name") for k in listed] == ["ci smoke"],
              "status %s" % status)

        status, body, _ = client.request(
            "GET", "/v1/models",
            headers={"Authorization": "Bearer " + ai_key})
        models = json.loads(body.decode("utf-8")) if status == 200 else {}
        check("openai models",
              status == 200 and models.get("object") == "list"
              and isinstance(models.get("data"), list),
              "status %s %s" % (status, body[:160]))

        status, body, _ = client.request("GET", "/v1/models", headers=owner)
        check("pairing key is not an ai key", status == 401,
              "status %s" % status)

        status, body, _ = client.request(
            "GET", "/v1/models",
            headers={"Authorization": "Bearer nope"})
        check("bad ai key rejected", status == 401, "status %s" % status)

        status, body, headers = client.request("OPTIONS", "/v1/models")
        check("cors preflight",
              status == 204
              and headers.get("Access-Control-Allow-Origin") == "*",
              "status %s" % status)

        status, body, _ = client.request("GET", "/api/local-apps", headers=owner)
        apps = json.loads(body.decode("utf-8")) if status == 200 else {}
        check("local apps shelf",
              status == 200 and apps.get("apps") == [],
              "status %s" % status)

        status, body, _ = client.request("GET", "/api/ai-engine", headers=owner)
        engine = json.loads(body.decode("utf-8")).get("engine", {}) \
            if status == 200 else {}
        check("engine status",
              status == 200 and engine.get("managed") is True
              and "state" in engine,
              "status %s %s" % (status, body[:160]))

        status, body, _ = client.request("GET", "/api/ai-engine")
        check("engine status needs a key", status == 401,
              "status %s" % status)

        status, body, _ = client.request("GET", "/linuxtomux.tar.gz")
        tarball = body if status == 200 else b""
        check("install tarball",
              status == 200 and tarball[:2] == b"\x1f\x8b",
              "status %s" % status)
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
            names = tf.getnames()
        check("tarball contents",
              "bridge/server.py" in names
              and "ui/settings.html" in names
              and "ui/console.css" in names
              and "install-termux.sh" in names
              and not any("__pycache__" in n or n.endswith(".pyc")
                          for n in names),
              "%d entries" % len(names))

        status, body, _ = client.request("GET", "/linuxtomux.tar.gz")
        check("tarball is stable", status == 200 and body == tarball,
              "second fetch differed")

        status, body, _ = client.request("POST", "/api/ai-keys/delete",
                                         {"id": created.get("id", "")}, owner)
        check("revoke ai key", status == 200, "status %s" % status)

        status, body, _ = client.request(
            "GET", "/v1/models",
            headers={"Authorization": "Bearer " + ai_key})
        check("revoked ai key rejected", status == 401, "status %s" % status)

        print("\n%d smoke checks passed" % len(passed))
    except Exception as exc:
        log.flush()
        try:
            lines = log_path.read_text(errors="replace").splitlines()[-30:]
            tail = "\n".join(lines)
        except OSError:
            tail = "no server log"
        if isinstance(exc, SmokeError):
            raise SmokeError("%s\n--- server log ---\n%s" % (exc, tail)) from None
        sys.stderr.write("--- server log ---\n%s\n" % tail)
        raise
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log.close()
        if "--keep" in sys.argv:
            print("kept %s" % home)
        else:
            shutil.rmtree(home, ignore_errors=True)


def main():
    try:
        run()
    except SmokeError as exc:
        print("FAIL smoke: %s" % exc, file=sys.stderr)
        return 1
    except Exception:
        import traceback
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
