import os
import sys
import shutil
import subprocess
from pathlib import Path
import typer
from rich.console import Console

console = Console()

def setup_medpilot_env(workspace: Path):
    """Check for and create medpilot virtual environment if missing."""
    has_conda = shutil.which("conda") is not None
    
    if has_conda:
        res = subprocess.run(["conda", "env", "list"], capture_output=True, text=True)
        envs = [line.split()[0] for line in res.stdout.splitlines() if line and not line.startswith("#")]
        if "medpilot" not in envs:
            if typer.confirm("Conda environment 'medpilot' not found. Do you want to create it? (Recommended for workspace isolation)", default=True):
                console.print("[cyan]Creating conda environment 'medpilot' (Python 3.11)...[/cyan]")
                subprocess.run(["conda", "create", "-n", "medpilot", "python=3.11", "pip", "-y"])
                console.print("[green]✓ Conda environment 'medpilot' created.[/green]")
    else:
        venv_path = workspace / "venv"
        if not venv_path.exists():
            if typer.confirm("Virtual environment not found. Do you want to create one at ~/.medpilot/venv?", default=True):
                console.print("[cyan]Creating standard Python venv...[/cyan]")
                subprocess.run([sys.executable, "-m", "venv", str(venv_path)])
                console.print("[green]✓ Virtual environment created.[/green]")

def auto_activate_env(workspace: Path):
    """Auto activate the medpilot environment for subprocesses by modifying PATH."""
    has_conda = shutil.which("conda") is not None
    
    if has_conda:
        res = subprocess.run(["conda", "env", "list"], capture_output=True, text=True)
        env_path = None
        for line in res.stdout.splitlines():
            if line.startswith("medpilot "):
                # Usually: `medpilot       *  /path/to/conda/envs/medpilot` or `medpilot       /path/to/conda/envs/medpilot`
                parts = line.split()
                env_path = parts[-1]
                break
        
        if env_path:
            bin_dir = os.path.join(env_path, "bin") if sys.platform != "win32" else os.path.join(env_path, "Scripts")
            if bin_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
                os.environ["CONDA_PREFIX"] = env_path
                os.environ["CONDA_DEFAULT_ENV"] = "medpilot"
    else:
        venv_path = workspace / "venv"
        if venv_path.exists():
            bin_dir = str(venv_path / "bin") if sys.platform != "win32" else str(venv_path / "Scripts")
            if bin_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
                os.environ["VIRTUAL_ENV"] = str(venv_path)

