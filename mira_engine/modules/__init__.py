"""Canonical modular workflow executors and shared contracts."""

from .base import ExecutorDescriptor, ModuleExecutor
from .prompts import module_system_instructions

__all__ = ["ExecutorDescriptor", "ModuleExecutor", "module_system_instructions"]
