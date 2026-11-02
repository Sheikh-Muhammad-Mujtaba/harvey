"""Username/password sign-in for the dashboard, with lockout.

Signing in trades the configured credentials for a session token that the
browser then sends exactly like the dashboard token (``Authorization:
Bearer``), so every other check in dashboard_auth still applies.

Credentials live in .env, never in code:

    HARVEY_DASHBOARD_USER=<username>
    HARVEY_DASHBOARD_PASSWORD_HASH=pbkdf2_sha256:<iterations>:<salt hex>:<hash hex>

``python -m harvey.dashboard_login`` prints a hash for a password typed at
the prompt. They are read on every attempt, so a change takes effect
without a restart. With either one missing, sign-in is off.

Lockout: MAX_FAILURES wrong attempts lock both the client's IP address and
its device (a random ID the browser keeps in localStorage and sends as
``X-Harvey-Device``) for LOCK_SECONDS. While locked, every attempt from that
IP or device is refused before the password is looked at, so a correct one
doesn't get through either. Failures older than LOCK_SECONDS are forgotten,
and a successful sign-in clears the count. The state is kept in a JSON file
so a restart doesn't reset it.

The client IP is the socket peer, or, when the peer is loopback (the nginx
in front of a public dashboard), the single ``X-Real-IP`` it sets. The
dashboard only binds to loopback, so nothing remote can supply that header
directly.

Sessions are kept in memory: restarting the dashboard signs everyone out.
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

MAX_FAILURES = 3
LOCK_SECONDS = 2 * 60 * 60
SESSION_SECONDS = 12 * 60 * 60
PBKDF2_ITERATIONS = 600_000

USER_KEY = "HARVEY_DASHBOARD_USER"
HASH_KEY = "HARVEY_DASHBOARD_PASSWORD_HASH"
DEVICE_HEADER = "x-harvey-device"
DEVICE_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")


# ── Passwords ──


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256:{iterations}:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt, expected = encoded.strip().split(":")
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(digest, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def check_credentials(username: str, password: str, user: str, encoded: str) -> bool:
    """Both compared every time, so a wrong username takes as long as a
    wrong password."""
    user_ok = hmac.compare_digest(username.encode("utf-8"), user.encode("utf-8"))
    password_ok = verify_password(password, encoded)
    return user_ok and password_ok


# ── Who is asking ──


def client_ip(scope, headers: dict[str, list[str]]) -> str:
    peer = (scope.get("client") or ("", 0))[0] or ""
    try:
        peer_is_loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        peer_is_loopback = False
    forwarded = headers.get("x-real-ip", [])
    if peer_is_loopback and len(forwarded) == 1:
        try:
            return str(ipaddress.ip_address(forwarded[0].strip()))
        except ValueError:
            pass
    return peer


def device_id(headers: dict[str, list[str]]) -> str | None:
    values = headers.get(DEVICE_HEADER, [])
    if len(values) == 1 and DEVICE_RE.fullmatch(values[0].strip()):
        return values[0].strip()
    return None


def limiter_keys(ip: str, device: str | None) -> list[str]:
    keys = [f"ip:{ip}"]
    if device:
        keys.append(f"device:{device}")
    return keys


# ── Lockout ──


class LoginLimiter:
    """Failed sign-ins per key (IP or device), persisted to a JSON file."""

    def __init__(self, path: Path, max_failures: int = MAX_FAILURES,
                 lock_seconds: int = LOCK_SECONDS):
        self.path = Path(path)
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _save(self, data: dict, now: float):
        live = {k: v for k, v in data.items()
                if max(v.get("locked_until", 0), v.get("since", 0) + self.lock_seconds) > now}
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".login-guard.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                if os.name != "nt":
                    os.fchmod(f.fileno(), 0o600)
                json.dump(live, f)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def locked_for(self, keys: list[str], now: float | None = None) -> int:
        """Seconds until every key may try again (0 if none is locked)."""
        now = time.time() if now is None else now
        with self._lock:
            data = self._load()
        return max([int(data.get(k, {}).get("locked_until", 0) - now) for k in keys] + [0])

    def record_failure(self, keys: list[str], now: float | None = None) -> int:
        """Count a failure against each key; seconds locked if this one locks."""
        now = time.time() if now is None else now
        with self._lock:
            data = self._load()
            locked = 0
            for key in keys:
                entry = data.get(key) or {}
                if entry.get("since", 0) + self.lock_seconds <= now:
                    entry = {"fails": 0, "since": now, "locked_until": 0}
                entry["fails"] = entry.get("fails", 0) + 1
                if entry["fails"] >= self.max_failures:
                    entry["locked_until"] = now + self.lock_seconds
                    locked = self.lock_seconds
                data[key] = entry
            self._save(data, now)
            return locked

    def failures_left(self, keys: list[str], now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            data = self._load()
        used = max([data.get(k, {}).get("fails", 0) for k in keys
                    if data.get(k, {}).get("since", 0) + self.lock_seconds > now] + [0])
        return max(self.max_failures - used, 0)

    def clear(self, keys: list[str], now: float | None = None):
        now = time.time() if now is None else now
        with self._lock:
            data = self._load()
            if any(k in data for k in keys):
                for key in keys:
                    data.pop(key, None)
                self._save(data, now)


# ── Sessions ──


class SessionStore:
    """Session tokens issued at sign-in. Stored as SHA-256 digests, so a
    lookup never compares the secret itself."""

    def __init__(self, lifetime: int = SESSION_SECONDS):
        self.lifetime = lifetime
        self._lock = threading.Lock()
        self._sessions: dict[str, float] = {}

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._sessions = {k: v for k, v in self._sessions.items() if v > now}
            self._sessions[self._key(token)] = now + self.lifetime
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            return self._sessions.get(self._key(token), 0) > time.time()

    def revoke(self, token: str | None):
        if token:
            with self._lock:
                self._sessions.pop(self._key(token), None)


def _main():
    password = getpass.getpass("Dashboard password: ")
    if password != getpass.getpass("Again: "):
        raise SystemExit("The passwords don't match.")
    print(f"{HASH_KEY}={hash_password(password)}")


if __name__ == "__main__":
    _main()
