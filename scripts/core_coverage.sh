#!/usr/bin/env bash
set -euo pipefail

# Run scoped coverage for core modules (providers excluded by design).
python -m pytest tests -q \
  --cov=mira_engine.agent.loop \
  --cov=mira_engine.agent.context \
  --cov=mira_engine.agent.memory \
  --cov=mira_engine.agent.routing \
  --cov=mira_engine.agent.tools.base \
  --cov=mira_engine.agent.tools.filesystem \
  --cov=mira_engine.agent.tools.shell \
  --cov=mira_engine.agent.tools.web \
  --cov=mira_engine.agent.tools.message \
  --cov=mira_engine.agent.tools.registry \
  --cov=mira_engine.agent.tools.spawn \
  --cov=mira_engine.agent.tools.cron \
  --cov=mira_engine.channels.manager \
  --cov=mira_engine.channels.ui \
  --cov=mira_engine.config.loader \
  --cov=mira_engine.config.schema \
  --cov-report=term-missing \
  --cov-report=xml:coverage-core.xml
