# Modular Workflow API Contract

Generated on 2026-07-22  
Parent design: `docs/modular-research-workflow-design.md`  
Storage contract: `docs/modular-workflow-storage-contract.md`  
Status: PROPOSED  
Scope: engine REST, WebSocket, and CLI parity

Decision note: the Project-sequence reconnect model was approved on 2026-07-22, and the initial v1 API vertical slice is implemented in the current worktree.

Implementation status: versioned Project/Run/Event/Artifact/Checkpoint REST routes, Project commands, executor signals, Project-sequence WebSocket replay, transient activity, multi-subscriber registration, CLI parity, structured interaction widgets, handoff policy, and mira-ui consumption are implemented in the current worktree. Legacy conversion is still proposed.

## Decision

The modular API uses three adapters over one `WorkflowService`:

```text
                         +-------------------+
REST command ---------->|                   |
CLI command ----------->|  WorkflowService  |----> JSON workflow store
executor callback ----->|                   |----> Project events.jsonl
                         +-------------------+
                                  |
                                  v
                         WebSocket event fanout
```

- REST submits mutations and returns typed receipts or typed errors.
- WebSocket subscribes to durable Project events and transient Run streams; it does not own workflow mutations.
- CLI invokes the same command models and `WorkflowService` in-process. It does not reimplement transition rules or require the UI server.
- Existing `/api/plan`, `/api/sessions/...`, and legacy `/ws` messages remain on the legacy path during migration.

## Protocol boundary

New REST routes live under `/api/workflow/v1`. The new WebSocket lives at `/ws/workflow/v1`. A separate path avoids adding another set of branches to the current project/session-oriented WebSocket handler.

Every request and message carries `workflow_contract_version: 1`. URI `v1` is the breaking transport version; the contract field supports mira/mira-ui compatibility checks and additive feature negotiation within that version.

## Contract discovery

```text
GET /api/workflow/v1/contract
```

Response:

```json
{
  "transport_version": "v1",
  "workflow_contract_version": 1,
  "minimum_client_contract_version": 1,
  "maximum_client_contract_version": 1,
  "features": [
    "project_event_replay",
    "run_commands",
    "structured_interactions",
    "artifact_import"
  ],
  "module_types": ["initiation", "planning", "experiment", "delivery"],
  "event_retention": { "mode": "full_project_history" }
}
```

mira-ui checks this response and its packaged `compatibility.json` before enabling modular UI. An unsupported contract leaves legacy Projects usable and shows an engine-upgrade message rather than attempting partial behavior.

## Resource endpoints

```text
GET    /api/workflow/v1/projects/{project_id}
POST   /api/workflow/v1/projects/{project_id}/commands

GET    /api/workflow/v1/projects/{project_id}/runs
POST   /api/workflow/v1/projects/{project_id}/runs
GET    /api/workflow/v1/projects/{project_id}/runs/{run_id}
POST   /api/workflow/v1/projects/{project_id}/runs/{run_id}/commands

GET    /api/workflow/v1/projects/{project_id}/events
GET    /api/workflow/v1/projects/{project_id}/runs/{run_id}/events

GET    /api/workflow/v1/projects/{project_id}/artifacts
POST   /api/workflow/v1/projects/{project_id}/artifacts/import
GET    /api/workflow/v1/projects/{project_id}/artifacts/{artifact_id}
GET    /api/workflow/v1/projects/{project_id}/artifacts/{artifact_id}/content

GET    /api/workflow/v1/projects/{project_id}/recovery
POST   /api/workflow/v1/projects/{project_id}/legacy-conversion
```

All nested IDs are validated against the resolved Project. A valid Run or Artifact ID from another Project returns `404`, not metadata about the other Project.

### Project snapshot

`GET .../projects/{project_id}` returns Project workflow metadata, the rebuilt module index, the latest `project_sequence`, recovery health, and links. It does not embed full Run, conversation, Artifact payload, or event histories.

### Run list

`GET .../runs` supports `module_type`, `status`, `derived_from_artifact_id`, `updated_after`, `cursor`, and `limit`. Default limit is 50; maximum is 200. Cursor pagination is based on stable `(updated_at, run_id)` ordering and does not use page numbers.

### Run snapshot

`GET .../runs/{run_id}` returns the validated `module.json` projection, selected Artifact metadata summaries, outstanding interaction, links, and the latest Project/Run sequences. Conversation history remains a separate paged resource in a later adapter over `SessionManager`.

## Creating a Run

```text
POST /api/workflow/v1/projects/{project_id}/runs
```

```json
{
  "workflow_contract_version": 1,
  "command_id": "uuid",
  "expected_project_revision": 12,
  "module_type": "planning",
  "label": "Planning 2",
  "input_artifacts": [
    {
      "artifact_id": "artifact-direction-a",
      "expected_type": "direction_candidate",
      "supported_schema": ">=1,<2"
    }
  ],
  "initial_request": {
    "kind": "free_text",
    "content": "Turn this direction into a testable research plan."
  },
  "start": true
}
```

`start: false` creates a `created` draft. `start: true` atomically creates it and enqueues it after validation. Creation never mutates the source Run or Artifact.

Response is a `CommandReceipt` plus the created Run link. Repeating the same `command_id` with the same canonical payload returns the original receipt. Reusing it with a different payload returns `command_id_conflict`.

## Command envelope

Project and Run mutations share one envelope:

```json
{
  "workflow_contract_version": 1,
  "command_id": "uuid",
  "command_type": "pause",
  "expected_revision": 8,
  "requested_at": "2026-07-22T11:00:00Z",
  "actor": {
    "kind": "user",
    "actor_id": "local-user"
  },
  "payload": {}
}
```

The engine supplies trusted server-side actor metadata when authentication exists. Client-provided `actor_id` is descriptive in local mode and must not be treated as authorization.

`requested_at` is informational client metadata. Server `accepted_at` and durable event `occurred_at` determine ordering; a skewed client clock cannot reorder commands or events.

### Project commands

| Command | Allowed state | Result |
|---|---|---|
| `set_handoff_policy` | active | Validates and replaces Project handoff rules |
| `archive` | active | Prevents new Runs and mutations; keeps read/export |
| `restore` | archived | Returns the Project to active |

Legacy conversion uses its explicit endpoint because it has backup, dry-run, and migration-report semantics rather than an ordinary state transition.

### Run commands

| Command | Allowed state | Important payload/result |
|---|---|---|
| `update_draft` | created | Label, inputs, and initial request; increments revision |
| `enqueue` | created | Validates inputs and creates the first Attempt |
| `pause` | queued, running, waiting_input | Immediate in queued/waiting state; persists `pending_control=pause` until a running executor reaches a safe boundary |
| `resume` | paused | Enqueues from compatible checkpoint |
| `cancel` | any non-terminal state | Immediate unless running; a running Run persists `pending_control=cancel` until its safe boundary while external work is best-effort cancelled |
| `retry` | failed | Creates a new immutable Attempt and enqueues |
| `submit_interaction` | waiting_input | Requires outstanding `interaction_id` and schema-valid response |
| `create_handoff` | completed | Selects output ArtifactRefs and creates/suggests a downstream Run |

There is no generic command that writes arbitrary status. The WorkflowService owns legal transitions.

A running cancellation has a bounded grace period. If cooperative cancellation does not reach a safe boundary before the deadline, the engine detaches the Attempt, marks the Run cancelled, ignores any late Attempt outputs, and records whether the external process/tool could be terminated. It must never publish late outputs into a cancelled Run.

User discussion enters through `submit_interaction`. Executors emit a typed `interaction.requested` event containing an `interaction_id`, prompt, response schema, and optional UI hints. This replaces module-specific transport messages such as `plan_answer` and `plan_decision` without forcing every interaction into free text.

## Command receipts

```json
{
  "workflow_contract_version": 1,
  "command_id": "uuid",
  "command_type": "pause",
  "status": "pending_safe_boundary",
  "project_id": "PRJ-001",
  "run_id": "run-uuid",
  "accepted_at": "2026-07-22T11:00:00Z",
  "resulting_revision": 9,
  "latest_project_sequence": 103,
  "links": {
    "run": "/api/workflow/v1/projects/PRJ-001/runs/run-uuid",
    "events": "/api/workflow/v1/projects/PRJ-001/events?after=103"
  }
}
```

Receipt statuses:

- `applied`: synchronous state mutation is durable;
- `accepted`: async work has been durably queued;
- `pending_safe_boundary`: pause/cancel is waiting for a running executor boundary;
- `duplicate`: original result returned without repeating work;
- `rejected`: represented through a typed non-2xx error response.

HTTP status is `200` for applied/duplicate, `201` for Run creation, and `202` for accepted/pending async work.

## Error envelope

Every REST and WebSocket protocol error uses the same shape:

```json
{
  "error": {
    "code": "stale_revision",
    "message": "Run changed after this view was loaded.",
    "retryable": true,
    "correlation_id": "uuid",
    "details": {
      "expected_revision": 8,
      "current_revision": 9
    }
  }
}
```

| HTTP | Code examples | Client behavior |
|---|---|---|
| 400 | `invalid_request`, `command_id_conflict` | Fix request; do not retry automatically |
| 404 | `project_not_found`, `run_not_found`, `artifact_not_found` | Refresh navigation |
| 409 | `stale_revision`, `invalid_transition`, `interaction_expired` | Refetch snapshot; preserve user input |
| 422 | `input_contract_failed`, `artifact_schema_unsupported` | Show field/Artifact validation details |
| 423 | `project_busy`, `project_read_only` | Retry lock timeouts with bounded backoff or ask user to upgrade/repair |
| 429 | `command_rate_limited` | Honor `Retry-After` |
| 500 | `workflow_internal_error` | Show correlation ID; never expose paths, prompts, or secrets |
| 503 | `executor_unavailable` | Keep Run queued/paused and offer retry |

## Durable event envelope

```json
{
  "message_type": "workflow_event",
  "workflow_contract_version": 1,
  "event_id": "uuid",
  "project_id": "PRJ-001",
  "project_sequence": 104,
  "run_id": "run-uuid",
  "run_sequence": 18,
  "event_type": "run.completed",
  "occurred_at": "2026-07-22T11:02:00Z",
  "project_revision": 13,
  "run_revision": 10,
  "command_id": "uuid-or-null",
  "correlation_id": "uuid",
  "payload_schema_version": 1,
  "payload": {
    "output_artifact_ids": ["artifact-uuid"]
  }
}
```

Project-only events omit `run_id`, `run_sequence`, and `run_revision`. Event payloads contain summaries and references, not full Artifact payloads, provider messages, or secrets.

### Durable event taxonomy

- Project: `project.workflow_enabled`, `project.policy_updated`, `project.archived`, `project.restored`.
- Run: `run.created`, `run.queued`, `run.started`, `run.pause_requested`, `run.paused`, `run.waiting_input`, `run.completed`, `run.failed`, `run.cancelled`, `run.retry_scheduled`.
- Interaction: `interaction.requested`, `interaction.accepted`, `interaction.expired`.
- Artifact: `artifact.created`, `artifact.imported`, `artifact.damaged`.
- Handoff: `handoff.suggested`, `handoff.enqueued`, `handoff.cancelled`.
- Recovery/migration: `recovery.completed`, `legacy.conversion_completed`, `legacy.conversion_failed`.

Event names describe facts that already became durable. Imperative names belong to commands.

## Transient WebSocket envelopes

Transient messages are not stored or replayed:

```json
{
  "message_type": "activity",
  "project_id": "PRJ-001",
  "run_id": "run-uuid",
  "activity_id": "uuid",
  "phase": "updated",
  "summary": "Searching recent protein-folding papers...",
  "occurred_at": "2026-07-22T11:01:00Z"
}
```

`summary` is the single-line, truncated current-operation text used by mira-ui. Other transient message types are `stream_delta`, `stream_end`, and `heartbeat`. A reconnect clears stale activity and derives Run status from durable snapshots/events; it does not replay old thinking indicators or token deltas.

## WebSocket subscription and replay

Client messages on `/ws/workflow/v1` are protocol controls only:

```json
{
  "message_type": "subscribe",
  "workflow_contract_version": 1,
  "subscription_id": "uuid",
  "project_id": "PRJ-001",
  "after_project_sequence": 99,
  "include_transient": true
}
```

Server behavior:

1. Validate Project and contract.
2. Register the subscriber before replay so a concurrent new event cannot create a gap.
3. Replay durable events after sequence 99 up to the captured watermark.
4. Deliver buffered/live events after the watermark.
5. Allow duplicates across registration/replay boundaries; never deliver a lower sequence after a higher one on the same connection.

The client applies events at least once: it ignores any `project_sequence <= last_applied_sequence`. Duplicate delivery is allowed; missing durable sequence numbers are not. If the cursor is greater than the server watermark, belongs to another Project, or is no longer retained, the server sends `snapshot_required`; the client refetches the Project snapshot and subscribes from its returned sequence.

One socket may hold multiple Project subscriptions, although mira-ui v1 normally keeps only the selected Project active. The server stores a set of subscribers per Project; one window must not replace another window's connection.

## REST event queries

```text
GET /api/workflow/v1/projects/{project_id}/events?after=99&limit=200
GET /api/workflow/v1/projects/{project_id}/runs/{run_id}/events?after_run_sequence=17&limit=100
```

The first endpoint reads the canonical Project event order. The second is a filtered view over the same log and returns both Project and Run sequences. Default limit is 100; maximum is 500. Responses include `next_after`, `has_more`, and the current watermark.

## Artifact import and content

Imports use multipart upload or an already uploaded Project-relative file reference. The request includes `command_id`, expected Project revision, Artifact type/schema version, and optional provenance. The engine copies or safely references the payload, computes its own hash and size, validates paths, and returns immutable Artifact metadata.

Artifact content responses set an explicit media type and safe `Content-Disposition`. Inline rendering is allowlisted by media type; unknown or active content downloads as an attachment. Range requests for large payloads may be added later without changing Artifact identity.

## CLI parity

```text
mira workflow project show <project>
mira workflow project policy <project> ...
mira workflow run list <project> [--type ...] [--status ...]
mira workflow run create <project> --type initiation|planning|experiment|delivery ...
mira workflow run show <project> <run>
mira workflow run enqueue|pause|resume|cancel|retry <project> <run>
mira workflow run respond <project> <run> --interaction <id> ...
mira workflow events <project> [--after N] [--follow]
mira workflow artifact list|show|import|export ...
```

CLI commands construct the same Pydantic command models and call `WorkflowService`. `--json` prints the REST-compatible envelope for automation; human output is a projection of the same result. Exit codes are stable by error category: success `0`, validation `2`, conflict `3`, busy/unavailable `4`, internal `1`.

## Legacy coexistence

- Existing WebSocket messages `message`, `plan_answer`, and `plan_decision` remain supported only for legacy Projects.
- Modular Projects reject legacy Plan mutations with `project_uses_modular_workflow` and a link to the new contract.
- mira-ui selects legacy or modular adapters once per opened Project; it must not send both protocols for one mutation.
- New endpoints are feature-flagged until storage, replay, and compatibility tests pass in packaged builds.

## Failure and test matrix

| Failure | Handling | Required test | User result |
|---|---|---|---|
| Duplicate REST submission | Return original receipt | repeated identical and conflicting command IDs | No repeated work |
| Stale UI revision | Return 409 with current revision | two-client race | Input preserved; refresh offered |
| Socket disconnect during replay | Resume from last applied Project sequence | disconnect at every replay boundary | No missing durable event |
| Live event during replay | Buffer or duplicate, never gap | concurrent append/replay test | Client deduplicates |
| Two UI windows | Fan out to both subscribers | multiple-socket integration | Neither window steals connection |
| Malformed/unknown event | Reject unsupported contract or isolate payload | future-event fixture | Upgrade/error message, no crash |
| Slow subscriber | Bound queue and disconnect with resumable cursor | backpressure integration | Reconnect notice, no engine stall |
| Invalid cross-Project ID | Return 404 | Run/Artifact scope tests | No data disclosure |
| Executor unavailable | Leave Run queued/paused and return typed error/event | executor failure test | Retry control remains available |
| Transient activity lost | Clear on reconnect; use durable Run state | reconnect UI test | No permanently stuck indicator |

```text
COMMAND PATHS                                      COVERAGE REQUIRED
REST -> command model -> WorkflowService
  +-- apply / reject / duplicate / stale           unit + API integration
CLI -> same command model -> WorkflowService
  +-- human and --json output                      CLI integration

EVENT PATHS
store append -> project sequence -> dispatcher
  +-- REST pagination                              integration
  +-- WS replay -> live handoff                    concurrency integration
  +-- duplicate boundary                           client reducer unit
  +-- disconnect/reconnect                         end-to-end

USER FLOWS
create direct-entry Run -> watch -> interact       end-to-end
pause running Run -> safe boundary -> resume       end-to-end
automatic handoff creates unknown new Run          end-to-end
two windows observe same Project                   integration
legacy Project remains on old protocol             regression
```

## Performance and limits

- Durable event payload target is under 64 KiB; large data belongs in Artifacts.
- WebSocket subscriber queues are bounded to 1,000 durable envelopes by default. Transient activity may be coalesced or dropped first; durable overflow closes the subscriber with `replay_required` and its last sent Project sequence instead of blocking persistence or other subscribers.
- Event replay is paged in batches and yields to the event loop between batches.
- Token deltas and activity updates may be coalesced; durable workflow events may not be dropped.
- A replay benchmark covers 100,000 durable Project events and at least two simultaneous subscribers.

## Minimal implementation boundary

First protocol PR after the storage PR:

1. Add command, receipt, error, and event envelope models beside workflow storage models.
2. Add `WorkflowService` command dispatch without any real module executor beyond a test/fake executor.
3. Add versioned REST Project/Run snapshot, create-Run, command, and event endpoints.
4. Add the separate versioned WebSocket subscription/replay handler and multi-subscriber registry.
5. Add the `mira workflow` CLI group over the same service.
6. Add engine contract, concurrency, replay, CLI parity, and legacy-regression tests.

Artifact import/content, executor activity streaming, recovery UI, structured interaction widgets, and Project policy commands are implemented. Legacy conversion remains a later vertical slice.

## What already exists

- `mira_engine/channels/ui.py`: reuse aiohttp server lifecycle, CORS policy, Project resolution, audit hooks, and streaming integration; do not extend the monolithic legacy message switch for workflow v1.
- `mira_engine/session/manager.py`: reuse Project-local JSONL conversation persistence with Run-scoped session keys.
- `mira_engine/cli/commands.py`: reuse Typer and existing engine configuration/bootstrap; add a subcommand group instead of a second CLI executable.
- `mira-ui/src/services/websocket.ts`: reuse reconnect timing/status reporting, but add subscription cursors, deduplication, multiple envelope types, and explicit malformed-message reporting.
- Existing compatibility manifest: extend with `workflow_contract_version` and version-bounded fixtures.

## Not in scope

- Remote multi-user authentication and authorization; the first contract remains local-engine scoped. Multi-user deployments are handled by Mira Server in front of one engine per user, which speaks this contract unchanged to each engine (see `docs/server-edition/architecture.md`).
- Exactly-once WebSocket delivery; durable events are at-least-once and client application is idempotent.
- GraphQL, gRPC, SSE, or an external message broker.
- Persisting token deltas or activity pings.
- Event-log compaction in v1; only durable domain events enter the log.
- Multiple engine processes writing one Project.
