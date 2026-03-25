import os
import sys
import subprocess
import pytest
from pathlib import Path

def test_install_sh_no_conda(tmp_path):
    """Test the shell script behavior when Conda is missing."""
    install_script = Path("install.sh").absolute()
    
    # Create a wrapper script to manipulate PATH and inputs
    wrapper_path = tmp_path / "run_install.sh"
    with open(wrapper_path, "w") as f:
        f.write(f'''#!/usr/bin/env bash
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"  # Exclude any conda
# mock pip and python to do nothing
function python() {{ echo "Simulated python $@"; }}
function pip() {{ echo "Simulated pip $@"; }}
export -f python
export -f pip

# Run installer and provide "n" to standard python virtual environment, 
# but wait, the script reads from terminal (read -p). We can provide input via stdin.
# Actually, the read -p reads from stdin unless -u is specified.
bash "{install_script}" << 'INPUT'
n
INPUT
''')
    wrapper_path.chmod(0o755)
    
    result = subprocess.run(["bash", str(wrapper_path)], capture_output=True, text=True)
    assert "Warning: conda is not installed" in result.stdout
    assert "Simulated pip install -e ." in result.stdout

def test_install_sh_with_conda(tmp_path):
    """Test the shell script behavior when Conda is present."""
    install_script = Path("install.sh").absolute()
    
    wrapper_path = tmp_path / "run_install.sh"
    with open(wrapper_path, "w") as f:
        f.write(f'''#!/usr/bin/env bash
# Mock conda and pip
function conda() {{
    if [ "$1" = "env" ] && [ "$2" = "list" ]; then
        echo "base /path/to/base"
        echo "other /path/to/other"
    else
        echo "Simulated conda $@"
    fi
}}
function pip() {{ echo "Simulated pip $@"; }}
export -f conda
export -f pip

# Run installer: Provide "n" to 'create new conda env', then provide 'base' to 'select existing env'
bash "{install_script}" << 'INPUT'
n
base
INPUT
''')
    wrapper_path.chmod(0o755)
    
    result = subprocess.run(["bash", str(wrapper_path)], capture_output=True, text=True)
    assert "Conda is installed." in result.stdout
    assert "Available conda environments:" in result.stdout
    assert "Selected environment: base" in result.stdout
    assert "Simulated pip install -e ." in result.stdout

