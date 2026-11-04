"""Hosted-app registry + live status probes for the homescreen.

Apps are Monico Labs web properties the bridge links OUT to (they are
not devices and nothing runs on them from here). Each app gets a
server-side probe whose result is cached briefly, so the homescreen
can show a truthful live status without hammering the site on every
page load.

A 200 alone is NOT "up": this fleet has already shipped a Monico Labs
page that answered 200 while rendering completely blank. The probe
therefore requires the app's content markers in what it serves. A 200
with the markers missing classifies as "blank", an HTTP error or a
failed fetch as "down", and only 200 + markers is "up".

One wrinkle: the storefront is a client-rendered app, so its HTML is
a small shell and the product names live in its JS bundle. When the
shell lacks the markers, the probe also checks the bundle(s) the
shell points at before calling the page blank: markers in the bundle
still mean the server is delivering the full app. A page whose HTML
and bundle both lack the markers reports "blank" with the reason.

Stdlib only, like the rest of the bridge.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.request
from urllib.parse import urljoin

STOREFRONT_URL = "https://monico-labs-insta.onrender.com/"
PROBE_TIMEOUT_S = 10
CACHE_TTL_S = 45  # homescreen polls every 20s; a probe stays fresh ~2 loads
MAX_BUNDLES = 3   # JS bundles to check when the HTML shell has no markers

_SCRIPT_SRC = re.compile(r'<script[^>]+src="([^"]+)"', re.IGNORECASE)

APPS: list[dict] = [
    {
        "id": "monico-labs",
        "name": "Monico Labs",
        "url": STOREFRONT_URL,
        "blurb": "Software storefront: Monico Agent, ReconAnything, "
                 "Wholesale Stack, Code Runner.",
        "markers": ["Monico Agent", "ReconAnything"],
    },
]


def default_fetch(url: str, timeout: float = PROBE_TIMEOUT_S) -> tuple[int, str]:
    req = urllib.request.Request(
        url, headers={"User-Agent": "monico-bridge-probe/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def classify(status_code: int | None, body: str,
             markers: list[str]) -> tuple[str, str]:
    """(status, reason) for one probe result.

    status is "up", "blank" or "down". When status_code is None the
    fetch itself failed and `body` carries the error text."""
    if status_code is None:
        return "down", body or "probe failed"
    if status_code != 200:
        return "down", f"HTTP {status_code}"
    missing = [m for m in markers if m not in (body or "")]
    if missing:
        return "blank", ("HTTP 200 but the page is missing its content "
                         "markers: " + ", ".join(missing))
    return "up", "HTTP 200 and content markers present"


def extract_bundle_urls(html: str, base_url: str) -> list[str]:
    """Absolute URLs of the scripts an HTML page loads, in order."""
    out: list[str] = []
    for src in _SCRIPT_SRC.findall(html or ""):
        url = urljoin(base_url, src)
        if url not in out:
            out.append(url)
    return out


class AppProbe:
    """Per-app probe with a short in-memory cache. `fetch` is
    injectable so tests never touch the network."""

    def __init__(self, fetch=None, ttl: float = CACHE_TTL_S) -> None:
        self.fetch = fetch or default_fetch
        self.ttl = ttl
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    def status(self, app: dict, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            hit = self._cache.get(app["id"])
            if hit and now - hit["checked_at"] < self.ttl:
                return dict(hit)
        try:
            code, body = self.fetch(app["url"])
            status, reason = classify(code, body, app.get("markers", []))
            if status == "blank":
                status, reason = self._bundle_check(
                    app, body, status, reason)
        except Exception as exc:
            status, reason = "down", f"probe error: {exc}"
        rec = {
            "id": app["id"], "name": app["name"], "url": app["url"],
            "blurb": app.get("blurb", ""),
            "status": status, "ok": status == "up", "reason": reason,
            "checked_at": int(now),
        }
        with self._lock:
            self._cache[app["id"]] = rec
        return dict(rec)

    def _bundle_check(self, app: dict, html: str, status: str,
                      reason: str) -> tuple[str, str]:
        """Client-rendered apps keep their content in the JS bundle,
        not the HTML shell. Before calling such a page blank, look for
        the markers in the bundle(s) its shell loads."""
        markers = app.get("markers", [])
        urls = extract_bundle_urls(html, app["url"])[:MAX_BUNDLES]
        if not urls:
            return status, reason + " (no app bundle found to check)"
        try:
            blob = ""
            for url in urls:
                code, body = self.fetch(url)
                if code == 200:
                    blob += body
        except Exception as exc:
            return status, reason + f" (bundle check failed: {exc})"
        missing = [m for m in markers if m not in blob]
        if not missing:
            return "up", ("HTTP 200; markers are not in the HTML shell "
                          "(client-rendered app) but are present in "
                          "its JS bundle")
        return status, reason + " (also missing from the app bundle)"

    def statuses(self, apps: list[dict] | None = None) -> list[dict]:
        return [self.status(a) for a in (APPS if apps is None else apps)]
