"""Transport- and repository-independent executor protocol."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from mira_engine.workflows.models import Artifact, ModuleRun, ModuleType

from .profiles import AgentProfile


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
class ModuleExecutionContext:
    run: ModuleRun
    artifacts: tuple[Artifact, ...]
    project_dir: Path
    user_content: str
    metadata: dict[str, Any]
    agent_profile: AgentProfile


@dataclass(frozen=True)
class ModuleInvocation:
    system_instructions: str
    blocked_tools: frozenset[str]


ModuleInvoker = Callable[[ModuleInvocation], Awaitable[Any]]


class ModuleExecutor(Protocol):
    descriptor: ExecutorDescriptor

    async def validate_inputs(self, context: ModuleExecutionContext) -> ValidationResult: ...

    async def execute(
        self,
        context: ModuleExecutionContext,
        invoke: ModuleInvoker,
    ) -> Any: ...
