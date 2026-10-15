"""The root-owned engine-launch script and the mira-server CLI."""

from __future__ import annotations

import os
import pwd
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mira_server.cli import app
from mira_server.spawner.launcher import (
    MARKER,
    LauncherInstallError,
    granted_launcher_rules,
    inspect_launcher,
    install_launcher,
    read_engine_python,
    render_launcher,
    sudoers_line,
    sudoers_rules,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX only")

FAKE_ENGINE = """#!/bin/sh
printf '%s\\n' "$0" "$@" > "{out}.argv"
env > "{out}.env"
pwd > "{out}.cwd"
"""


@pytest.fixture
def fake_engine(tmp_path):
    out = tmp_path / "result"
    engine = tmp_path / "engine-bin" / "python"
    engine.parent.mkdir()
    engine.write_text(FAKE_ENGINE.format(out=out))
    engine.chmod(0o755)
    return engine, out


def _run_launcher(path: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    # Run through this interpreter so the test does not depend on the shebang target.
    return subprocess.run([sys.executable, str(path), *args], capture_output=True, text=True,
                          env=env if env is not None else {"PATH": "/usr/bin:/bin"}, timeout=30)


def test_render_and_metadata(tmp_path) -> None:
    script = render_launcher("/opt/mira/bin/python")
    assert script.startswith("#!/opt/mira/bin/python -I\n")
    assert MARKER in script
    path = tmp_path / "engine-launch"
    path.write_text(script)
    assert read_engine_python(path) == "/opt/mira/bin/python"
    compile(script, "engine-launch", "exec")


@pytest.mark.parametrize("bad", ["relative/python", "/opt/my env/python"])
def test_render_rejects_bad_interpreters(bad) -> None:
    with pytest.raises(LauncherInstallError):
        render_launcher(bad)


@pytest.mark.parametrize("action,expected", [
    ("start", ["remote", "attach", "--json", "--timeout", "60"]),
    ("status", ["remote", "status", "--json"]),
    ("stop", ["remote", "stop", "--json"]),
])
def test_launcher_execs_engine_with_clean_env(tmp_path, fake_engine, action, expected) -> None:
    engine, out = fake_engine
    path = install_launcher(tmp_path / "libexec" / "mira" / "engine-launch", str(engine), require_root=False)
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    hostile = {"PATH": "/evil/bin:/usr/bin:/bin", "MIRA_HOME": "/tmp/evil", "PYTHONPATH": "/evil",
               "LD_PRELOAD": "/evil.so", "MIRA_GATEWAY_TOKEN": "x", "HOME": "/tmp/evil"}
    result = _run_launcher(path, action, env=hostile)
    assert result.returncode == 0, result.stderr
    argv = Path(f"{out}.argv").read_text().splitlines()
    assert argv[0] == str(engine)
    assert argv[1:4] == ["-I", "-m", "mira_engine"]
    assert argv[4:] == expected
    env = dict(line.split("=", 1) for line in Path(f"{out}.env").read_text().splitlines() if "=" in line)
    me = pwd.getpwuid(os.geteuid())
    assert env["HOME"] == me.pw_dir
    assert env["MIRA_HOME"] == os.path.join(me.pw_dir, ".mira")
    assert env["USER"] == me.pw_name
    assert env["PATH"].startswith(str(engine.parent) + ":")
    for leaked in ("PYTHONPATH", "LD_PRELOAD", "MIRA_GATEWAY_TOKEN"):
        assert leaked not in env
    assert "/evil" not in env["PATH"]


@pytest.mark.parametrize("args", [[], ["attach"], ["start", "--json"], ["start; rm -rf /"]])
def test_launcher_rejects_other_input(tmp_path, fake_engine, args) -> None:
    engine, out = fake_engine
    path = install_launcher(tmp_path / "engine-launch", str(engine), require_root=False)
    result = _run_launcher(path, *args)
    assert result.returncode == 64
    assert not Path(f"{out}.argv").exists()


def test_install_requires_root(tmp_path) -> None:
    if os.geteuid() == 0:
        pytest.skip("running as root")
    with pytest.raises(LauncherInstallError, match="root"):
        install_launcher(tmp_path / "engine-launch", "/opt/mira/bin/python")


def test_inspect_flags_non_root_owner_and_missing_engine(tmp_path) -> None:
    path = install_launcher(tmp_path / "engine-launch", "/nonexistent/python", require_root=False)
    problems = inspect_launcher(path)
    if os.geteuid() != 0:
        assert any("not root" in p for p in problems)
    assert any("engine interpreter" in p for p in problems)
    assert inspect_launcher(tmp_path / "missing")[0].endswith("run `mira-server install-launcher`")
    link = tmp_path / "link"
    link.symlink_to(path)
    assert "symlink" in inspect_launcher(link)[0]


def test_sudoers_line() -> None:
    assert sudoers_line(Path("/usr/local/libexec/mira/engine-launch"), server_user="mira-server",
                        users_group="mira-users") == \
        "mira-server ALL=(%mira-users) NOPASSWD: /usr/local/libexec/mira/engine-launch"


def test_sudoers_rules_add_stop_for_removed_users() -> None:
    assert sudoers_rules(Path("/usr/local/libexec/mira/engine-launch"), server_user="mira-server",
                         users_group="mira-users") == [
        "mira-server ALL=(%mira-users) NOPASSWD: /usr/local/libexec/mira/engine-launch",
        "mira-server ALL=(ALL,!root) NOPASSWD: /usr/local/libexec/mira/engine-launch stop",
    ]


LAUNCHER = Path("/usr/local/libexec/mira/engine-launch")
SUDO_L_HEADER = ("Matching Defaults entries for mira-server on host:\n    env_reset, secure_path=/usr/bin\n\n"
                 "User mira-server may run the following commands on host:\n")


@pytest.mark.parametrize(("rules", "expected"), [
    ("", (False, False)),
    ("    (%mira-users) NOPASSWD: /usr/local/libexec/mira/engine-launch\n", (True, False)),
    ("    (%mira-users) NOPASSWD: /usr/local/libexec/mira/engine-launch\n"
     "    (ALL, !root) NOPASSWD: /usr/local/libexec/mira/engine-launch stop\n", (True, True)),
    # A password-protected rule is useless to `sudo -n`.
    ("    (%mira-users) /usr/local/libexec/mira/engine-launch\n", (False, False)),
    # Another launcher path, or stop limited to the group, does not count.
    ("    (%mira-users) NOPASSWD: /opt/other/engine-launch\n", (False, False)),
    ("    (%mira-users) NOPASSWD: /usr/local/libexec/mira/engine-launch stop\n", (False, False)),
    ("    (ALL : ALL) NOPASSWD: /usr/bin/true, /usr/local/libexec/mira/engine-launch\n", (True, True)),
])
def test_granted_launcher_rules(rules: str, expected: tuple[bool, bool]) -> None:
    assert granted_launcher_rules(SUDO_L_HEADER + rules, LAUNCHER) == expected


# ── CLI ──────────────────────────────────────────────────────────────


def test_cli_hash_password() -> None:
    result = CliRunner().invoke(app, ["hash-password", "--stdin"], input="pw\n")
    assert result.exit_code == 0
    assert result.stdout.strip().startswith("scrypt$")


def test_cli_install_launcher(tmp_path) -> None:
    result = CliRunner().invoke(app, ["install-launcher", "--prefix", str(tmp_path), "--engine-python",
                                      "/opt/mira/bin/python", "--no-root-check"])
    assert result.exit_code == 0, result.output
    path = tmp_path / "libexec" / "mira" / "engine-launch"
    assert path.exists()
    assert f"mira-server ALL=(%mira-users) NOPASSWD: {path}" in result.output
    assert f"mira-server ALL=(ALL,!root) NOPASSWD: {path} stop" in result.output


def test_cli_serve_refuses_dev_settings(tmp_path) -> None:
    config = tmp_path / "mira-server.toml"
    config.write_text(f'[server]\ndata_dir = "{tmp_path}/data"\n[spawner]\nbackend = "local"\n')
    result = CliRunner().invoke(app, ["serve", "--config", str(config)])
    assert result.exit_code == 2
    assert "--dev" in result.output
    config.write_text(f'[server]\nlisten = "0.0.0.0:8740"\ndata_dir = "{tmp_path}/data"\n'
                      '[spawner]\nbackend = "local"\n')
    result = CliRunner().invoke(app, ["serve", "--config", str(config), "--dev"])
    assert result.exit_code == 2
    assert "loopback" in result.output


def test_cli_check_reports_problems(tmp_path) -> None:
    config = tmp_path / "mira-server.toml"
    config.write_text(f'[server]\ndata_dir = "{tmp_path}/data"\n[spawner]\nlauncher = "{tmp_path}/none"\n'
                      f'sudo = "{tmp_path}/no-sudo"\n[auth]\npam_service = "mira-server-test-missing"\n')
    result = CliRunner().invoke(app, ["check", "--config", str(config)])
    assert result.exit_code == 1
    assert "FAIL" in result.output and "install-launcher" in result.output


def test_cli_check_invalid_config(tmp_path) -> None:
    config = tmp_path / "mira-server.toml"
    config.write_text("[auth]\nbackend = 'ldap'\n")
    result = CliRunner().invoke(app, ["check", "--config", str(config)])
    assert result.exit_code == 1
    assert "auth.backend" in result.output


def _policy_report(capsys, text: str | None) -> tuple[int, str]:
    from mira_engine.security import host_policy
    from mira_server.cli import _check_host_policy, _Report

    if text is not None:
        host_policy.POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
        host_policy.POLICY_PATH.write_text(text)
    report = _Report()
    _check_host_policy(report)
    return report.errors, capsys.readouterr().out


def test_check_warns_without_a_host_policy(capsys) -> None:
    errors, output = _policy_report(capsys, None)
    assert errors == 0 and "no host policy" in output


def test_check_fails_on_an_invalid_host_policy(capsys) -> None:
    errors, output = _policy_report(capsys, "[tools]\nexec = 'no'\n")
    assert errors == 1 and "engines will not start" in output


def test_check_reports_what_the_policy_enforces(capsys, monkeypatch) -> None:
    from mira_engine.agent.tools import sandbox

    monkeypatch.setattr(sandbox, "sandbox_available", lambda network=True: (False, "bwrap missing"))
    errors, output = _policy_report(capsys, '[tools]\nrestrict_to_workspace = true\n[sandbox]\nmode = "bwrap"\n')
    assert "enforces: tools.restrictToWorkspace, tools.exec.sandbox" in output
    assert errors == 1 and "bwrap missing" in output
