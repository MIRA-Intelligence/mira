"""Tests for the community channel reconnect/auth-failure handling."""

from types import SimpleNamespace

import websockets.exceptions as wse
from websockets.frames import Close

from mira_engine.channels.community import _is_auth_error


def test_connection_closed_1008_is_auth_error():
    exc = wse.ConnectionClosedError(Close(1008, "unauthorized"), Close(1008, "unauthorized"), True)
    assert _is_auth_error(exc) is True


def test_connection_closed_reason_unauthorized_is_auth_error():
    # Even with a non-1008 code, an explicit unauthorized/forbidden reason counts.
    exc = wse.ConnectionClosedError(Close(1011, "forbidden: bad token"), None, None)
    assert _is_auth_error(exc) is True


def test_connection_closed_other_code_is_not_auth_error():
    exc = wse.ConnectionClosedError(Close(1011, "internal error"), None)
    assert _is_auth_error(exc) is False


def test_invalid_status_401_is_auth_error():
    exc = wse.InvalidStatus.__new__(wse.InvalidStatus)
    exc.response = SimpleNamespace(status_code=401)
    assert _is_auth_error(exc) is True


def test_invalid_status_500_is_not_auth_error():
    exc = wse.InvalidStatus.__new__(wse.InvalidStatus)
    exc.response = SimpleNamespace(status_code=500)
    assert _is_auth_error(exc) is False


def test_generic_network_error_is_not_auth_error():
    assert _is_auth_error(OSError("connection refused")) is False
    assert _is_auth_error(TimeoutError("open timeout")) is False
