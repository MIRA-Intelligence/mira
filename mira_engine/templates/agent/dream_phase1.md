Compare conversation history against current memory files. Also scan memory files for stale content — even if not mentioned in history.

Output one line per finding:
[FILE] atomic fact (not already in memory)
[FILE-REMOVE] reason for removal

Files: PERSONAL (reusable workflows and cross-project knowledge), USER (stable user identity, preferences and interests), SOUL (bot behavior, tone), MEMORY (project facts and decisions)

Rules:
- Atomic facts: "has a cat named Luna" not "discussed pet care"
- Corrections: [USER] location is Tokyo, not Osaka
- Capture confirmed approaches the user validated
- Store stable identity, preferences and interests in shared USER.md, never PERSONAL
- Keep local paths, project architecture, incidents and project-only preferences in MEMORY
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

USER.local.md settings and project USER.md overlays take precedence over learned shared USER.md.
Never duplicate name, timezone or language already set by Settings into USER.md or PERSONAL.
Never change USER.local.md. Keep USER.md preferences under ### Communication Style,
## Topics of Interest, and - **Preferred language(s)**: ... for community persona compatibility.
Community sharing must continue through PrivacyGuard and user approval.

Preserve existing template headings and edit their bullets; do not create duplicate sections.
