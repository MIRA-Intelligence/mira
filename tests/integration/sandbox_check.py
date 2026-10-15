#!/usr/bin/env python3
"""Agent isolation check, run as a real host user by the Mira Server CI job (#153).

The job installs deploy/mira-server/policy.toml as /etc/mira/policy.toml and
runs this file as one user (``sudo -u bob -H``) while another user's engine is
running. It checks that the host policy reaches the engine config, that shell
commands run in the bwrap sandbox, and that inside it the user's own Mira home
(config, gateway token), other users' homes and other users' engines (without
their token) are out of reach.

Not collected by pytest (the file name does not start with ``test_``).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"    ok: {message}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--other-home", required=True)
    parser.add_argument("--other-port", type=int, required=True)
    args = parser.parse_args()

    from mira_engine.agent.tools.sandbox import SandboxSpec, bwrap_argv, sandbox_available
    from mira_engine.agent.tools.shell import ExecTool
    from mira_engine.config.loader import get_home_dir, load_config

    config = load_config()
    check(config.tools.exec.sandbox == "bwrap", "the host policy forces the bwrap sandbox")
    check(config.tools.restrict_to_workspace, "the host policy forces restrict_to_workspace")
    ok, reason = sandbox_available()
    check(ok, f"bwrap works for this user ({reason or 'no error'})")

    mira_home = get_home_dir().resolve()
    secret_file = mira_home / "runtime" / "isolation-check-secret"
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    secret_file.write_text("do-not-leak")
    workspace = Path(tempfile.mkdtemp(prefix="isolation-", dir=Path.home())).resolve()

    def sandboxed(script: str) -> subprocess.CompletedProcess:
        argv = bwrap_argv(SandboxSpec.build(workspace)) + ["bash", "-c", script]
        return subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)

    result = sandboxed(f"cat {secret_file}; ls -A {mira_home}; echo end")
    check("do-not-leak" not in result.stdout and result.stdout.strip().endswith("end"),
          f"own Mira home is hidden in the sandbox (saw: {result.stdout.strip()!r})")
    token_file = mira_home / "runtime" / "gateway-token"
    if token_file.exists():
        result = sandboxed(f"cat {token_file}")
        check(token_file.read_text().strip() not in result.stdout, "own gateway token is not readable")
    result = sandboxed(f"ls {args.other_home}")
    check(result.returncode != 0, f"another user's home is invisible ({args.other_home})")
    probe = (f"import urllib.request, urllib.error\n"
             f"try:\n    urllib.request.urlopen('http://127.0.0.1:{args.other_port}/api/config', timeout=5)\n"
             f"    print('status 200')\nexcept urllib.error.HTTPError as e:\n    print('status', e.code)\n")
    result = subprocess.run(bwrap_argv(SandboxSpec.build(workspace)) + ["python3", "-c", probe],
                            capture_output=True, text=True, timeout=60, check=False)
    check(result.stdout.strip() == "status 401",
          f"another user's engine refuses the sandbox without a token ({result.stdout.strip() or result.stderr.strip()})")

    # The exec tool itself, configured from the enforced config.
    tool = ExecTool(
        working_dir=str(workspace),
        restrict_to_workspace=config.tools.restrict_to_workspace,
        sandbox=config.tools.exec.sandbox,
        sandbox_network=config.tools.exec.sandbox_network,
    )
    output = asyncio.run(tool.execute(command="echo written > out.txt && echo $HOME"))
    check((workspace / "out.txt").read_text() == "written\n", f"exec tool writes inside the workspace ({output!r})")
    # restrict_to_workspace refuses the absolute path; the sandbox would hide it anyway.
    output = asyncio.run(tool.execute(command=f"cat {secret_file}"))
    check("do-not-leak" not in output, "exec tool cannot read the Mira home")
    output = asyncio.run(tool.execute(command="cat ../.mira/runtime/isolation-check-secret"))
    check("do-not-leak" not in output, "exec tool cannot reach the Mira home through a relative path")
    output = asyncio.run(tool.execute(command="pwd", working_dir="/"))
    check("working_dir outside the workspace" in output, "exec tool refuses a working_dir outside the workspace")

    secret_file.unlink()
    print(f"\nIsolation checks passed for {os.environ.get('USER')}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
