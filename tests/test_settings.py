"""Unit tests: AI API keys (hashed at rest) and the local app shelf,
plus their HTTP endpoints.
Run: python3 -m pytest tests/ -q   (or python3 tests/test_settings.py)
"""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONICO_HOME", "/tmp/monico-settings-test-home")

from bridge import server as server_mod  # noqa: E402
from bridge.common import get_or_create_key  # noqa: E402
from bridge.settings import AIKeyStore, LocalAppStore  # noqa: E402


def _store(cls):
    return cls(Path(tempfile.mkdtemp()) / "store.json")


def _dir():
    return Path(tempfile.mkdtemp())


# ---------------------------------------------------------------- AI keys
def test_create_returns_the_key_once_and_stores_only_a_hash():
    store = _store(AIKeyStore)
    res = store.create("laptop agent")
    assert res["ok"] is True and res["key"].startswith("mk-ai-"), res
    raw = store.path.read_text()
    assert res["key"] not in raw, "the plaintext key must never be written"
    assert "hash" in raw
    assert store.verify(res["key"])["id"] == res["id"]
    assert store.verify("mk-ai-not-a-real-key") is None
    listed = store.list()
    assert len(listed) == 1 and listed[0]["name"] == "laptop agent", listed
    assert listed[0]["prefix"] and len(listed[0]["prefix"]) < len(res["key"])
    assert "key" not in listed[0] and "hash" not in listed[0], listed[0]


def test_key_survives_a_restart_and_revoke_kills_it():
    store = _store(AIKeyStore)
    res = store.create("phone")
    again = AIKeyStore(store.path)
    assert again.verify(res["key"]), "a saved key must still verify after reload"
    assert again.revoke(res["id"]) == {"ok": True}
    assert again.verify(res["key"]) is None
    assert again.list() == []
    assert "error" in again.revoke("aik-nope")


def test_key_needs_a_name():
    store = _store(AIKeyStore)
    assert "error" in store.create("   ")
    assert store.list() == []


# ------------------------------------------------------------ local apps
def test_ready_repo_is_detected_as_a_git_checkout():
    root = _dir()
    (root / ".git").mkdir()
    store = _store(LocalAppStore)
    res = store.add("linuxtomux", "repo", str(root))
    assert res["ok"] is True, res
    app = res["app"]
    assert app["status"] == "ready" and app["ok"] is True
    assert app["git_repo"] is True and app["kind_label"] == "Repo"
    listed = store.list()
    assert len(listed) == 1 and listed[0]["path"] == str(root)


def test_plain_folder_is_ready_but_not_a_repo():
    root = _dir()
    store = _store(LocalAppStore)
    app = store.add("my tool", "software", str(root))["app"]
    assert app["ok"] is True and app["git_repo"] is False
    assert app["reason"] == "folder on disk"


def test_missing_path_is_reported_honestly():
    store = _store(LocalAppStore)
    app = store.add("gone", "app", "/nonexistent/monico-target")["app"]
    assert app["ok"] is False and app["status"] == "missing"
    assert "not found" in app["reason"]
    assert store.list()[0]["exists"] is False


def test_repo_url_without_a_checkout_is_not_local():
    store = _store(LocalAppStore)
    app = store.add("remote", "repo", "",
                    "https://github.com/jaykk99/linuxtomux")["app"]
    assert app["ok"] is False and app["status"] == "not-local"
    assert app["url"].startswith("https://")


def test_input_is_validated():
    store = _store(LocalAppStore)
    assert "error" in store.add("", "repo", "/tmp")
    assert "error" in store.add("x", "gadget", "/tmp")
    assert "error" in store.add("x", "repo", "relative/dir")
    assert "error" in store.add("x", "repo", "", "ftp://example.com/x")
    assert "error" in store.add("x", "repo", "", "")
    assert store.list() == []


def test_duplicate_names_and_removal():
    store = _store(LocalAppStore)
    first = store.add("duo", "app", str(_dir()))
    assert "error" in store.add("DUO", "app", str(_dir()))
    removed = store.remove(first["app"]["id"])
    assert removed == {"ok": True}
    assert store.list() == []
    assert "error" in store.remove("app-nope")
    # a name freed by removal can be reused
    assert store.add("duo", "app", str(_dir()))["ok"] is True


def test_shelf_survives_a_restart():
    store = _store(LocalAppStore)
    store.add("kept", "app", str(_dir()))
    again = LocalAppStore(store.path)
    assert [a["name"] for a in again.list()] == ["kept"]


# --------------------------------------------------------------- endpoints
def _server():
    server_mod.BRIDGE.ai_keys = _store(AIKeyStore)
    server_mod.BRIDGE.local_apps = _store(LocalAppStore)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.make_handler())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _get(url, key=None):
    headers = {"X-Pair-Key": key} if key else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _post(url, payload, key=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-Pair-Key"] = key
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_settings_page_and_ai_key_endpoints():
    httpd, base = _server()
    key, _ = get_or_create_key()
    try:
        code, body = _get(base + "/api/ai-keys")
        assert code == 401, (code, body)
        assert _post(base + "/api/ai-keys", {"name": "x"})[0] == 401

        # Signed out: the login form points back at the settings page.
        code, html = _get(base + "/settings")
        assert code == 200 and 'action="/login?next=/settings"' in html, html[:200]

        code, html = _get(base + "/settings", key=key)
        assert code == 200 and "AI API keys" in html and "Create key" in html

        code, created = _post(base + "/api/ai-keys", {"name": "cli"}, key=key)
        assert code == 200 and created["key"].startswith("mk-ai-"), created

        code, body = _get(base + "/api/ai-keys", key=key)
        assert code == 200
        listed = json.loads(body)["keys"]
        assert len(listed) == 1 and listed[0]["name"] == "cli"
        assert "hash" not in json.dumps(listed) and created["key"] not in json.dumps(listed)

        code, res = _post(base + "/api/ai-keys/delete",
                          {"id": created["id"]}, key=key)
        assert code == 200 and res["ok"] is True
        assert _get(base + "/api/ai-keys", key=key)[1] == '{"keys": []}'
    finally:
        httpd.shutdown()


def test_local_app_endpoints():
    httpd, base = _server()
    key, _ = get_or_create_key()
    root = _dir()
    try:
        assert _get(base + "/api/local-apps")[0] == 401
        assert _post(base + "/api/local-apps",
                     {"name": "x", "kind": "app", "path": str(root)})[0] == 401

        code, res = _post(base + "/api/local-apps",
                          {"name": "bridge", "kind": "app",
                           "path": str(root)}, key=key)
        assert code == 200 and res["app"]["ok"] is True, res

        code, body = _get(base + "/api/local-apps", key=key)
        assert code == 200
        apps = json.loads(body)["apps"]
        assert len(apps) == 1 and apps[0]["name"] == "bridge"
        assert apps[0]["status"] == "ready"

        code, res = _post(base + "/api/local-apps/delete",
                          {"id": apps[0]["id"]}, key=key)
        assert code == 200 and res["ok"] is True
        assert _get(base + "/api/local-apps", key=key)[1] == '{"apps": []}'

        # A bad entry comes back as a 400 with a readable reason.
        code, res = _post(base + "/api/local-apps",
                          {"name": "", "kind": "app"}, key=key)
        assert code == 400 and "error" in res, res
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
