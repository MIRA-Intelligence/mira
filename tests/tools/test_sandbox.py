"""Tests for mira_engine.agent.tools.sandbox (bwrap)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mira_engine.agent.tools import sandbox
from mira_engine.agent.tools.sandbox import SandboxSpec, bwrap_argv, sandbox_available


def _pairs(args: list[str], flag: str) -> list[tuple[str, str]]:
    return [(args[i + 1], args[i + 2]) for i, token in enumerate(args) if token == flag]


def _mira_home() -> Path:
    return Path(os.environ["MIRA_HOME"]).resolve()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "projects" / "demo"
    path.mkdir(parents=True)
    return path.resolve()


class TestBwrapArgv:
    def test_structure(self, workspace: Path) -> None:
        args = bwrap_argv(SandboxSpec.build(workspace))
        assert args[0] == "bwrap" and args[-1] == "--"
        for flag in ("--new-session", "--die-with-parent", "--unshare-pid", "--unshare-ipc"):
            assert flag in args
        assert "--unshare-net" not in args
        assert ("/usr", "/usr") in _pairs(args, "--ro-bind-try")
        assert ("/etc", "/etc") in _pairs(args, "--ro-bind-try")

    def test_network_none(self, workspace: Path) -> None:
        assert "--unshare-net" in bwrap_argv(SandboxSpec.build(workspace, network="none"))

    def test_workspace_is_the_last_writable_mount(self, workspace: Path) -> None:
        args = bwrap_argv(SandboxSpec.build(workspace))
        binds = _pairs(args, "--bind")
        assert binds[-1] == (str(workspace), str(workspace))
        assert args[args.index("--chdir") + 1] == str(workspace)

    def test_home_is_backed_by_the_sandbox_home(self, workspace: Path) -> None:
        args = bwrap_argv(SandboxSpec.build(workspace))
        home = os.environ.get("HOME") or str(Path.home())
        sandbox_home = _mira_home() / "sandbox" / "home"
        assert (str(sandbox_home), home) in _pairs(args, "--bind")
        assert sandbox_home.is_dir()
        assert sandbox_home.stat().st_mode & 0o777 == 0o700

    def test_mira_home_is_masked_after_extra_paths(self, workspace: Path, tmp_path: Path) -> None:
        extra = tmp_path / "conda"
        extra.mkdir()
        args = bwrap_argv(SandboxSpec.build(workspace, read_only_paths=[str(extra), str(_mira_home())]))
        mask = args.index("--tmpfs", args.index(str(extra)))
        assert args[mask + 1] == str(_mira_home())
        # The mask comes after every user/admin path.
        assert args.index(str(extra)) < mask

    def test_workspace_containing_mira_home_masks_again(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        mira_home = home / ".mira"
        mira_home.mkdir(parents=True)
        monkeypatch.setenv("MIRA_HOME", str(mira_home))
        workspace = str(home.resolve())
        args = bwrap_argv(SandboxSpec.build(home))
        workspace_bind = max(i for i, token in enumerate(args)
                             if token == "--bind" and args[i + 1] == workspace and args[i + 2] == workspace)
        masks = [i for i, token in enumerate(args) if token == "--tmpfs" and args[i + 1] == str(mira_home.resolve())]
        assert len(masks) == 2 and masks[-1] > workspace_bind

    def test_cwd_outside_workspace_falls_back(self, workspace: Path, tmp_path: Path) -> None:
        args = bwrap_argv(SandboxSpec.build(workspace), tmp_path)
        assert args[args.index("--chdir") + 1] == str(workspace)

    def test_cwd_inside_workspace(self, workspace: Path) -> None:
        sub = workspace / "src"
        sub.mkdir()
        args = bwrap_argv(SandboxSpec.build(workspace), sub)
        assert args[args.index("--chdir") + 1] == str(sub)

    def test_unavailable_off_linux(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        sandbox.reset_probe_cache()
        ok, reason = sandbox_available()
        sandbox.reset_probe_cache()
        assert not ok and "Linux" in reason


# ── real bwrap (Linux CI installs bubblewrap) ─────────────────────────

_REAL = sys.platform.startswith("linux") and shutil.which("bwrap") is not None


def _run(spec: SandboxSpec, script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    argv = bwrap_argv(spec) + ["bash", "-c", script]
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env, check=False)


@pytest.mark.skipif(not _REAL, reason="needs Linux with bubblewrap")
class TestRealSandbox:
    @pytest.fixture(autouse=True)
    def _require_working_bwrap(self) -> None:
        sandbox.reset_probe_cache()
        ok, reason = sandbox_available()
        if not ok:
            if os.environ.get("MIRA_REQUIRE_BWRAP"):
                pytest.fail(f"bwrap unavailable: {reason}")
            pytest.skip(reason)

    def test_workspace_writable_and_secrets_hidden(self, workspace: Path) -> None:
        mira_home = _mira_home()
        (mira_home / "runtime").mkdir(parents=True, exist_ok=True)
        (mira_home / "config.json").write_text('{"secret": "sk-test"}')
        (mira_home / "runtime" / "gateway-token").write_text("tok-test")
        result = _run(SandboxSpec.build(workspace),
                      f"echo hi > out.txt && cat {mira_home}/config.json; "
                      f"cat {mira_home}/runtime/gateway-token; ls -A {mira_home}; echo done")
        assert result.returncode == 0, result.stderr
        assert (workspace / "out.txt").read_text() == "hi\n"
        assert "sk-test" not in result.stdout and "tok-test" not in result.stdout
        assert result.stdout.strip().endswith("done")

    def test_other_directories_are_invisible(self, workspace: Path, tmp_path: Path) -> None:
        private = tmp_path / "private"
        private.mkdir()
        (private / "notes.txt").write_text("private")
        result = _run(SandboxSpec.build(workspace), f"cat {private}/notes.txt")
        assert result.returncode != 0
        assert "private" not in result.stdout

    def test_read_only_paths_cannot_be_written(self, workspace: Path, tmp_path: Path) -> None:
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "data.txt").write_text("data")
        result = _run(SandboxSpec.build(workspace, read_only_paths=[str(shared)]),
                      f"cat {shared}/data.txt && ! touch {shared}/new 2>/dev/null")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "data"
        assert not (shared / "new").exists()

    def test_own_pid_namespace(self, workspace: Path) -> None:
        result = _run(SandboxSpec.build(workspace), "ls /proc | grep -c '^[0-9]'")
        assert result.returncode == 0, result.stderr
        assert int(result.stdout.strip()) < 10

    def test_network_none_has_only_loopback(self, workspace: Path) -> None:
        if not sandbox_available(network=False)[0]:
            pytest.skip("bwrap cannot create a network namespace here")
        result = _run(SandboxSpec.build(workspace, network="none"), "cat /proc/net/dev")
        assert result.returncode == 0, result.stderr
        interfaces = {line.split(":")[0].strip() for line in result.stdout.splitlines()[2:]}
        assert interfaces == {"lo"}

    def test_home_persists_between_commands(self, workspace: Path) -> None:
        spec = SandboxSpec.build(workspace)
        assert _run(spec, "mkdir -p ~/.cache/demo && echo kept > ~/.cache/demo/x").returncode == 0
        result = _run(spec, "cat ~/.cache/demo/x")
        assert result.stdout == "kept\n"
