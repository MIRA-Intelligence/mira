"""Where the Mira Server deployment files live (``mira-server deploy-dir``).

The wheel ships ``deploy/mira-server`` as ``mira_server/deploy`` (PAM services,
systemd unit, host policy, AppArmor profile, example configs and the Ubuntu
installer). In a source checkout or an editable install, the files are read from
the repository's ``deploy/mira-server`` instead.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
# A file every copy contains, to tell the directory apart from a stray one.
MARKER_FILE = "pam.d-mira-server"


class DeployFilesMissingError(RuntimeError):
    pass


def candidates() -> list[Path]:
    return [PACKAGE_DIR / "deploy", PACKAGE_DIR.parent / "deploy" / "mira-server"]


def deploy_dir() -> Path:
    for path in candidates():
        if (path / MARKER_FILE).is_file():
            return path
    tried = ", ".join(str(path) for path in candidates())
    raise DeployFilesMissingError(f"Mira Server deployment files not found (tried {tried}); "
                                  "reinstall mira-engine[server]")
