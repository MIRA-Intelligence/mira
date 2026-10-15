import os
import stat

import pytest

from mira_engine.security.gateway_auth import gateway_auth_middleware, load_gateway_token


def test_token_is_persisted_and_reused_with_private_permissions(tmp_path):
    token = load_gateway_token(tmp_path)
    path = tmp_path / "runtime" / "gateway-token"
    assert len(token) >= 32
    assert path.read_text().strip() == token
    assert load_gateway_token(tmp_path) == token
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("value", ["", "short", "valid-token-with spaces", "x" * 513])
def test_invalid_existing_token_fails_closed(tmp_path, value):
    path = tmp_path / "runtime" / "gateway-token"
    path.parent.mkdir()
    path.write_text(value)
    path.chmod(0o600)
    with pytest.raises(ValueError, match="Invalid gateway token"):
        load_gateway_token(tmp_path)
    assert path.read_text() == value


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions and link semantics")
def test_token_symlink_is_rejected_without_changing_target(tmp_path):
    original = tmp_path / "original"
    original.write_text("test-token-1234567890")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "gateway-token").symlink_to(original)
    with pytest.raises(ValueError, match="symbolic link"):
        load_gateway_token(tmp_path)
    assert original.read_text() == "test-token-1234567890"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_shared_token_file_permissions_are_rejected(tmp_path):
    load_gateway_token(tmp_path)
    path = tmp_path / "runtime" / "gateway-token"
    path.chmod(0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        load_gateway_token(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX hard links")
def test_hard_linked_token_is_rejected(tmp_path):
    load_gateway_token(tmp_path)
    path = tmp_path / "runtime" / "gateway-token"
    os.link(path, tmp_path / "other-link")
    with pytest.raises(ValueError, match="without hard links"):
        load_gateway_token(tmp_path)


@pytest.mark.parametrize("value", ["", "short", "contains invalid spaces"])
def test_middleware_cannot_be_created_without_valid_token(value):
    with pytest.raises(ValueError, match="valid gateway token"):
        gateway_auth_middleware(value)
