#!/usr/bin/env bash
set -euo pipefail

# Run scoped coverage for core modules (providers excluded by design).
python -m pytest tests -q \
  --cov=medpilot.agent.loop \
  --cov=medpilot.agent.context \
  --cov=medpilot.agent.memory \
  --cov=medpilot.agent.routing \
  --cov=medpilot.agent.tools.base \
  --cov=medpilot.agent.tools.filesystem \
  --cov=medpilot.agent.tools.shell \
  --cov=medpilot.agent.tools.web \
  --cov=medpilot.agent.tools.message \
  --cov=medpilot.agent.tools.registry \
  --cov=medpilot.agent.tools.spawn \
  --cov=medpilot.agent.tools.cron \
  --cov=medpilot.channels.manager \
  --cov=medpilot.channels.web \
  --cov=medpilot.config.loader \
  --cov=medpilot.config.schema \
  --cov-report=term-missing \
  --cov-report=xml:coverage-core.xml
