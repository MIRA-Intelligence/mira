"""Host policy (/etc/mira/policy.toml) parsing and enforcement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mira_engine.config import loader
from mira_engine.config.schema import Config, MCPServerConfig
from mira_engine.config.ui_runtime import apply_ui_runtime_update
from mira_engine.security import host_policy
from mira_engine.security.host_policy import (
    HostPolicyError,
    PolicyLockedError,
    enforce,
    load_host_policy,
    parse_policy,
)

POLICY = Path("/etc/mira/policy.toml")


def write_policy(text: str) -> Path:
    path = host_policy.POLICY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    host_policy.reset_cache()
    return path


def test_no_file_means_no_policy() -> None:
    policy = load_host_policy()
    assert not policy.active
    assert policy.locked_settings() == []


def test_parse_full_policy() -> None:
    policy = parse_policy("""
[tools]
restrict_to_workspace = true
exec = false
mcp = false
plugin_install = false

[sandbox]
mode = "bwrap"
network = "none"
read_only_paths = ["/data/ref", "/data/ref/"]
read_write_paths = ["/scratch"]
""", POLICY, groups=set())
    assert policy.restrict_to_workspace and policy.exec_disabled and policy.mcp_disabled
    assert policy.plugin_install_disabled
    assert policy.sandbox == "bwrap" and policy.network_disabled
    assert policy.read_only_paths == ("/data/ref",)
    assert policy.read_write_paths == ("/scratch",)
    assert policy.locked_settings() == [
        "tools.restrictToWorkspace", "tools.exec.enable", "tools.exec.sandbox",
        "tools.exec.sandboxNetwork", "tools.mcpServers",
    ]


def test_permissive_values_enforce_nothing() -> None:
    policy = parse_policy("""
[tools]
restrict_to_workspace = false
exec = true
[sandbox]
mode = "off"
network = "full"
""", POLICY, groups=set())
    assert policy.locked_settings() == []


def test_group_entries_apply_in_order_and_can_relax() -> None:
    text = """
[tools]
exec = false
[sandbox]
mode = "bwrap"
network = "none"
read_only_paths = ["/base"]

[[groups]]
name = "trusted"
tools.exec = true
sandbox.network = "full"
sandbox.read_only_paths = ["/trusted"]

[[groups]]
name = "interns"
tools.exec = false
"""
    trusted = parse_policy(text, POLICY, groups={"trusted"})
    assert trusted.groups == ("trusted",)
    assert not trusted.exec_disabled and not trusted.network_disabled
    assert trusted.sandbox == "bwrap"
    assert trusted.read_only_paths == ("/base", "/trusted")
    both = parse_policy(text, POLICY, groups={"trusted", "interns"})
    assert both.exec_disabled
    nobody = parse_policy(text, POLICY, groups=set())
    assert nobody.exec_disabled and nobody.network_disabled


@pytest.mark.parametrize("text, message", [
    ("[tools]\nexec = 'no'\n", "tools.exec must be true or false"),
    ("[tools]\nshell = false\n", "unknown key(s) in [tools]: shell"),
    ("[sandbox]\nmode = 'docker'\n", "sandbox.mode"),
    ("[sandbox]\nread_only_paths = ['relative']\n", "not an absolute path"),
    ("[limits]\n", "unknown section(s): limits"),
    ("[[groups]]\ntools.exec = false\n", "groups[0].name"),
    ("[[groups]]\nname = 'x'\nsandbox.network = 'some'\n", "sandbox.network"),
    ("not toml = = 1", "policy.toml"),
])
def test_invalid_policy_is_an_error(text: str, message: str) -> None:
    with pytest.raises(HostPolicyError, match=message.replace("(", r"\(").replace(")", r"\)").replace("[", r"\[")):
        parse_policy(text, POLICY, groups=set())


def test_invalid_policy_file_stops_config_loading() -> None:
    write_policy("[tools]\nexec = 1\n")
    with pytest.raises(HostPolicyError):
        loader.load_config()


def test_policy_file_is_reread_when_it_changes() -> None:
    path = write_policy("[tools]\nexec = false\n")
    assert load_host_policy().exec_disabled
    path.write_text("[tools]\nexec = true\n")
    import os
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 5))
    assert not load_host_policy().exec_disabled


def test_enforce_and_save_keep_the_users_own_values(tmp_path: Path) -> None:
    write_policy("""
[tools]
restrict_to_workspace = true
mcp = false
[sandbox]
mode = "bwrap"
network = "none"
""")
    config_path = tmp_path / "config.json"
    user = Config()
    user.tools.mcp_servers = {"files": MCPServerConfig(command="npx", args=["server"])}
    user.agents.defaults.max_tool_iterations = 7
    config_path.write_text(json.dumps(user.model_dump(by_alias=True)))

    cfg = loader.load_config(config_path)
    assert cfg.tools.restrict_to_workspace is True
    assert cfg.tools.exec.sandbox == "bwrap"
    assert cfg.tools.exec.sandbox_network == "none"
    assert cfg.tools.mcp_servers == {}

    cfg.agents.defaults.max_tool_iterations = 9
    loader.save_config(cfg, config_path)
    saved = json.loads(config_path.read_text())
    assert saved["tools"]["restrictToWorkspace"] is False
    assert saved["tools"]["exec"]["sandbox"] == ""
    assert saved["tools"]["exec"]["sandboxNetwork"] == "full"
    assert saved["tools"]["mcpServers"]["files"]["command"] == "npx"
    assert saved["agents"]["defaults"]["maxToolIterations"] == 9


def test_enforce_is_idempotent() -> None:
    write_policy("[tools]\nexec = false\n")
    cfg = Config()
    enforce(cfg)
    enforce(cfg)
    assert cfg.tools.exec.enable is False
    assert cfg._policy_user_values[("tools", "exec", "enable")] is True


def test_runtime_api_cannot_turn_off_an_enforced_setting(tmp_path: Path) -> None:
    write_policy("[tools]\nrestrict_to_workspace = true\n")
    cfg = loader.load_config(tmp_path / "missing.json")
    with pytest.raises(PolicyLockedError, match="tools.restrictToWorkspace"):
        apply_ui_runtime_update(cfg, {"runtime": {"restrict_to_workspace": False}},
                                current_projects_root=tmp_path)
    # Sending the enforced value back (the UI posts whole forms) is fine.
    apply_ui_runtime_update(cfg, {"runtime": {"restrict_to_workspace": True}}, current_projects_root=tmp_path)
