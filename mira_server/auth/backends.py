"""Password checks: PAM in production, scrypt hashes from the config in dev mode."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from mira_server.config import AuthSection

log = logging.getLogger(__name__)

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024


class PasswordBackend(Protocol):
    async def verify(self, username: str, password: str) -> bool:
        """True when ``password`` is correct for ``username``."""


class ExtraMissingError(RuntimeError):
    """The ``mira-engine[server]`` extra is not installed."""


def require_pam() -> None:
    try:
        import pam  # noqa: F401
    except ImportError:
        raise ExtraMissingError(
            "python-pam is not installed. Install Mira Server with: pip install 'mira-engine[server]'"
        ) from None


class PamBackend:
    """Authenticate through the host PAM stack (``/etc/pam.d/<service>``).

    PAM modules may block (network directories, failure delays), so each check
    runs on a small dedicated thread pool. ``pam_acct_mgmt`` runs as well, so
    expired or locked accounts are refused.
    """

    def __init__(self, service: str, *, max_workers: int = 4) -> None:
        require_pam()
        self.service = service
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mira-pam")

    def _check(self, username: str, password: str) -> bool:
        import pam

        handle = pam.pam()
        ok = bool(handle.authenticate(username, password, service=self.service, resetcreds=False))
        if not ok:
            log.info("PAM refused %s: %s", username, handle.reason)
        return ok

    async def verify(self, username: str, password: str) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._check, username, password)


# ── dev backend ──────────────────────────────────────────────────────


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    """``scrypt$<n>$<r>$<p>$<salt>$<hash>`` (base64url, unpadded)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=_SCRYPT_MAXMEM, dklen=32)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(digest)}"


def parse_password_hash(value: str) -> tuple[int, int, int, bytes, bytes]:
    parts = value.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise ValueError("password_hash must come from `mira-server hash-password` (scrypt$n$r$p$salt$hash)")
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, digest = _unb64(parts[4]), _unb64(parts[5])
    except ValueError as exc:
        raise ValueError(f"malformed password_hash: {exc}") from None
    if n < 2**10 or n & (n - 1) or not 1 <= r <= 32 or not 1 <= p <= 16 or len(salt) < 8 or len(digest) < 16:
        raise ValueError("password_hash parameters are out of range")
    return n, r, p, salt, digest


def verify_password(password: str, encoded: str) -> bool:
    try:
        n, r, p, salt, digest = parse_password_hash(encoded)
    except ValueError:
        return False
    candidate = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                               maxmem=_SCRYPT_MAXMEM, dklen=len(digest))
    return hmac.compare_digest(candidate, digest)


class DevBackend:
    """Users and scrypt hashes from ``[[auth.dev_users]]``. Local UI development only."""

    def __init__(self, auth: AuthSection) -> None:
        self._hashes = {user.name: user.password_hash for user in auth.dev_users}
        # Unknown names still cost one scrypt, so timing does not reveal valid names.
        self._dummy = hash_password(secrets.token_urlsafe(16))

    async def verify(self, username: str, password: str) -> bool:
        encoded = self._hashes.get(username)
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, verify_password, password, encoded or self._dummy)
        return ok and encoded is not None


def make_backend(auth: AuthSection) -> PasswordBackend:
    if auth.backend == "dev":
        return DevBackend(auth)
    return PamBackend(auth.pam_service)
