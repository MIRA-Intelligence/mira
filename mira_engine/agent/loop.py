"""Agent loop entry point — backwards-compatible shim.

Historically this module hosted a single ``AgentLoop`` class that bundled
both the general agent loop and Mira's research-specific orchestration. The
implementation has been split into two classes:

* :class:`mira_engine.agent.base_loop.BaseAgentLoop` — nanobot-style baseline
  used by ``mira agent`` and any general agent workload.
* :class:`mira_engine.agent.research_loop.ResearchAgentLoop` — research
  superset used by ``mira research`` and the desktop UI gateway. Adds
  auto-mode, agent profiles, automation policies, task-plan guardrails, and
  cumulative session token tracking.

To preserve every existing import (`from mira_engine.agent.loop import
AgentLoop`), :data:`AgentLoop` is aliased to ``ResearchAgentLoop`` so the
default behaviour for callers that have not yet migrated is unchanged.
Callers that explicitly want the leaner base loop must import
:class:`BaseAgentLoop` directly.
"""

from __future__ import annotations

from mira_engine.agent.base_loop import UNIFIED_SESSION_KEY, BaseAgentLoop
from mira_engine.agent.research_loop import ResearchAgentLoop

# Backwards-compatible alias: existing callers (gateway, serve, mira_engine
# facade, tests, channels) get the research-superset by default. New callers
# that want the nanobot-shaped baseline should import ``BaseAgentLoop``
# directly.
AgentLoop = ResearchAgentLoop

__all__ = [
    "AgentLoop",
    "BaseAgentLoop",
    "ResearchAgentLoop",
    "UNIFIED_SESSION_KEY",
]
