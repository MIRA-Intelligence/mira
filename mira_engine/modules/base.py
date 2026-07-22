"""Transport- and repository-independent executor protocol."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from mira_engine.workflows.models import ModuleType


@dataclass(frozen=True)
class ExecutorDescriptor:
    module_type: ModuleType
    executor_name: str
    executor_version: str
    input_contract_version: int = 1
    checkpoint_contract_version: int = 1
    output_contract_version: int = 1


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutorSignal:
    signal_type: str
    payload: dict[str, Any]


class ModuleExecutor(Protocol):
    descriptor: ExecutorDescriptor

    async def validate_inputs(self, context: dict[str, Any]) -> ValidationResult: ...

    def execute(self, context: dict[str, Any]) -> AsyncIterator[ExecutorSignal]: ...
