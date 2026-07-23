"""Canonical modular workflow executors and shared contracts."""

from .base import ExecutorDescriptor, ModuleExecutionContext, ModuleExecutor
from .executors import (
    DeliveryExecutor,
    ExperimentExecutor,
    InitiationExecutor,
    ModuleExecutorRegistry,
    PlanningExecutor,
)
from .prompts import module_system_instructions

__all__ = [
    "DeliveryExecutor",
    "ExecutorDescriptor",
    "ExperimentExecutor",
    "InitiationExecutor",
    "ModuleExecutionContext",
    "ModuleExecutor",
    "ModuleExecutorRegistry",
    "PlanningExecutor",
    "module_system_instructions",
]
