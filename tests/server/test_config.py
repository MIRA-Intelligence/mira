from __future__ import annotations

from pathlib import Path

import pytest

from mira_server.auth.backends import hash_password
from mira_server.config import (
    ConfigError,
    check_serve_allowed,
    load_config,
    parse_config,
    parse_listen,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "deploy" / "mira-server" / "mira-server.toml"


def test_defaults_are_production_settings() -> None:
    cfg = parse_config({})
    assert cfg.server.listen == "127.0.0.1:8740"
    assert cfg.server.data_dir == Path("/var/lib/mira-server")
    assert cfg.auth.backend == "pam" and cfg.auth.pam_service == "mira-server"
    assert (cfg.auth.users_group, cfg.auth.admins_group) == ("mira-users", "mira-admins")
    assert (cfg.auth.access_token_ttl, cfg.auth.refresh_token_ttl) == (900, 30 * 86400)
    assert cfg.spawner.backend == "sudo"
    assert cfg.spawner.launcher == Path("/usr/local/libexec/mira/engine-launch")
    assert cfg.spawner.idle_timeout == 7200
    assert cfg.dev_only_settings() == []
    assert cfg.db_path == Path("/var/lib/mira-server/mira-server.sqlite3")


def test_shipped_example_is_valid() -> None:
    cfg = load_config(EXAMPLE)
    assert cfg.dev_only_settings() == []


@pytest.mark.parametrize("value,expected", [
    ("127.0.0.1:8740", ("127.0.0.1", 8740)),
    ("[::1]:9000", ("::1", 9000)),
    ("0.0.0.0:80", ("0.0.0.0", 80)),
])
def test_parse_listen(value, expected) -> None:
    assert parse_listen(value) == expected


@pytest.mark.parametrize("value", ["8740", ":8740", "host:", "host:99999", "[::1]8740"])
def test_parse_listen_rejects(value) -> None:
    with pytest.raises(ValueError):
        parse_listen(value)


def test_default_origins_are_file_and_public_url() -> None:
    cfg = parse_config({"server": {"public_url": "https://Mira.Example.com/"}})
    assert cfg.server.effective_origins() == ["file://", "null", "https://mira.example.com"]
    assert parse_config({}).server.effective_origins() == ["file://", "null"]
    explicit = parse_config({"server": {"allowed_origins": ["null", "http://localhost:5173/"]}})
    assert explicit.server.effective_origins() == ["null", "http://localhost:5173"]


@pytest.mark.parametrize("origins", [["*"], ["https://x.example/path"], ["ftp://x.example"]])
def test_bad_origins_rejected(origins) -> None:
    with pytest.raises(ConfigError, match="allowed_origins"):
        parse_config({"server": {"allowed_origins": origins}})


@pytest.mark.parametrize("data,where", [
    ({"server": {"lisen": "x"}}, "server.lisen"),
    ({"auth": {"backend": "ldap"}}, "auth.backend"),
    ({"auth": {"access_token_ttl": 5}}, "auth.access_token_ttl"),
    ({"spawner": {"launcher": "relative/path"}}, "spawner.launcher"),
    ({"auth": {"users_group": "bad group"}}, "auth.users_group"),
    ({"server": {"public_url": "mira.example.com"}}, "server.public_url"),
    ({"server": {"trusted_proxies": ["not-an-ip"]}}, "server.trusted_proxies"),
])
def test_invalid_values_name_the_setting(data, where) -> None:
    with pytest.raises(ConfigError, match=where.replace(".", r"\.")):
        parse_config(data)


def test_dev_backend_needs_users_and_valid_hashes() -> None:
    with pytest.raises(ConfigError, match="dev_users"):
        parse_config({"auth": {"backend": "dev"}})
    with pytest.raises(ConfigError, match="password_hash"):
        parse_config({"auth": {"backend": "dev", "dev_users": [{"name": "a", "password_hash": "plain"}]}})
    cfg = parse_config({"auth": {"backend": "dev",
                                 "dev_users": [{"name": "a", "password_hash": hash_password("x")}]}})
    assert cfg.dev_only_settings() == ["auth.backend = 'dev'"]


def test_serve_refuses_dev_settings_without_flag() -> None:
    cfg = parse_config({"spawner": {"backend": "local"}})
    with pytest.raises(ConfigError, match="--dev"):
        check_serve_allowed(cfg, dev=False)
    check_serve_allowed(cfg, dev=True)
    cfg = parse_config({"spawner": {"verify_listener": False}})
    with pytest.raises(ConfigError, match="verify_listener"):
        check_serve_allowed(cfg, dev=False)


def test_dev_requires_loopback() -> None:
    cfg = parse_config({"server": {"listen": "0.0.0.0:8740"}, "spawner": {"backend": "local"}})
    with pytest.raises(ConfigError, match="loopback"):
        check_serve_allowed(cfg, dev=True)
    check_serve_allowed(parse_config({"server": {"listen": "[::1]:8740"}}), dev=True)
    check_serve_allowed(parse_config({"server": {"listen": "0.0.0.0:8740"}}), dev=False)


def test_load_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("[server\n")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(bad)
