"""The TLS trust store of a frozen (PyInstaller) build.

A standalone executable ships the OpenSSL it was built with, and OpenSSL
looks for its CA certificates at a path compiled in on the *build* machine.
For the macOS release that is python.org's
``/Library/Frameworks/Python.framework/Versions/3.11/etc/openssl/cert.pem``,
which exists on almost no user's Mac. Python's default SSL context
(``ssl.create_default_context()`` — used by aiohttp, urllib, websockets, …)
then starts with an **empty** trust store and every HTTPS connection fails
with ``CERTIFICATE_VERIFY_FAILED``: "self-signed certificate in certificate
chain" when the server sends its root along, "unable to get local issuer
certificate" otherwise. httpx is unaffected because it loads certifi's bundle
itself — which is why ``mira community login`` (httpx) worked while the
desktop app's "join the community" (aiohttp in the UI channel) did not.

:func:`ensure_ca_bundle` runs first thing in the frozen executable. When the
compiled-in CA file and directory are both missing and the operator has not
set ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``, it points ``SSL_CERT_FILE`` at the
certifi bundle PyInstaller packs. OpenSSL reads these variables whenever a
context loads its default certificates, so every library that relies on the
default context is repaired at once, and an operator who needs a corporate CA
still only has to set ``SSL_CERT_FILE``. Development installs are left alone:
their interpreter's OpenSSL path exists.
"""

from __future__ import annotations

import os
import ssl
import sys
from collections.abc import Callable, Mapping, MutableMapping


def pick_ca_bundle(
    *,
    frozen: bool,
    environ: Mapping[str, str],
    cafile: str | None,
    capath: str | None,
    certifi_where: Callable[[], str] | None,
) -> str | None:
    """The CA file to force via ``SSL_CERT_FILE``, or None to leave OpenSSL alone.

    ``cafile`` / ``capath`` are OpenSSL's defaults *as they exist on this
    machine* (``ssl.get_default_verify_paths()`` returns None for a missing
    one). Pure, so the decision is testable without a frozen build.
    """
    if not frozen:
        return None
    if environ.get("SSL_CERT_FILE") or environ.get("SSL_CERT_DIR"):
        return None  # the operator decided; maybe a corporate CA bundle
    if cafile or capath:
        return None  # the compiled-in path happens to exist here
    if certifi_where is None:
        return None
    try:
        path = certifi_where()
    except Exception:  # noqa: BLE001 - a broken certifi must not stop the executable
        return None
    return path if path and os.path.isfile(path) else None


def ensure_ca_bundle(environ: MutableMapping[str, str] = os.environ) -> str | None:
    """Repair the frozen build's trust store if needed; returns the path forced, if any."""
    paths = ssl.get_default_verify_paths()
    try:
        from certifi import where
    except ImportError:
        where = None
    chosen = pick_ca_bundle(
        frozen=bool(getattr(sys, "frozen", False)),
        environ=environ,
        cafile=paths.cafile,
        capath=paths.capath,
        certifi_where=where,
    )
    if chosen:
        environ["SSL_CERT_FILE"] = chosen
    return chosen
