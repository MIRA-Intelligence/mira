Compare conversation history against current memory files. Also scan memory files for stale content — even if not mentioned in history.

Output one line per finding:
[FILE] atomic fact (not already in memory)
[FILE-REMOVE] reason for removal

Files: PERSONAL (confirmed user identity, preferences, reusable workflows and cross-project knowledge), USER (project-specific user context), SOUL (bot behavior, tone), MEMORY (project facts and decisions)

Rules:
- Atomic facts: "has a cat named Luna" not "discussed pet care"
- Corrections: [PERSONAL] location is Tokyo, not Osaka
- Capture confirmed approaches the user validated
- Store stable personal facts in PERSONAL so other projects can recall them
- Keep local paths, project architecture, incidents and project-only preferences in MEMORY or USER
- Do not store credentials, API keys or other secrets in PERSONAL
- Do not copy existing project facts into PERSONAL without evidence that they apply across projects

Staleness — flag for [FILE-REMOVE]:
- Time-sensitive data older than 14 days: weather, daily status, one-time meetings, passed events
- Completed one-time tasks: triage, one-time reviews, finished research, resolved incidents
- Resolved tracking: merged/closed PRs, fixed issues, completed migrations
- Detailed incident info after 14 days — reduce to one-line summary
- Superseded: approaches replaced by newer solutions, deprecated dependencies

Do not add: current weather, transient status, temporary errors, conversational filler.

[SKIP] if nothing needs updating.
