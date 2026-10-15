"""``mira-server`` command line: serve, hash-password, install-launcher, check."""

from __future__ import annotations

import getpass
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer

from mira_server.config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    ServerConfig,
    check_serve_allowed,
    load_config,
)

CONFIG_ENV = "MIRA_SERVER_CONFIG"

app = typer.Typer(help="Mira Server: host-account sign-in and one engine per user.", no_args_is_help=True,
                  add_completion=False, rich_markup_mode=None)


def _config_path(value: Path | None) -> Path:
    if value is not None:
        return value
    env = os.environ.get(CONFIG_ENV, "").strip()
    return Path(env) if env else DEFAULT_CONFIG_PATH


def _load(path: Path) -> ServerConfig:
    try:
        return load_config(path)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2) from None


ConfigOption = typer.Option(None, "--config", "-c", help=f"Config file (default ${CONFIG_ENV} or {DEFAULT_CONFIG_PATH})")


@app.command()
def serve(
    config: Path | None = ConfigOption,
    dev: bool = typer.Option(False, "--dev", help="Allow dev-only settings (dev auth, local spawner); loopback only"),
    log_level: str = typer.Option("INFO", "--log-level", help="Python log level"),
) -> None:
    """Run Mira Server."""
    logging.basicConfig(level=log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # aiohttp's access log would record query strings, which may carry tokens.
    logging.getLogger("aiohttp.access").disabled = True
    cfg = _load(_config_path(config))
    try:
        check_serve_allowed(cfg, dev=dev)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2) from None
    if os.name == "posix" and os.geteuid() == 0:
        typer.echo("Error: do not run Mira Server as root; run it as the unprivileged mira-server account.",
                   err=True)
        raise typer.Exit(2)
    if dev:
        logging.getLogger("mira_server").warning(
            "DEV MODE: %s. Not for real users.", ", ".join(cfg.dev_only_settings()) or "no isolation guarantees")

    from aiohttp import web

    from mira_server.app import create_server
    from mira_server.auth.backends import ExtraMissingError

    os.umask(0o077)
    try:
        server = create_server(cfg, dev=dev)
    except ExtraMissingError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2) from None
    host = cfg.server.host
    logging.getLogger("mira_server").info("Mira Server listening on %s (data dir %s)", cfg.server.listen,
                                          cfg.server.data_dir)
    web.run_app(server.build_app(), host=host, port=cfg.server.port, access_log=None, print=None,
                handle_signals=True)


@app.command("hash-password")
def hash_password_command(
    stdin: bool = typer.Option(False, "--stdin", help="Read the password from the first line of stdin"),
) -> None:
    """Print an scrypt hash for [[auth.dev_users]] password_hash (dev mode only)."""
    from mira_server.auth.backends import hash_password

    if stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Repeat: ") != password:
            typer.echo("Error: passwords do not match", err=True)
            raise typer.Exit(1)
    if not password:
        typer.echo("Error: empty password", err=True)
        raise typer.Exit(1)
    typer.echo(hash_password(password))


@app.command("install-launcher")
def install_launcher_command(
    prefix: Path = typer.Option(Path("/usr/local"), "--prefix", help="Installs <prefix>/libexec/mira/engine-launch"),
    engine_python: str = typer.Option(sys.executable, "--engine-python",
                                      help="Interpreter of the system-wide engine install (e.g. /opt/mira/bin/python)"),
    server_user: str = typer.Option("mira-server", "--server-user", help="Account Mira Server runs as"),
    users_group: str = typer.Option("mira-users", "--users-group", help="Group allowed to use Mira Server"),
    no_root_check: bool = typer.Option(False, "--no-root-check", hidden=True,
                                       help="Write without root (tests only; the result is not safe)"),
) -> None:
    """Write the root-owned engine launcher and print the sudoers rule."""
    from mira_server.spawner.launcher import (
        LauncherInstallError,
        default_launcher_path,
        install_launcher,
        sudoers_rules,
    )

    path = default_launcher_path(prefix)
    try:
        install_launcher(path, engine_python, require_root=not no_root_check)
    except (LauncherInstallError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    rules = sudoers_rules(path, server_user=server_user, users_group=users_group)
    typer.echo(f"Wrote {path} (engine: {engine_python}).\n")
    typer.echo("Add these sudoers rules (for example: visudo -f /etc/sudoers.d/mira-server):\n")
    for rule in rules:
        typer.echo(f"    {rule}")
    typer.echo("")
    typer.echo("Notes:")
    typer.echo(f"- The first rule starts, inspects and stops engines of {users_group} members. The second only")
    typer.echo(f"  stops engines, for people removed from {users_group} (their engine would keep running otherwise).")
    typer.echo(f"- {path} and every directory above it must stay root-owned and not group/world-writable.")
    typer.echo(f"- The engine at {engine_python} must be root-owned too (e.g. a venv in /opt/mira).")
    typer.echo(f"- In mira-server.toml set [spawner] launcher = \"{path}\".")
    typer.echo("- Run `mira-server check` as the mira-server account to verify the setup.")


class _Report:
    def __init__(self) -> None:
        self.errors = 0

    def ok(self, message: str) -> None:
        typer.echo(f"ok    {message}")

    def warn(self, message: str) -> None:
        typer.echo(f"warn  {message}")

    def fail(self, message: str) -> None:
        self.errors += 1
        typer.echo(f"FAIL  {message}")


def _check_pam(cfg: ServerConfig, report: _Report) -> None:
    from mira_server.auth.backends import ExtraMissingError, require_pam

    try:
        require_pam()
        report.ok("python-pam is installed")
    except ExtraMissingError as exc:
        report.fail(str(exc))
    service = Path("/etc/pam.d") / cfg.auth.pam_service
    if service.exists():
        report.ok(f"PAM service {service} exists")
    else:
        report.fail(f"PAM service file {service} is missing (see deploy/mira-server/pam.d-mira-server)")
    shadow = Path("/etc/shadow")
    if shadow.exists() and not os.access(shadow, os.R_OK):
        report.warn("cannot read /etc/shadow: pam_unix (local passwords) only verifies other users' "
                    "passwords for root or members of the 'shadow' group; LDAP/SSSD (pam_sss) is unaffected")


def _check_groups(cfg: ServerConfig, report: _Report) -> None:
    try:
        import grp
    except ImportError:
        report.fail("grp is unavailable on this platform")
        return
    for name, required in ((cfg.auth.users_group, True), (cfg.auth.admins_group, False)):
        try:
            members = grp.getgrnam(name).gr_mem
            report.ok(f"group {name} exists ({len(members)} listed members)")
        except KeyError:
            (report.fail if required else report.warn)(f"group {name} does not exist")


def _check_sudo(cfg: ServerConfig, report: _Report) -> None:
    from mira_server.spawner.launcher import granted_launcher_rules, inspect_launcher

    launcher = cfg.spawner.launcher
    problems = inspect_launcher(launcher)
    for problem in problems:
        report.fail(problem)
    if not problems:
        report.ok(f"launcher {launcher} is root-owned and not writable by others")
    sudo = str(cfg.spawner.sudo)
    if not os.access(sudo, os.X_OK):
        report.fail(f"sudo not found at {sudo}")
        return
    try:
        result = subprocess.run([sudo, "-n", "-l"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.fail(f"`sudo -n -l` failed: {exc}")
        return
    full, stop = granted_launcher_rules(result.stdout, launcher)
    if full:
        report.ok(f"sudo allows this account to run {launcher}")
    else:
        report.fail(f"no NOPASSWD sudo rule lets this account run {launcher} (`sudo -n -l` exit "
                    f"{result.returncode}); run `mira-server install-launcher` for the rules")
    if stop:
        report.ok(f"sudo allows `{launcher} stop` for accounts outside {cfg.auth.users_group}")
    elif full:
        report.warn(f"no sudo rule for `{launcher} stop` as other accounts: engines of people removed from "
                    f"{cfg.auth.users_group} keep running until stopped over SSH (see `install-launcher`)")


def _check_data_dir(cfg: ServerConfig, report: _Report) -> None:
    data_dir = cfg.server.data_dir
    if not data_dir.exists():
        parent = data_dir.parent
        if os.access(parent, os.W_OK):
            report.ok(f"data dir {data_dir} will be created")
        else:
            report.fail(f"data dir {data_dir} does not exist and {parent} is not writable")
        return
    mode = data_dir.stat().st_mode & 0o777
    if not os.access(data_dir, os.W_OK):
        report.fail(f"data dir {data_dir} is not writable by this account")
    elif mode & 0o077:
        report.warn(f"data dir {data_dir} has mode {mode:o}; Mira Server tightens it to 700 on start")
    else:
        report.ok(f"data dir {data_dir} is private")


@app.command()
def check(config: Path | None = ConfigOption) -> None:
    """Validate the config and the host setup (run it as the mira-server account)."""
    path = _config_path(config)
    report = _Report()
    try:
        cfg = load_config(path)
        report.ok(f"config {path} is valid")
    except ConfigError as exc:
        report.fail(str(exc))
        raise typer.Exit(1) from None
    for setting in cfg.dev_only_settings():
        report.warn(f"{setting} is dev-only (serve needs --dev)")
    if os.name == "posix" and os.geteuid() == 0:
        report.warn("running as root: run `check` as the mira-server account for meaningful results")
    _check_data_dir(cfg, report)
    if cfg.auth.backend == "pam":
        _check_pam(cfg, report)
        _check_groups(cfg, report)
    if cfg.spawner.backend == "sudo":
        _check_sudo(cfg, report)
    elif not shutil.which(cfg.spawner.engine_python or sys.executable):
        report.fail(f"engine interpreter {cfg.spawner.engine_python} not found")
    if not sys.platform.startswith("linux") and cfg.spawner.backend == "sudo":
        report.warn("production Mira Server (PAM, sudo, /proc/net/tcp) is supported on Linux only")
    if report.errors:
        typer.echo(f"\n{report.errors} problem(s) found.")
        raise typer.Exit(1)
    typer.echo("\nAll checks passed.")


def main() -> None:
    app()
