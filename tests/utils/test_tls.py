"""The frozen build's TLS trust store (mira_engine.utils.tls).

The released macOS executable carried OpenSSL whose compiled-in CA path was
python.org's framework path from the build runner; on a user's Mac that path
is missing, the default SSL context had zero CAs and the desktop app's "join
the community" failed with CERTIFICATE_VERIFY_FAILED while the httpx-based
CLI worked. These tests pin the decision logic and the OpenSSL behaviour the
fix relies on.
"""

from __future__ import annotations

import ssl
import sys

import certifi
import pytest

from mira_engine.utils import tls


def _pick(**overrides):
    kwargs = dict(frozen=True, environ={}, cafile=None, capath=None, certifi_where=certifi.where)
    kwargs.update(overrides)
    return tls.pick_ca_bundle(**kwargs)


def test_a_development_install_is_left_alone() -> None:
    assert _pick(frozen=False) is None


def test_an_operator_override_wins() -> None:
    assert _pick(environ={"SSL_CERT_FILE": "/corp/ca-bundle.pem"}) is None
    assert _pick(environ={"SSL_CERT_DIR": "/corp/certs"}) is None


def test_a_compiled_in_path_that_exists_here_is_trusted() -> None:
    assert _pick(cafile="/etc/ssl/cert.pem") is None
    assert _pick(capath="/etc/ssl/certs") is None


def test_the_build_machine_path_being_gone_falls_back_to_certifi() -> None:
    assert _pick() == certifi.where()


def test_without_a_usable_certifi_nothing_is_forced(tmp_path) -> None:
    assert _pick(certifi_where=None) is None
    assert _pick(certifi_where=lambda: str(tmp_path / "missing.pem")) is None

    def broken() -> str:
        raise RuntimeError("certifi exploded")

    assert _pick(certifi_where=broken) is None


def _no_defaults(tmp_path) -> ssl.DefaultVerifyPaths:
    return ssl.DefaultVerifyPaths(
        None,
        None,
        "SSL_CERT_FILE",
        str(tmp_path / "Python.framework" / "etc" / "openssl" / "cert.pem"),
        "SSL_CERT_DIR",
        str(tmp_path / "Python.framework" / "etc" / "openssl" / "certs"),
    )


def test_ensure_sets_ssl_cert_file_in_a_frozen_build(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: _no_defaults(tmp_path))
    env: dict[str, str] = {}
    assert tls.ensure_ca_bundle(env) == certifi.where()
    assert env == {"SSL_CERT_FILE": certifi.where()}


def test_ensure_does_nothing_outside_a_frozen_build(monkeypatch, tmp_path) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: _no_defaults(tmp_path))
    env: dict[str, str] = {}
    assert tls.ensure_ca_bundle(env) is None
    assert env == {}


def test_ensure_keeps_an_operator_bundle(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: _no_defaults(tmp_path))
    env = {"SSL_CERT_FILE": "/corp/ca-bundle.pem"}
    assert tls.ensure_ca_bundle(env) is None
    assert env == {"SSL_CERT_FILE": "/corp/ca-bundle.pem"}


@pytest.mark.skipif(sys.platform == "win32", reason="Windows also loads the OS certificate store")
def test_the_default_context_follows_ssl_cert_file(monkeypatch, tmp_path) -> None:
    # What the frozen build suffered: OpenSSL's paths point nowhere → zero CAs.
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "missing"))
    assert ssl.create_default_context().cert_store_stats()["x509_ca"] == 0
    # What the fix does: the variable alone repairs every default context.
    monkeypatch.setenv("SSL_CERT_FILE", certifi.where())
    assert ssl.create_default_context().cert_store_stats()["x509_ca"] > 100
