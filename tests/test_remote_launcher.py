"""The ``<MIRA_HOME>/bin/mira`` launcher and ``mira remote setup`` (#173)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mira_engine import __version__
from mira_engine.cli import launcher, remote
from mira_engine.cli.commands import app

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the launcher is POSIX only")


@pytest.fixture
def path() -> Path:
    return Path(os.environ["MIRA_HOME"]).resolve() / "bin" / "mira"


@pytest.fixture
def installed(monkeypatch):
    """Treat this interpreter as a pip install, whatever the test environment is."""
    monkeypatch.setattr(launcher, "_installed_in_interpreter", lambda: True)


def _fake_python(tmp_path: Path, name: str = "python") -> str:
    exe = tmp_path / "other-env" / "bin" / name
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    return str(exe)


def _invoke(*args: str):
    result = CliRunner().invoke(app, ["remote", *args])
    lines = result.stdout.splitlines()
    assert len(lines) == 1, f"stdout must be exactly one JSON line: {result.stdout!r}"
    return result.exit_code, json.loads(lines[0])


# ── content ──────────────────────────────────────────────────────────


def test_render_runs_the_interpreter_with_the_package():
    text = launcher.render("/opt/env/bin/python")
    lines = text.splitlines()
    assert lines[0] == "#!/bin/sh"
    assert launcher.MARKER in lines
    assert lines[-1] == """exec '/opt/env/bin/python' -m mira_engine "$@\""""
    assert text.endswith("\n")


@pytest.mark.parametrize("exe", ["/opt/my env/bin/python", "/opt/it's/bin/python", '/opt/a"b $HOME`x`/python'])
def test_render_quotes_awkward_paths(tmp_path, exe):
    rendered = launcher.render(exe)
    # With `exec` swapped for printf, sh must print the path as one literal word.
    probe = _write(tmp_path / "probe", rendered.replace("exec ", "printf '%s\\n' "))
    out = subprocess.run(["sh", str(probe), "x y"], capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == [exe, "-m", "mira_engine", "x y"]
    assert launcher.inspect(_write(tmp_path / "mira", rendered)).target == exe


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_setup_writes_mode_0755_launcher_for_this_interpreter(path):
    assert launcher.setup() == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert path.read_text(encoding="utf-8") == launcher.render(sys.executable)
    state = launcher.inspect(path)
    assert state.generated and state.target == sys.executable and state.target_ok


def test_write_is_atomic(tmp_path, monkeypatch):
    target = _write(tmp_path / "mira", "old")
    seen: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append((str(Path(src).parent), Path(src).read_text()))
        real_replace(src, dst)

    monkeypatch.setattr(launcher.os, "replace", spy)
    launcher.write(target, "new")
    assert seen == [(str(tmp_path), "new")]
    assert target.read_text() == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["mira"]


def test_failed_write_leaves_the_old_file_and_no_temp(tmp_path, monkeypatch):
    target = _write(tmp_path / "mira", "old")

    def boom(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(launcher.os, "replace", boom)
    with pytest.raises(OSError):
        launcher.write(target, "new")
    assert target.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["mira"]


# ── auto-maintenance ─────────────────────────────────────────────────


def test_ensure_creates_a_missing_launcher(installed, path):
    launcher.ensure_launcher()
    assert launcher.inspect(path).target == sys.executable
    assert stat.S_IMODE(path.stat().st_mode) == 0o755


def test_ensure_keeps_a_valid_launcher_pointing_elsewhere(installed, path, tmp_path):
    other = _fake_python(tmp_path)
    launcher.write(path, launcher.render(other))
    launcher.ensure_launcher()
    assert launcher.inspect(path).target == other


@pytest.mark.parametrize("make_stale", ["missing", "not-executable"])
def test_ensure_rewrites_a_launcher_whose_interpreter_is_gone(installed, path, tmp_path, make_stale):
    other = _fake_python(tmp_path)
    launcher.write(path, launcher.render(other))
    if make_stale == "missing":
        os.unlink(other)
    else:
        os.chmod(other, 0o644)
    launcher.ensure_launcher()
    assert launcher.inspect(path).target == sys.executable


def test_ensure_never_touches_a_users_own_file(installed, path, tmp_path):
    path.parent.mkdir(parents=True)
    _write(path, "#!/bin/sh\nexec /gone/python -m mira_engine \"$@\"\n")
    launcher.ensure_launcher()
    assert "/gone/python" in path.read_text()


def test_ensure_never_replaces_a_symlink(installed, path, tmp_path):
    path.parent.mkdir(parents=True)
    path.symlink_to(tmp_path / "dangling")
    launcher.ensure_launcher()
    assert path.is_symlink()


@pytest.mark.parametrize("value", ["1", "true"])
def test_ensure_respects_the_opt_out(installed, path, monkeypatch, value):
    monkeypatch.setenv(launcher.OPT_OUT_ENV, value)
    launcher.ensure_launcher()
    assert not path.exists()


def test_ensure_skips_frozen_builds(installed, path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    launcher.ensure_launcher()
    assert not path.exists()


def test_ensure_is_a_noop_on_windows(installed, path, monkeypatch):
    monkeypatch.setattr(launcher, "_IS_WINDOWS", True)
    launcher.ensure_launcher()
    assert not path.exists()


def test_ensure_skips_when_not_installed_in_this_interpreter(path, monkeypatch):
    monkeypatch.setattr(launcher, "_installed_in_interpreter", lambda: False)
    launcher.ensure_launcher()
    assert not path.exists()


def test_ensure_never_raises(installed, monkeypatch):
    def boom(*_args):
        raise PermissionError("read-only home")

    monkeypatch.setattr(launcher, "write", boom)
    launcher.ensure_launcher()


def test_app_bundle_interpreters_are_not_installs(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/Applications/Mira.app/Contents/Resources/python/bin/python3")
    assert launcher._installed_in_interpreter() is False


def test_every_cli_invocation_ensures_the_launcher_without_stdout_noise(installed, path, monkeypatch):
    monkeypatch.setattr(remote, "attach", lambda _timeout: {"protocol": 1, "ok": True, "port": 1})
    code, payload = _invoke("attach", "--json")
    assert code == 0 and payload["port"] == 1
    assert launcher.inspect(path).target == sys.executable


# ── mira remote setup ────────────────────────────────────────────────


def test_setup_json_contract(path, tmp_path):
    launcher.write(path, launcher.render(_fake_python(tmp_path)))  # setup switches installs
    code, payload = _invoke("setup", "--json")
    assert code == 0
    assert payload == {"protocol": 1, "ok": True, "launcher": str(path),
                       "executable": sys.executable, "version": __version__}
    assert launcher.inspect(path).target == sys.executable


def test_setup_human_output_mentions_the_desktop_app(path):
    result = CliRunner().invoke(app, ["remote", "setup"])
    assert result.exit_code == 0
    assert str(path) in result.stdout and "desktop app" in result.stdout


def test_setup_refuses_to_overwrite_a_users_file(path):
    path.parent.mkdir(parents=True)
    _write(path, "#!/bin/sh\necho mine\n")
    code, payload = _invoke("setup", "--json")
    assert code == 1
    assert payload["ok"] is False and payload["protocol"] == 1
    assert payload["error"]["code"] == "launcher_conflict"
    assert path.read_text() == "#!/bin/sh\necho mine\n"


@pytest.mark.parametrize(("attr", "code"), [("_IS_WINDOWS", "unsupported_platform"), ("frozen", "unsupported")])
def test_setup_reports_unsupported_builds(path, monkeypatch, attr, code):
    if attr == "frozen":
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    else:
        monkeypatch.setattr(launcher, attr, True)
    exit_code, payload = _invoke("setup", "--json")
    assert exit_code == 1 and payload["error"]["code"] == code
    assert not path.exists()


# ── end to end ───────────────────────────────────────────────────────


@pytest.mark.skipif(not launcher._installed_in_interpreter(),
                    reason="mira-engine is not pip-installed in this interpreter")
def test_generated_launcher_runs_the_cli_without_activation(path, tmp_path):
    """`python -m mira_engine` is the `mira` CLI, reachable with a bare PATH."""
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin",
           "MIRA_HOME": os.environ["MIRA_HOME"], "MIRA_RUNTIME_DIR": os.environ["MIRA_RUNTIME_DIR"]}
    setup = subprocess.run([sys.executable, "-m", "mira_engine", "remote", "setup", "--json"],
                           env=env, capture_output=True, text=True, timeout=120)
    assert setup.returncode == 0, setup.stderr
    assert json.loads(setup.stdout)["launcher"] == str(path)

    version = subprocess.run([str(path), "--version"], env=env, capture_output=True, text=True, timeout=120)
    assert version.returncode == 0 and __version__ in version.stdout

    status = subprocess.run([str(path), "remote", "status", "--json"],
                            env=env, capture_output=True, text=True, timeout=120)
    assert status.returncode == 0, status.stderr
    lines = status.stdout.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["running"] is False
