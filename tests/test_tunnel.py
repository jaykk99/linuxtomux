"""Unit tests: the Cloudflare quick-tunnel helper.
Run: python3 -m pytest tests/ -q"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bridge import tunnel  # noqa: E402


def test_missing_cloudflared_returns_none(monkeypatch):
    monkeypatch.setattr(tunnel, "find_cloudflared", lambda: None)
    assert tunnel.start_quick_tunnel(18765, lambda url: None) is None
    assert "cloudflared" in tunnel.CLOUDFLARED_INSTALL_NOTE
    assert "curl" in tunnel.CLOUDFLARED_INSTALL


def test_fake_cloudflared_reports_public_url(tmp_path, monkeypatch):
    stub = tmp_path / "cloudflared"
    stub.write_text(
        "#!/bin/sh\n"
        "echo '2026-11-12T00:00:00Z INF https://test-abc-123.trycloudflare.com "
        "has been started' >&2\n"
        "sleep 30\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + ":" + os.environ["PATH"])
    seen: list[str] = []
    done = threading.Event()
    proc = tunnel.start_quick_tunnel(
        18765, lambda url: (seen.append(url), done.set()))
    assert proc is not None
    try:
        assert done.wait(timeout=10), "no URL reported by fake cloudflared"
        assert seen == ["https://test-abc-123.trycloudflare.com"]
    finally:
        tunnel.stop_tunnel(proc)
        assert proc.poll() is not None


def test_tunnel_url_regex():
    assert tunnel.TUNNEL_URL_RE.search(
        "INF https://foo-bar-9.trycloudflare.com started")
    assert not tunnel.TUNNEL_URL_RE.search("no url here")
