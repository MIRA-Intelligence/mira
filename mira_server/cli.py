"""``mira-server`` command line: serve, hash-password, install-launcher, check, deploy-dir, usage, quota."""

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
    sudoers_file: Path | None = typer.Option(None, "--sudoers-file",
                                             help="Also write the rules to this sudoers drop-in after "
                                                  "`visudo -cf` accepts them (e.g. /etc/sudoers.d/mira-server)"),
    visudo: str = typer.Option("visudo", "--visudo", hidden=True, help="visudo binary (tests)"),
    no_root_check: bool = typer.Option(False, "--no-root-check", hidden=True,
                                       help="Write without root (tests only; the result is not safe)"),
) -> None:
    """Write the root-owned engine launcher and print (or install) the sudoers rules."""
    from mira_server.spawner.launcher import (
        LauncherInstallError,
        default_launcher_path,
        install_launcher,
        install_sudoers,
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
    if sudoers_file is not None:
        try:
            install_sudoers(sudoers_file, rules, visudo=visudo, require_root=not no_root_check)
        except (LauncherInstallError, OSError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from None
        typer.echo(f"Wrote {sudoers_file} (checked with visudo):\n")
    else:
        typer.echo("Add these sudoers rules (for example: visudo -f /etc/sudoers.d/mira-server):\n")
    for rule in rules:
        typer.echo(f"    {rule}")
    typer.echo("")
    typer.echo("Notes:")
    typer.echo("- Cmnd_Alias/Defaults run `start` through the mira-engine PAM service (install")
    typer.echo("  $(mira-server deploy-dir)/pam.d-mira-engine as /etc/pam.d/mira-engine first): engines then run in")
    typer.echo("  the user's user-<uid>.slice, where per-user systemd limits apply.")
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
        report.fail(f"PAM service file {service} is missing (see $(mira-server deploy-dir)/pam.d-mira-server)")
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
    _check_engine_session(result.stdout, report)
    if stop:
        report.ok(f"sudo allows `{launcher} stop` for accounts outside {cfg.auth.users_group}")
    elif full:
        report.warn(f"no sudo rule for `{launcher} stop` as other accounts: engines of people removed from "
                    f"{cfg.auth.users_group} keep running until stopped over SSH (see `install-launcher`)")


def _check_engine_session(sudo_list: str, report: _Report) -> None:
    """`start` must go through the mira-engine PAM service for per-user slices."""
    from mira_server.spawner.launcher import PAM_SESSION_SERVICE

    pam_file = Path("/etc/pam.d") / PAM_SESSION_SERVICE
    if f"pam_service={PAM_SESSION_SERVICE}" not in sudo_list.replace(" ", ""):
        report.warn("sudo does not run `engine-launch start` through the mira-engine PAM service: engines "
                    "will not run in the users' user-<uid>.slice, so per-user limits do not apply "
                    "(see `install-launcher`)")
    elif not pam_file.exists():
        report.fail(f"sudoers uses PAM service {PAM_SESSION_SERVICE} but {pam_file} is missing, so engines "
                    "cannot start (copy $(mira-server deploy-dir)/pam.d-mira-engine)")
    else:
        report.ok(f"engines start in a user session (PAM service {pam_file})")


def _check_host_policy(report: _Report) -> None:
    """The engine host policy (/etc/mira/policy.toml) that isolates users' agents."""
    from mira_engine.agent.tools.sandbox import sandbox_available
    from mira_engine.security import host_policy

    path = host_policy.POLICY_PATH
    if not path.exists():
        report.warn(f"no host policy at {path}: agents run shell commands without the sandbox and may "
                    "leave their project; copy $(mira-server deploy-dir)/policy.toml there")
        return
    try:
        policy = host_policy.parse_policy(path.read_text(encoding="utf-8"), path, groups=set())
    except (host_policy.HostPolicyError, OSError, UnicodeDecodeError) as exc:
        report.fail(f"host policy is invalid, so engines will not start: {exc}")
        return
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        report.warn(f"{path} should be owned by root and not writable by group or others")
    report.ok(f"host policy {path} is valid (enforces: {', '.join(policy.locked_settings()) or 'nothing'})")
    if not policy.restrict_to_workspace:
        report.warn("the host policy does not set tools.restrict_to_workspace = true")
    if policy.sandbox != "bwrap":
        report.warn("the host policy does not set sandbox.mode = \"bwrap\"")
        return
    ok, reason = sandbox_available(not policy.network_disabled)
    if ok:
        report.ok("bwrap sandbox works on this host")
    else:
        report.fail(f"the host policy requires the bwrap sandbox but it does not work here ({reason}); "
                    "agents cannot run shell commands until it does")


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
    _check_host_policy(report)
    if report.errors:
        typer.echo(f"\n{report.errors} problem(s) found.")
        raise typer.Exit(1)
    typer.echo("\nAll checks passed.")


@app.command("deploy-dir")
def deploy_dir_command() -> None:
    """Print the directory with the deployment files (PAM services, unit, policy, installer)."""
    from mira_server.deploy_files import DeployFilesMissingError, deploy_dir

    try:
        typer.echo(str(deploy_dir()))
    except DeployFilesMissingError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None


# ── token usage and quotas ───────────────────────────────────────────

quota_app = typer.Typer(help="Token quotas for team providers (run as the mira-server account).",
                        no_args_is_help=True, add_completion=False, rich_markup_mode=None)
app.add_typer(quota_app, name="quota")

_SUFFIXES = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000}


def _open_store(config: Path | None):
    import sqlite3

    from mira_server.store import Store

    cfg = _load(_config_path(config))
    if not cfg.db_path.exists():
        typer.echo(f"Error: {cfg.db_path} does not exist (has Mira Server run yet?)", err=True)
        raise typer.Exit(1)
    try:
        return Store(cfg.db_path)
    except (OSError, sqlite3.Error) as exc:
        typer.echo(f"Error: cannot open {cfg.db_path}: {exc} (run as the mira-server account)", err=True)
        raise typer.Exit(1) from None


def _tokens(value: str, *, override: bool) -> int | None:
    """``2M``, ``500k``, ``1000``; ``unlimited`` (0); ``default`` (an override follows the team default)."""
    text = value.strip().lower().replace(",", "").replace("_", "")
    if text in {"unlimited", "none", "0"}:
        return 0
    if text == "default":
        if not override:
            raise typer.BadParameter("'default' only applies to --user")
        return None
    factor = _SUFFIXES.get(text[-1:], 1)
    number = text[:-1] if text[-1:] in _SUFFIXES else text
    try:
        amount = float(number) * factor
    except ValueError:
        raise typer.BadParameter(f"{value!r} is not a number of tokens (e.g. 2M, 500k, unlimited)") from None
    if amount < 0 or amount != int(amount):
        raise typer.BadParameter(f"{value!r} is not a whole number of tokens")
    return int(amount)


def _fmt(limit: int | None) -> str:
    return "unlimited" if not limit else f"{limit:,}"


@app.command()
def usage(
    since: str = typer.Option("", "--from", help="First day, YYYY-MM-DD (default: the 1st of this month)"),
    until: str = typer.Option("", "--to", help="Last day, YYYY-MM-DD (default: today)"),
    by: str = typer.Option("user", "--by", help="Group by: user, day, month, provider, model (comma-separated)"),
    as_csv: bool = typer.Option(False, "--csv", help="CSV instead of a table"),
    config: Path | None = ConfigOption,
) -> None:
    """Token usage of team providers (run as the mira-server account)."""
    import csv
    from datetime import date

    from mira_server.store import USAGE_COLUMNS, USAGE_GROUPS
    from mira_server.usage import local_day, period_start

    today = local_day()
    try:
        since = date.fromisoformat(since or period_start("monthly", today)).isoformat()
        until = date.fromisoformat(until or today).isoformat()
    except ValueError:
        raise typer.BadParameter("--from and --to take dates as YYYY-MM-DD") from None
    group_by = [group.strip() for group in by.split(",") if group.strip()]
    if any(group not in USAGE_GROUPS for group in group_by) or len(set(group_by)) != len(group_by):
        raise typer.BadParameter(f"--by takes distinct values of: {', '.join(USAGE_GROUPS)}")
    store = _open_store(config)
    rows = store.usage_summary(since_day=since, until_day=until, group_by=group_by)
    columns = [*group_by, *USAGE_COLUMNS]
    if as_csv:
        writer = csv.writer(sys.stdout)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row[column] for column in columns])
        return
    typer.echo(f"Team-provider token usage, {since} to {until}")
    if not rows:
        typer.echo("(none)")
        return
    cells = [[str(row[c]) if c in group_by else f"{row[c]:,}" for c in columns] for row in rows]
    widths = [max(len(column), *(len(line[i]) for line in cells)) for i, column in enumerate(columns)]
    typer.echo("  ".join(column.ljust(widths[i]) for i, column in enumerate(columns)))
    for line in cells:
        typer.echo("  ".join(value.ljust(widths[i]) if i < len(group_by) else value.rjust(widths[i])
                             for i, value in enumerate(line)))


@quota_app.command("show")
def quota_show(config: Path | None = ConfigOption) -> None:
    """Show the token quotas."""
    from mira_server.usage import parse_quotas

    quotas = parse_quotas(_open_store(config).get_setting("quotas") or {})
    typer.echo(f"Default per user: daily {_fmt(quotas.default.daily)}, monthly {_fmt(quotas.default.monthly)}")
    typer.echo(f"Warn at: {quotas.warn_percent}%")
    for name, limits in sorted((quotas.users or {}).items()):
        daily = "default" if limits.daily is None else _fmt(limits.daily)
        monthly = "default" if limits.monthly is None else _fmt(limits.monthly)
        typer.echo(f"  {name}: daily {daily}, monthly {monthly}")


@quota_app.command("set")
def quota_set(
    user: str = typer.Option("", "--user", "-u", help="Change this user's override instead of the team default"),
    daily: str = typer.Option("", "--daily", help="Daily tokens: 2M, 500k, unlimited; with --user also default"),
    monthly: str = typer.Option("", "--monthly", help="Monthly tokens, as --daily"),
    warn: int = typer.Option(0, "--warn", help="Warn users at this percentage of a quota (1-100)"),
    config: Path | None = ConfigOption,
) -> None:
    """Change the team default, a user's override, or the warning threshold. Applies at once."""
    from mira_server.config import valid_username
    from mira_server.usage import QuotaError, parse_quotas

    if not (daily or monthly or warn):
        raise typer.BadParameter("give --daily, --monthly and/or --warn")
    store = _open_store(config)
    data = parse_quotas(store.get_setting("quotas") or {}).as_json()
    if user:
        entry = data["users"].setdefault(user, {"daily": None, "monthly": None})
    else:
        entry = data["default"]
    for period, value in (("daily", daily), ("monthly", monthly)):
        if value:
            entry[period] = _tokens(value, override=bool(user))
    if warn:
        data["warn_percent"] = warn
    try:
        quotas = parse_quotas(data, valid_user=valid_username)
    except QuotaError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2) from None
    store.put_setting("quotas", quotas.as_json())
    store.audit("team.quotas_changed", username=f"cli:{getpass.getuser()}", default=quotas.default.as_json(),
                users=[user] if user else [], warn_percent=quotas.warn_percent)
    quota_show(config)


def main() -> None:
    app()
