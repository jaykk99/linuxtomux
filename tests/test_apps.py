"""Unit tests: hosted-app registry, probe classification, probe
cache, and the /api/apps endpoint.
Run: python3 -m pytest tests/ -q   (or python3 tests/test_apps.py)"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONICO_HOME", "/tmp/monico-apps-test-home")

from bridge import apps as apps_mod  # noqa: E402
from bridge import server as server_mod  # noqa: E402
from bridge.common import get_or_create_key  # noqa: E402

GOOD_BODY = ("<html><body><h1>Monico Agent</h1>"
             "<p>ReconAnything - scan anything.</p></body></html>")
BLANK_BODY = "<html><head><title>Monico Labs</title></head><body></body></html>"
SPA_SHELL = ('<html><head><title>Monico Labs</title>'
             '<script type="module" crossorigin src="/assets/index-abc.js">'
             '</script></head><body><div id="root"></div></body></html>')
APP = apps_mod.APPS[0]


def test_registry_has_monico_labs_storefront():
    assert APP["id"] == "monico-labs"
    assert APP["url"] == "https://monico-labs-insta.onrender.com/"
    assert "Monico Agent" in APP["markers"]
    assert "ReconAnything" in APP["markers"]


def test_probe_up_when_200_with_markers():
    probe = apps_mod.AppProbe(fetch=lambda url: (200, GOOD_BODY), ttl=60)
    rec = probe.status(APP, now=1000.0)
    assert rec["status"] == "up" and rec["ok"] is True, rec
    assert rec["id"] == "monico-labs" and rec["url"] == APP["url"]


def test_probe_blank_when_200_without_markers():
    probe = apps_mod.AppProbe(fetch=lambda url: (200, BLANK_BODY), ttl=60)
    rec = probe.status(APP, now=1000.0)
    assert rec["status"] == "blank" and rec["ok"] is False, rec
    assert "markers" in rec["reason"], rec


def test_extract_bundle_urls():
    urls = apps_mod.extract_bundle_urls(SPA_SHELL, APP["url"])
    assert urls == ["https://monico-labs-insta.onrender.com/assets/index-abc.js"], urls
    assert apps_mod.extract_bundle_urls(BLANK_BODY, APP["url"]) == []


def test_probe_up_via_bundle_for_spa_shell():
    # The real storefront: HTML shell has no markers, its JS bundle does.
    def fetch(url):
        if url.endswith(".js"):
            return 200, GOOD_BODY
        return 200, SPA_SHELL
    probe = apps_mod.AppProbe(fetch=fetch, ttl=60)
    rec = probe.status(APP, now=1000.0)
    assert rec["status"] == "up" and rec["ok"] is True, rec
    assert "bundle" in rec["reason"], rec


def test_probe_blank_when_bundle_also_lacks_markers():
    def fetch(url):
        if url.endswith(".js"):
            return 200, "var x = 1; // no storefront content here"
        return 200, SPA_SHELL
    probe = apps_mod.AppProbe(fetch=fetch, ttl=60)
    rec = probe.status(APP, now=1000.0)
    assert rec["status"] == "blank" and rec["ok"] is False, rec
    assert "bundle" in rec["reason"], rec


def test_probe_down_on_http_error():
    probe = apps_mod.AppProbe(fetch=lambda url: (503, "oops"), ttl=60)
    rec = probe.status(APP, now=1000.0)
    assert rec["status"] == "down" and rec["ok"] is False, rec
    assert "503" in rec["reason"], rec


def test_probe_down_on_fetch_error():
    def boom(url):
        raise OSError("connection refused")
    probe = apps_mod.AppProbe(fetch=boom, ttl=60)
    rec = probe.status(APP, now=1000.0)
    assert rec["status"] == "down" and rec["ok"] is False, rec
    assert "connection refused" in rec["reason"], rec


def test_probe_result_is_cached():
    calls = []

    def counting(url):
        calls.append(url)
        return 200, GOOD_BODY

    probe = apps_mod.AppProbe(fetch=counting, ttl=60)
    probe.status(APP, now=1000.0)
    probe.status(APP, now=1030.0)   # inside the TTL: no second fetch
    assert len(calls) == 1, calls
    probe.status(APP, now=1070.0)   # past the TTL: probes again
    assert len(calls) == 2, calls


def test_api_apps_endpoint():
    # The endpoint must hand back the Monico Labs entry with the
    # probe's classification, and stay behind the owner key like
    # /api/devices. Probe fetch is faked: no real network in tests.
    server_mod.BRIDGE.app_probe = apps_mod.AppProbe(
        fetch=lambda url: (200, GOOD_BODY), ttl=60)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.make_handler())
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/apps", timeout=10)
            raise AssertionError("expected 401 without the owner key")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401, exc.code
        key, _ = get_or_create_key()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/apps",
            headers={"X-Pair-Key": key})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    finally:
        httpd.shutdown()
    entries = {a["id"]: a for a in data["apps"]}
    assert "monico-labs" in entries, data
    entry = entries["monico-labs"]
    assert entry["url"] == "https://monico-labs-insta.onrender.com/"
    assert entry["status"] == "up" and entry["ok"] is True, entry


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
