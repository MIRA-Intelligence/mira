"""The Windows service's APPDATA/LOCALAPPDATA repair and the gh login hint."""

import pytest

from mira_engine.utils.service_env import ensure_user_profile_dirs, gh_login_hint, profile_dirs

USER = r"C:\Users\ryu"
SYSTEM_ROAMING = r"C:\Windows\system32\config\systemprofile\AppData\Roaming"
SYSTEM_LOCAL = r"C:\Windows\system32\config\systemprofile\AppData\Local"


def _service_env(**extra):
    return {"MIRA_SERVICE_MODE": "windows-service", "USERPROFILE": USER, **extra}


def test_profile_dirs_follow_the_windows_layout():
    assert profile_dirs(USER) == {
        "APPDATA": r"C:\Users\ryu\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\ryu\AppData\Local",
    }


def test_localsystem_profile_dirs_are_redirected_to_the_user():
    # WinSW runs the engine as LocalSystem with only HOME/USERPROFILE pointed
    # at the user; gh reads %APPDATA%\GitHub CLI and saw the system profile.
    env = _service_env(APPDATA=SYSTEM_ROAMING, LOCALAPPDATA=SYSTEM_LOCAL)

    changed = ensure_user_profile_dirs(env, platform="win32")

    assert changed == {
        "APPDATA": r"C:\Users\ryu\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\ryu\AppData\Local",
    }
    assert env["APPDATA"] == r"C:\Users\ryu\AppData\Roaming"
    assert env["LOCALAPPDATA"] == r"C:\Users\ryu\AppData\Local"


def test_missing_variables_are_filled_in():
    env = _service_env()

    assert set(ensure_user_profile_dirs(env, platform="win32")) == {"APPDATA", "LOCALAPPDATA"}


def test_variables_already_under_the_profile_are_left_alone():
    # A service running as the user, or a redirected folder inside the profile.
    env = _service_env(
        APPDATA=r"c:\users\RYU\AppData\Roaming", LOCALAPPDATA=r"C:\Users\ryu\Data\Local"
    )

    assert ensure_user_profile_dirs(env, platform="win32") == {}
    assert env["APPDATA"] == r"c:\users\RYU\AppData\Roaming"


def test_a_profile_prefix_that_is_not_the_profile_is_not_mistaken_for_it():
    env = _service_env(
        APPDATA=r"C:\Users\ryu2\AppData\Roaming", LOCALAPPDATA=r"C:\Users\ryu\AppData\Local"
    )

    assert ensure_user_profile_dirs(env, platform="win32") == {
        "APPDATA": r"C:\Users\ryu\AppData\Roaming"
    }


@pytest.mark.parametrize(
    "platform, mode",
    [
        ("darwin", "launchd"),
        ("linux", "systemd-user"),
        ("win32", "windows-background"),
        ("win32", ""),
    ],
)
def test_nothing_happens_outside_windows_service_mode(platform, mode):
    env = {"MIRA_SERVICE_MODE": mode, "USERPROFILE": USER, "APPDATA": SYSTEM_ROAMING}

    assert ensure_user_profile_dirs(env, platform=platform) == {}
    assert env["APPDATA"] == SYSTEM_ROAMING
    assert gh_login_hint(platform=platform, environ=env) == ""


def test_nothing_happens_without_a_user_profile():
    assert (
        ensure_user_profile_dirs({"MIRA_SERVICE_MODE": "windows-service"}, platform="win32") == {}
    )


def test_service_mode_argument_overrides_the_environment():
    env = {"USERPROFILE": USER, "APPDATA": SYSTEM_ROAMING}

    assert "APPDATA" in ensure_user_profile_dirs(
        env, platform="win32", service_mode="windows-service"
    )


def test_gh_login_hint_explains_insecure_storage_for_the_windows_service():
    hint = gh_login_hint(platform="win32", environ=_service_env())

    assert "LocalSystem" in hint
    assert "gh auth login --insecure-storage" in hint and "gh auth setup-git" in hint
    assert r"%APPDATA%\GitHub CLI\hosts.yml" in hint
