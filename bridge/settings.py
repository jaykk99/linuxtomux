"""Owner settings stored on this server.

Two things the console lets the owner manage here:

* AI API keys. Creating a key returns the secret exactly once; only its
  SHA-256 hash, a short prefix for identification, and its name are
  written to ~/.monico-bridge/ai-keys.json (0600). Revoking deletes the
  record, so a leaked key file can never leak the keys themselves.

* The local app shelf. Repos, apps and software that run on THIS box.
  An entry is a name, a kind (repo/app/software), a local path and/or a
  repo URL. Nothing is started from here: the shelf records what lives
  on the machine and reports honestly whether it is still there (path
  exists, folder is a git checkout). A path that disappeared shows as
  missing, never guessed at.

Stdlib only, like the rest of the bridge.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from pathlib import Path

from .common import DATA_DIR, load_json, log, save_json

AI_KEY_PREFIX = "mk-ai-"

# kind -> how the console labels it
LOCAL_APP_KINDS = {"repo": "Repo", "app": "App", "software": "Software"}


def clean_name(value) -> str:
    """One-line display name: trimmed, inner whitespace collapsed, 60 max."""
    return " ".join(str(value or "").split())[:60]


def _public_key(rec: dict) -> dict:
    """A stored key record minus the hash: safe to hand to the UI."""
    return {"id": rec["id"], "name": rec.get("name", ""),
            "prefix": rec.get("prefix", ""), "created": rec.get("created", 0)}


class AIKeyStore:
    """AI API keys: created, listed (prefix only) and revoked.

    The secret itself is never stored. `verify()` hashes a presented key
    and compares in constant time, so a key file that leaks still gives
    nobody a working key.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DATA_DIR / "ai-keys.json"
        self._lock = threading.Lock()
        keys = load_json(self.path).get("keys")
        self._keys: list[dict] = [k for k in keys
                                  if isinstance(k, dict) and k.get("id")] \
            if isinstance(keys, list) else []

    def _save(self) -> None:
        save_json(self.path, {"keys": self._keys})

    @staticmethod
    def _hash(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    def create(self, name: str) -> dict:
        name = clean_name(name)
        if not name:
            return {"error": "give the key a name so you can tell it apart later"}
        key = AI_KEY_PREFIX + secrets.token_urlsafe(24)
        rec = {
            "id": "aik-" + secrets.token_hex(4),
            "name": name,
            "prefix": key[:len(AI_KEY_PREFIX) + 6],
            "hash": self._hash(key),
            "created": int(time.time()),
        }
        with self._lock:
            self._keys.append(rec)
            self._save()
        log(f"ai key created {rec['id']} name={name!r}")
        return {"ok": True, "key": key, **_public_key(rec)}

    def list(self) -> list[dict]:
        with self._lock:
            recs = list(self._keys)
        return [_public_key(r) for r in
                sorted(recs, key=lambda r: r.get("created", 0), reverse=True)]

    def revoke(self, key_id: str) -> dict:
        with self._lock:
            before = len(self._keys)
            self._keys = [k for k in self._keys if k["id"] != key_id]
            if len(self._keys) == before:
                return {"error": "unknown key"}
            self._save()
        log(f"ai key revoked {key_id}")
        return {"ok": True}

    def verify(self, key: str) -> dict | None:
        """The key record for a presented secret, or None."""
        if not key:
            return None
        digest = self._hash(key)
        with self._lock:
            for rec in self._keys:
                if secrets.compare_digest(digest, rec.get("hash", "")):
                    return _public_key(rec)
        return None


class LocalAppStore:
    """The local app shelf: repos, apps and software on this machine."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DATA_DIR / "local-apps.json"
        self._lock = threading.Lock()
        apps = load_json(self.path).get("apps")
        self._apps: list[dict] = [a for a in apps
                                  if isinstance(a, dict) and a.get("id")] \
            if isinstance(apps, list) else []

    def _save(self) -> None:
        save_json(self.path, {"apps": self._apps})

    def add(self, name: str = "", kind: str = "", path: str = "",
            url: str = "") -> dict:
        name = clean_name(name)
        if not name:
            return {"error": "give the app a name"}
        kind = str(kind or "").strip().lower()
        if kind not in LOCAL_APP_KINDS:
            return {"error": "kind must be repo, app or software"}
        local = None
        raw_path = str(path or "").strip()
        if raw_path:
            local = Path(raw_path).expanduser()
            if not local.is_absolute():
                return {"error": "path must be absolute (or start with ~)"}
        url = str(url or "").strip()
        if url and not url.startswith(("http://", "https://",
                                       "git@", "ssh://")):
            return {"error": "repo URL must be http(s), ssh:// or git@"}
        if local is None and not url:
            return {"error": "add a local path, a repo URL, or both"}
        with self._lock:
            if any(clean_name(a.get("name", "")).lower() == name.lower()
                   for a in self._apps):
                return {"error": f"an app named {name!r} is already on the shelf"}
            rec = {"id": "app-" + secrets.token_hex(4), "name": name,
                   "kind": kind, "path": str(local) if local is not None else "",
                   "url": url, "created": int(time.time())}
            self._apps.append(rec)
            self._save()
        log(f"local app added {rec['id']} name={name!r} kind={kind} "
            f"path={rec['path']!r} url={url!r}")
        return {"ok": True, "app": self.view(rec)}

    def list(self) -> list[dict]:
        with self._lock:
            recs = list(self._apps)
        return [self.view(r) for r in
                sorted(recs, key=lambda r: r.get("created", 0), reverse=True)]

    def remove(self, app_id: str) -> dict:
        with self._lock:
            before = len(self._apps)
            self._apps = [a for a in self._apps if a["id"] != app_id]
            if len(self._apps) == before:
                return {"error": "unknown app"}
            self._save()
        log(f"local app removed {app_id}")
        return {"ok": True}

    def view(self, rec: dict) -> dict:
        """An entry plus what is true on disk right now."""
        path = rec.get("path") or ""
        p = Path(path) if path else None
        exists = bool(p is not None and p.exists())
        is_dir = bool(exists and p.is_dir())
        git_repo = bool(is_dir and (p / ".git").exists())
        if not path:
            status, ok = "not-local", False
            reason = "no local checkout yet: clone it, then add its path"
        elif not exists:
            status, ok = "missing", False
            reason = "path not found on this server"
        elif not is_dir:
            status, ok = "missing", False
            reason = "path is not a folder"
        else:
            status, ok = "ready", True
            reason = "git checkout" if git_repo else "folder on disk"
        kind = rec.get("kind", "")
        return {
            "id": rec["id"], "name": rec.get("name", ""), "kind": kind,
            "kind_label": LOCAL_APP_KINDS.get(kind, kind.title() or "App"),
            "path": path, "url": rec.get("url", ""),
            "created": rec.get("created", 0),
            "exists": exists, "is_dir": is_dir, "git_repo": git_repo,
            "status": status, "ok": ok, "reason": reason,
        }
