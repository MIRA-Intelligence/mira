"""Who a user is and whether they may use Mira Server.

Production identities come from NSS (``pwd``/``grp``), so local accounts, LDAP
and SSSD all work the same way. Membership includes supplementary groups
(``os.getgrouplist``). ``users_group`` grants access, ``admins_group`` adds the
admin role. Dev identities come from ``[[auth.dev_users]]`` and all map to the
uid running Mira Server.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

from mira_server.config import AuthSection, valid_username

ROLE_USER = "user"
ROLE_ADMIN = "admin"


@dataclass(frozen=True)
class Identity:
    name: str
    uid: int
    home: str
    display_name: str
    roles: tuple[str, ...]

    @property
    def is_admin(self) -> bool:
        return ROLE_ADMIN in self.roles

    @property
    def role(self) -> str:
        return ROLE_ADMIN if self.is_admin else ROLE_USER

    def as_json(self) -> dict[str, Any]:
        return {"name": self.name, "display_name": self.display_name, "roles": list(self.roles)}


class UnknownUserError(Exception):
    """No such account."""


class AccessDeniedError(Exception):
    """The account exists but may not use Mira Server."""


class IdentityResolver(Protocol):
    def resolve(self, username: str) -> Identity:
        """Return the identity or raise :class:`UnknownUserError` / :class:`AccessDeniedError`."""


def _display_name(name: str, gecos: str) -> str:
    first = (gecos or "").split(",", 1)[0].strip()
    return first or name


class NssIdentityResolver:
    def __init__(self, users_group: str, admins_group: str, *, pwd_module: Any = None,
                 grp_module: Any = None, getgrouplist: Any = None) -> None:
        if pwd_module is None or grp_module is None:
            import grp
            import pwd

            pwd_module = pwd_module or pwd
            grp_module = grp_module or grp
        self.users_group = users_group
        self.admins_group = admins_group
        self._pwd = pwd_module
        self._grp = grp_module
        self._getgrouplist = getgrouplist or os.getgrouplist

    def group_names(self, name: str, primary_gid: int) -> set[str]:
        names: set[str] = set()
        try:
            gids: Iterable[int] = self._getgrouplist(name, primary_gid)
        except OSError:
            gids = [primary_gid]
        for gid in set(gids) | {primary_gid}:
            try:
                names.add(self._grp.getgrgid(gid).gr_name)
            except KeyError:
                continue
        return names

    def resolve(self, username: str) -> Identity:
        if not valid_username(username):
            raise UnknownUserError(username)
        try:
            entry = self._pwd.getpwnam(username)
        except KeyError:
            raise UnknownUserError(username) from None
        if entry.pw_uid == 0:
            raise AccessDeniedError("root may not use Mira Server")
        groups = self.group_names(entry.pw_name, entry.pw_gid)
        if self.users_group not in groups:
            raise AccessDeniedError(f"{entry.pw_name} is not a member of {self.users_group}")
        roles = (ROLE_USER, ROLE_ADMIN) if self.admins_group in groups else (ROLE_USER,)
        return Identity(
            name=entry.pw_name,
            uid=entry.pw_uid,
            home=entry.pw_dir,
            display_name=_display_name(entry.pw_name, entry.pw_gecos),
            roles=roles,
        )


class DevIdentityResolver:
    """``[[auth.dev_users]]``; every dev user runs as the current OS user."""

    def __init__(self, auth: AuthSection) -> None:
        self._users = {user.name: user for user in auth.dev_users}

    def resolve(self, username: str) -> Identity:
        user = self._users.get(username)
        if user is None:
            raise UnknownUserError(username)
        return Identity(
            name=user.name,
            uid=os.getuid(),
            home=str(Path.home()),
            display_name=user.display_name or user.name,
            roles=(ROLE_USER, ROLE_ADMIN) if user.admin else (ROLE_USER,),
        )
