"""Shared bits for the Monico Labs Termux <-> Linux bridge.

Stdlib only, so the same code runs on a Linux box and inside Termux
(Android) with zero pip installs. Whoever holds the pairing key gets
shell access on BOTH ends, so treat it like the linux-remote API key:
0600 file, never printed after setup, never committed.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

VERSION = "0.1.0"

DATA_DIR = Path(os.environ.get("MONICO_HOME", str(Path.home() / ".monico-bridge"))).expanduser()
KEY_FILE = DATA_DIR / "pairing-key"
CLIENT_CONFIG = DATA_DIR / "client.json"
MODEL_CONFIG = DATA_DIR / "model.json"
LOG_FILE = DATA_DIR / "bridge.log"

DEFAULT_PORT = int(os.environ.get("MONICO_PORT", "8765"))
CLIENT_SERVICE_PORT = int(os.environ.get("MONICO_SERVICE_PORT", "8899"))


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        DATA_DIR.chmod(0o700)
    except OSError:
        pass
    return DATA_DIR


def get_or_create_key() -> tuple[str, bool]:
    """Same pattern as linux-remote: one bearer key, 0600, shown once."""
    ensure_data_dir()
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip(), False
    key = secrets.token_urlsafe(32)
    KEY_FILE.write_text(key + "\n")
    try:
        KEY_FILE.chmod(0o600)
    except OSError:
        pass
    return key, True


def accepted_tokens() -> set[str]:
    """Pairing key + any extra owner tokens (e.g. a termux-code tc_v1
    gateway token Jay adds with `monico-server token-add`)."""
    tokens: set[str] = set()
    if KEY_FILE.exists():
        tokens.add(KEY_FILE.read_text().strip())
    extra = DATA_DIR / "accepted_tokens"
    if extra.exists():
        for line in extra.read_text().splitlines():
            line = line.strip()
            if line:
                tokens.add(line)
    env = os.environ.get("MONICO_OWNER_TOKEN", "").strip()
    if env:
        tokens.add(env)
    return {t for t in tokens if t}


def check_token(token: str | None) -> bool:
    if not token:
        return False
    return any(secrets.compare_digest(token, t) for t in accepted_tokens())


def log(msg: str) -> None:
    ensure_data_dir()
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n"
    try:
        with LOG_FILE.open("a") as fh:
            fh.write(line)
    except OSError:
        pass


# Typo-guard denylist, adapted from linux-remote. This is a bridge
# between Jay's OWN two devices behind one owner key, not a sandbox;
# the list only stops obvious fat-finger disasters.
_DENY = [
    r"rm\s+-[a-z]*r[a-z]*f[a-z]*\s+/(?:\s|$)",
    r"rm\s+-[a-z]*r[a-z]*f[a-z]*\s+~(?:\s|$)",
    r"rm\s+-[a-z]*r[a-z]*f[a-z]*\s+\$HOME(?:\s|$)",
    r"rm\s+-[a-z]*r[a-z]*f[a-z]*\s+\.\.(?:\s|$)",
    r"mkfs",
    r"dd\s+.*of=/dev/",
    r":\(\)\s*\{",
    r"\bshutdown\b",
    r"\breboot\b",
]
_DENY_RE = [re.compile(p) for p in _DENY]


def blocked_reason(command: str) -> str | None:
    if os.environ.get("MONICO_ALLOW_DANGEROUS", "") == "1":
        return None
    for rx in _DENY_RE:
        if rx.search(command):
            return f"blocked by destructive-command guard ({rx.pattern})"
    return None


def run_command(command: str, timeout_s: int = 60) -> dict:
    """Run a shell command, capture bounded output. Used on both ends."""
    timeout_s = max(1, min(int(timeout_s or 60), 600))
    reason = blocked_reason(command)
    if reason:
        return {"stdout": "", "stderr": reason, "exit_code": 126,
                "blocked": True, "duration_ms": 0}
    started = time.time()
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout_s, executable="/bin/sh",
        )
        out, err, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = f"timed out after {timeout_s}s"
        code = 124
    except Exception as exc:  # pragma: no cover - defensive
        out, err, code = "", f"exec error: {exc}", 1
    cap = 512 * 1024
    return {
        "stdout": out[:cap], "stderr": err[:cap], "exit_code": code,
        "blocked": False, "duration_ms": int((time.time() - started) * 1000),
    }


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_json(path: Path, data: dict, mode: int = 0o600) -> None:
    ensure_data_dir()
    path.write_text(json.dumps(data, indent=2) + "\n")
    try:
        path.chmod(mode)
    except OSError:
        pass


def model_status() -> dict:
    """What the model installer recorded for THIS device, if anything."""
    cfg = load_json(MODEL_CONFIG)
    if not cfg:
        return {"installed": False}
    return {"installed": True, **cfg}
