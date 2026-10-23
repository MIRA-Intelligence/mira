"""Exercise account cleanup with mocked processes; never modify host accounts."""

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="the Ubuntu uninstaller requires Bash",
)

UNINSTALLER = Path(__file__).resolve().parents[2] / "deploy/mira-server/uninstall.sh"


@pytest.fixture
def run_cleanup(tmp_path):
    """Provide only fake account/process commands in the shell's PATH."""
    state_path = tmp_path / "state.json"
    calls_path = tmp_path / "calls.jsonl"
    commands = tmp_path / "bin"
    commands.mkdir()
    shim = commands / "fake-command"
    shim.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(r"""
        import json
        import os
        import sys
        from pathlib import Path

        command = Path(sys.argv[0]).name
        args = sys.argv[1:]
        path = Path(os.environ["MOCK_STATE"])
        state = json.loads(path.read_text())
        with open(os.environ["MOCK_CALLS"], "a") as log:
            log.write(json.dumps([command, *args]) + "\n")

        def matches(process):
            selectors = {"-u": "euid", "-U": "ruid"}
            selected = [
                process[attribute] in args[args.index(option) + 1].split(",")
                for option, attribute in selectors.items() if option in args
            ]
            return bool(selected) and all(selected)

        code = 0
        if command == "id":
            code = 0 if state["account_exists"] else 1
        elif command in {"pgrep", "pkill"}:
            found = [p for p in state["processes"] if matches(p)]
            code = 0 if found else 1
            if command == "pkill":
                state["processes"] = [
                    p for p in state["processes"]
                    if not (matches(p) and p.get("killable", True))
                ]
        elif command == "sleep":
            state["sleeps"] += 1
            state["processes"] = [
                p for p in state["processes"]
                if p.get("exit_after_sleep", float("inf")) > state["sleeps"]
            ]
        elif command == "userdel":
            if any(args[0] in (p["ruid"], p["euid"]) for p in state["processes"]):
                print("userdel: account is still used by a process", file=sys.stderr)
                code = 8
            else:
                state["account_exists"] = False
        elif command != "loginctl":
            raise RuntimeError("Unexpected command: " + command)
        path.write_text(json.dumps(state))
        sys.exit(code)
        """),
        encoding="utf-8",
    )
    shim.chmod(0o755)
    for command in ["id", "pgrep", "pkill", "loginctl", "sleep", "userdel"]:
        (commands / command).symlink_to(shim)

    # Load only this function, not the uninstaller's privileged main entrypoint.
    source = UNINSTALLER.read_text(encoding="utf-8")
    start = source.index("remove_account() {\n")
    end = source.index("\n}\n", start) + len("\n}")
    script = (
        'set -euo pipefail\nSERVER_USER=mira-server\nrun() { "$@"; }\n'
        + source[start:end]
        + "\nremove_account\n"
    )

    def run(processes, *, account_exists=True):
        state_path.write_text(
            json.dumps(
                {
                    "account_exists": account_exists,
                    "processes": processes,
                    "sleeps": 0,
                }
            )
        )
        result = subprocess.run(
            [shutil.which("bash"), "--noprofile", "--norc", "-c", script],
            env={
                **os.environ,
                "PATH": str(commands),
                "MOCK_STATE": str(state_path),
                "MOCK_CALLS": str(calls_path),
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        state = json.loads(state_path.read_text())
        calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
        return result, state, calls

    return run


@pytest.mark.parametrize(
    "uids",
    [
        [("mira-server", "root")],  # sudo retains the caller's real UID.
        [("root", "mira-server")],
        [("mira-server", "mira-server")],
        [("mira-server", "root"), ("root", "mira-server")],
    ],
)
def test_cleanup_removes_real_or_effective_uid_processes_only(run_cleanup, uids):
    processes = [
        {"pid": index + 1, "ruid": real, "euid": effective}
        for index, (real, effective) in enumerate(uids)
    ]
    unrelated = {"pid": 100, "ruid": "alice", "euid": "alice"}
    root_process = {"pid": 101, "ruid": "root", "euid": "root"}
    result, state, _ = run_cleanup([*processes, unrelated, root_process])
    assert result.returncode == 0, result.stderr
    assert not state["account_exists"]
    assert state["processes"] == [unrelated, root_process]


def test_cleanup_waits_for_real_uid_process_to_exit_naturally(run_cleanup):
    result, state, calls = run_cleanup(
        [
            {"pid": 1, "ruid": "mira-server", "euid": "root", "exit_after_sleep": 1},
        ]
    )
    assert result.returncode == 0, result.stderr
    assert not state["account_exists"]
    assert state["sleeps"] == 1
    assert not any(call[0] == "pkill" for call in calls)


def test_cleanup_refuses_account_deletion_while_process_survives(run_cleanup):
    process = {"pid": 1, "ruid": "mira-server", "euid": "root", "killable": False}
    result, state, calls = run_cleanup([process])
    assert result.returncode == 8
    assert state["account_exists"]
    assert state["processes"] == [process]
    assert state["sleeps"] == 10
    assert [call for call in calls if call[0] == "userdel"] == [["userdel", "mira-server"]]


@pytest.mark.parametrize("account_exists", [True, False])
def test_cleanup_with_no_service_processes(run_cleanup, account_exists):
    unrelated = {"pid": 100, "ruid": "alice", "euid": "alice"}
    result, state, calls = run_cleanup([unrelated], account_exists=account_exists)
    assert result.returncode == 0, result.stderr
    assert not state["account_exists"]
    assert state["processes"] == [unrelated]
    assert state["sleeps"] == 0
    assert not any(call[0] in {"pkill", "loginctl"} for call in calls)
    if not account_exists:
        assert calls == [["id", "mira-server"]]
