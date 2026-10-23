"""The service PATH repair and executable lookup (desktop bundle: `gh` not found)."""

import os
import stat
import sys
from pathlib import Path

import pytest

import mira_engine.utils.exec_path as exec_path
from mira_engine.utils.exec_path import (
    ensure_service_path,
    extend_path,
    extended_environ,
    missing_executable_message,
    resolve_executable,
    well_known_bin_dirs,
)

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX executables and path separators")


@pytest.fixture
def candidates(tmp_path, monkeypatch):
    """Two controlled candidate directories: `present` exists, `absent` does not.

    The real per-platform list includes directories that exist on most
    machines (/usr/local/bin), which would make the behaviour tests depend
    on the host.
    """
    present = tmp_path / "pkg" / "bin"
    present.mkdir(parents=True)
    absent = tmp_path / "nowhere" / "bin"
    monkeypatch.setattr(exec_path, "well_known_bin_dirs", lambda **_: [present, absent])
    return present, absent


def test_well_known_dirs_cover_the_package_managers_per_platform(tmp_path):
    mac = well_known_bin_dirs(platform="darwin", home=tmp_path)
    assert Path("/opt/homebrew/bin") in mac and Path("/usr/local/bin") in mac
    assert tmp_path / ".local" / "bin" in mac

    linux = well_known_bin_dirs(platform="linux", home=tmp_path)
    assert Path("/home/linuxbrew/.linuxbrew/bin") in linux and Path("/snap/bin") in linux
    assert Path("/opt/homebrew/bin") not in linux

    roots = {"ProgramFiles": r"C:\Program Files", "LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
    win = [str(p) for p in well_known_bin_dirs(platform="win32", home=tmp_path, environ=roots)]
    assert os.path.join(r"C:\Program Files", "GitHub CLI") in win
    assert os.path.join(r"C:\Users\u\AppData\Local", "Microsoft", "WinGet", "Links") in win
    # An unset Windows root (ProgramData here) contributes nothing.
    assert not any("chocolatey" in p for p in win)


@POSIX
def test_extend_path_appends_only_existing_new_directories_after_the_current_ones(
    candidates, tmp_path
):
    present, _absent = candidates
    already = tmp_path / "already"
    already.mkdir()
    environ = {"PATH": f"/usr/bin:{already}"}

    path, added = extend_path(environ, platform="linux", home=tmp_path)

    assert added == [str(present)]
    assert path.split(os.pathsep) == ["/usr/bin", str(already), str(present)]


@POSIX
def test_extend_path_does_not_duplicate_a_directory_already_on_path(candidates, tmp_path):
    present, _absent = candidates
    environ = {"PATH": f"{present}/"}  # same directory, different spelling

    path, added = extend_path(environ, platform="linux", home=tmp_path)

    assert added == []
    assert path == f"{present}/"


@POSIX
def test_ensure_service_path_mutates_the_environment_only_when_something_was_added(
    candidates, tmp_path
):
    present, _absent = candidates
    environ = {"PATH": "/usr/bin"}

    assert ensure_service_path(environ, platform="linux", home=tmp_path) == [str(present)]
    assert environ["PATH"] == f"/usr/bin:{present}"

    untouched = {"PATH": f"/usr/bin:{present}"}
    assert ensure_service_path(untouched, platform="linux", home=tmp_path) == []
    assert untouched == {"PATH": f"/usr/bin:{present}"}


@POSIX
def test_resolve_executable_finds_a_program_the_bare_path_cannot_see(candidates, tmp_path):
    present, _absent = candidates
    gh = present / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    environ = {"PATH": "/usr/bin:/bin"}

    assert resolve_executable("gh", environ, platform="linux", home=tmp_path) == str(gh)
    assert resolve_executable("gh-missing", environ, platform="linux", home=tmp_path) is None

    # Children of that program (gh runs git) see the same directories.
    env = extended_environ(environ, platform="linux", home=tmp_path)
    assert env["PATH"] == f"/usr/bin:/bin:{present}"


@POSIX
def test_missing_executable_message_names_the_program_and_what_was_searched(candidates, tmp_path):
    present, absent = candidates
    msg = missing_executable_message("gh", {"PATH": "/usr/bin"}, platform="linux", home=tmp_path)

    assert msg.startswith("`gh` is not installed")
    assert "/usr/bin" in msg and str(present) in msg and str(absent) not in msg
    assert "background service" in msg and "gh auth login" in msg
    assert "Install `foo`" in missing_executable_message(
        "foo", {"PATH": ""}, platform="linux", home=tmp_path
    )
