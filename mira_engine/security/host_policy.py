"""Host policy: settings an administrator enforces for every engine on a host.

``/etc/mira/policy.toml`` is owned by root and read by every engine that starts
on the host, whether Mira Server, Remote-SSH or a person started it. Values it
sets are applied on top of the user's ``config.json`` and cannot be changed
through the API. It is not a boundary against the user (who can run any
program as themselves) but protects the user's agent from prompt injection and
lets administrators restrict what agents may do on a shared host::

    [tools]
    restrict_to_workspace = true   # file and shell tools stay in the project
    exec = false                   # disable the shell tool
    mcp = false                    # do not start MCP servers
    plugin_install = false         # refuse skill plugin install/uninstall

    [sandbox]
    mode = "bwrap"                 # run shell commands in the bwrap sandbox
    network = "none"               # "full" (default) or "none"
    read_only_paths = ["/data/reference"]
    read_write_paths = ["/scratch"]

    [[groups]]                     # overrides for members of a Unix group,
    name = "mira-trusted"          # applied in file order after the base
    tools.exec = true
    sandbox.network = "full"

Each setting is either enforced or left to the user: ``restrict_to_workspace =
true``, ``exec/mcp/plugin_install = false``, ``mode = "bwrap"`` and ``network =
"none"`` are enforced; the opposite values (or no value) leave the choice to
the user's own config. A group entry can therefore relax the base policy for
its members. Path lists are combined. An invalid policy file stops the engine
from starting rather than being ignored.
"""

from __future__ import annotations

import os
import sys
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mira_engine.config.schema import Config

POLICY_PATH = Path("/etc/mira/policy.toml")
LOCKED_ERROR_CODE = "locked_by_policy"

_TOOLS_KEYS = {"restrict_to_workspace", "exec", "mcp", "plugin_install"}
_SANDBOX_KEYS = {"mode", "network", "read_only_paths", "read_write_paths"}
_SANDBOX_MODES = {"", "off", "bwrap"}
_NETWORK_MODES = {"full", "none"}

# Config paths (by alias, as written in config.json) the policy can enforce.
RESTRICT_PATH = ("tools", "restrictToWorkspace")
EXEC_ENABLE_PATH = ("tools", "exec", "enable")
SANDBOX_PATH = ("tools", "exec", "sandbox")
SANDBOX_NETWORK_PATH = ("tools", "exec", "sandboxNetwork")
MCP_SERVERS_PATH = ("tools", "mcpServers")


class HostPolicyError(Exception):
    """The policy file exists but cannot be read or is invalid."""


class PolicyLockedError(ValueError):
    """A request tried to change a setting the host policy enforces."""

    code = LOCKED_ERROR_CODE


@dataclass(frozen=True)
class HostPolicy:
    source: Path | None = None
    groups: tuple[str, ...] = ()
    restrict_to_workspace: bool = False
    exec_disabled: bool = False
    mcp_disabled: bool = False
    plugin_install_disabled: bool = False
    sandbox: str = ""
    network_disabled: bool = False
    read_only_paths: tuple[str, ...] = ()
    read_write_paths: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return self.source is not None

    def locked_settings(self) -> list[str]:
        """Dotted config keys (as in config.json) that the policy enforces."""
        paths = []
        if self.restrict_to_workspace:
            paths.append(RESTRICT_PATH)
        if self.exec_disabled:
            paths.append(EXEC_ENABLE_PATH)
        if self.sandbox:
            paths.append(SANDBOX_PATH)
        if self.network_disabled:
            paths.append(SANDBOX_NETWORK_PATH)
        if self.mcp_disabled:
            paths.append(MCP_SERVERS_PATH)
        return [".".join(path) for path in paths]

    def summary(self) -> dict[str, Any]:
        """What clients (UI, status) may show; contains no secrets."""
        return {
            "source": str(self.source) if self.source else None,
            "groups": list(self.groups),
            "locked": self.locked_settings(),
            "plugin_install": not self.plugin_install_disabled,
        }


_EMPTY = HostPolicy()
_lock = threading.Lock()
_cached: tuple[Path, float | None, HostPolicy] | None = None


def _require(condition: bool, path: Path, message: str) -> None:
    if not condition:
        raise HostPolicyError(f"{path}: {message}")


def _check_section(table: Any, keys: set[str], where: str, path: Path) -> dict[str, Any]:
    _require(isinstance(table, dict), path, f"[{where}] must be a table")
    unknown = sorted(set(table) - keys)
    _require(not unknown, path, f"unknown key(s) in [{where}]: {', '.join(unknown)}")
    return table


def _bool(value: Any, name: str, path: Path) -> bool:
    _require(isinstance(value, bool), path, f"{name} must be true or false")
    return value


def _paths(value: Any, name: str, path: Path) -> list[str]:
    _require(isinstance(value, list) and all(isinstance(item, str) for item in value),
             path, f"{name} must be a list of absolute paths")
    for item in value:
        _require(os.path.isabs(item), path, f"{name}: {item!r} is not an absolute path")
    return [os.path.normpath(item) for item in value]


def _apply_layer(state: dict[str, Any], layer: dict[str, Any], where: str, path: Path) -> None:
    tools = _check_section(layer.get("tools", {}), _TOOLS_KEYS, f"{where}tools", path)
    for key, value in tools.items():
        state[key] = _bool(value, f"tools.{key}", path)
    sandbox = _check_section(layer.get("sandbox", {}), _SANDBOX_KEYS, f"{where}sandbox", path)
    if "mode" in sandbox:
        mode = sandbox["mode"]
        _require(mode in _SANDBOX_MODES, path, f"sandbox.mode must be one of {sorted(_SANDBOX_MODES)}")
        state["mode"] = "" if mode == "off" else mode
    if "network" in sandbox:
        network = sandbox["network"]
        _require(network in _NETWORK_MODES, path, "sandbox.network must be \"full\" or \"none\"")
        state["network"] = network
    for key in ("read_only_paths", "read_write_paths"):
        if key in sandbox:
            state[key] = [*state[key], *_paths(sandbox[key], f"sandbox.{key}", path)]


def _user_groups() -> set[str]:
    if sys.platform == "win32":
        return set()
    import grp
    import pwd

    try:
        entry = pwd.getpwuid(os.getuid())
        gids = os.getgrouplist(entry.pw_name, entry.pw_gid)
    except (KeyError, OSError):
        gids = os.getgroups()
    names = set()
    for gid in gids:
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    return names


def parse_policy(text: str, path: Path, *, groups: set[str] | None = None) -> HostPolicy:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise HostPolicyError(f"{path}: {exc}") from None
    unknown = sorted(set(data) - {"tools", "sandbox", "groups"})
    _require(not unknown, path, f"unknown section(s): {', '.join(unknown)}")
    state: dict[str, Any] = {"read_only_paths": [], "read_write_paths": []}
    _apply_layer(state, data, "", path)
    entries = data.get("groups", [])
    _require(isinstance(entries, list), path, "[[groups]] must be an array of tables")
    member_of = _user_groups() if groups is None else groups
    matched: list[str] = []
    for index, entry in enumerate(entries):
        _require(isinstance(entry, dict), path, f"groups[{index}] must be a table")
        unknown = sorted(set(entry) - {"name", "tools", "sandbox"})
        _require(not unknown, path, f"unknown key(s) in groups[{index}]: {', '.join(unknown)}")
        name = entry.get("name")
        _require(isinstance(name, str) and bool(name), path, f"groups[{index}].name must be a group name")
        # Validate every entry, even ones that do not apply to this user.
        layer_state: dict[str, Any] = {"read_only_paths": [], "read_write_paths": []}
        _apply_layer(layer_state, entry, f"groups.{name}.", path)
        if name in member_of:
            matched.append(name)
            _apply_layer(state, entry, f"groups.{name}.", path)
    return HostPolicy(
        source=path,
        groups=tuple(matched),
        restrict_to_workspace=state.get("restrict_to_workspace") is True,
        exec_disabled=state.get("exec") is False,
        mcp_disabled=state.get("mcp") is False,
        plugin_install_disabled=state.get("plugin_install") is False,
        sandbox=state.get("mode", ""),
        network_disabled=state.get("network") == "none",
        read_only_paths=tuple(dict.fromkeys(state["read_only_paths"])),
        read_write_paths=tuple(dict.fromkeys(state["read_write_paths"])),
    )


def load_host_policy(path: Path | None = None) -> HostPolicy:
    """The policy for this process's user; re-read when the file changes."""
    global _cached
    target = path or POLICY_PATH
    try:
        mtime: float | None = target.stat().st_mtime
    except FileNotFoundError:
        mtime = None
    except OSError as exc:
        raise HostPolicyError(f"{target}: {exc}") from None
    with _lock:
        if _cached is not None and _cached[0] == target and _cached[1] == mtime:
            return _cached[2]
    if mtime is None:
        policy = _EMPTY
    else:
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise HostPolicyError(f"{target}: cannot read the host policy ({exc})") from None
        policy = parse_policy(text, target)
    with _lock:
        _cached = (target, mtime, policy)
    return policy


def reset_cache() -> None:
    global _cached
    with _lock:
        _cached = None


# ── applying the policy to a Config ──────────────────────────────────


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True)
    if isinstance(value, dict):
        return {key: _dump(item) for key, item in value.items()}
    return value


def enforce(config: "Config", policy: HostPolicy | None = None) -> HostPolicy:
    """Apply ``policy`` to ``config`` in place, remembering the user's values.

    ``save_config`` writes the remembered values back, so the policy never ends
    up in the user's config.json.
    """
    policy = policy if policy is not None else load_host_policy()
    remembered: dict[tuple[str, ...], Any] = config._policy_user_values
    tools = config.tools

    def force(path: tuple[str, ...], current: Any, value: Any, setter: Any) -> None:
        if path not in remembered:
            remembered[path] = _dump(current)
        setter(value)

    if policy.restrict_to_workspace:
        force(RESTRICT_PATH, tools.restrict_to_workspace, True,
              lambda v: setattr(tools, "restrict_to_workspace", v))
    if policy.exec_disabled:
        force(EXEC_ENABLE_PATH, tools.exec.enable, False, lambda v: setattr(tools.exec, "enable", v))
    if policy.sandbox:
        force(SANDBOX_PATH, tools.exec.sandbox, policy.sandbox, lambda v: setattr(tools.exec, "sandbox", v))
    if policy.network_disabled:
        force(SANDBOX_NETWORK_PATH, tools.exec.sandbox_network, "none",
              lambda v: setattr(tools.exec, "sandbox_network", v))
    if policy.mcp_disabled:
        force(MCP_SERVERS_PATH, tools.mcp_servers, {}, lambda v: setattr(tools, "mcp_servers", v))
    return policy


def restore_user_values(config: "Config", data: dict[str, Any]) -> dict[str, Any]:
    """Put the user's own values back into a ``model_dump(by_alias=True)``."""
    for path, value in config._policy_user_values.items():
        node = data
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value
    return data


def check_runtime_change(path: tuple[str, ...], current: Any, requested: Any,
                         policy: HostPolicy | None = None) -> None:
    """Raise :class:`PolicyLockedError` when an API request changes an enforced value."""
    policy = policy if policy is not None else load_host_policy()
    if ".".join(path) in policy.locked_settings() and requested != current:
        raise PolicyLockedError(f"{'.'.join(path)} is set by the host policy ({policy.source}) "
                                "and cannot be changed here")
