import os
import sys
import shutil
import subprocess
from pathlib import Path


def auto_activate_env(workspace: Path):
    """Auto activate the mira environment for subprocesses by modifying PATH."""
    has_conda = shutil.which("conda") is not None

    if has_conda:
        use_shell = sys.platform == "win32"
        try:
            res = subprocess.run(
                ["conda", "env", "list"],
                capture_output=True,
                text=True,
                shell=use_shell,
            )
        except FileNotFoundError:
            res = subprocess.CompletedProcess((), 1, "", "")
        env_path = None
        for line in res.stdout.splitlines():
            if line.startswith("mira "):
                parts = line.split()
                env_path = parts[-1]
                break

        if env_path:
            bin_dir = os.path.join(env_path, "bin") if sys.platform != "win32" else os.path.join(env_path, "Scripts")
            if bin_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
                os.environ["CONDA_PREFIX"] = env_path
                os.environ["CONDA_DEFAULT_ENV"] = "mira"
    else:
        venv_path = workspace / "venv"
        if venv_path.exists():
            bin_dir = str(venv_path / "bin") if sys.platform != "win32" else str(venv_path / "Scripts")
            if bin_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
                os.environ["VIRTUAL_ENV"] = str(venv_path)
