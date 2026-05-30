"""Slash command routing and built-in handlers."""

from mira_engine.command.router import CommandContext, CommandRouter

__all__ = ["CommandContext", "CommandRouter", "register_builtin_commands"]


def register_builtin_commands(router: CommandRouter) -> None:
    from mira_engine.command.builtin import register_builtin_commands as _register

    _register(router)
