# Modular Workflow Storage Contract

Generated on 2026-07-22  
Parent design: `docs/modular-research-workflow-design.md`  
Status: PROPOSED  
Scope: storage contract only

Implementation status: the JSON Project/Run/Event/Artifact/Checkpoint repository, recovery reconciliation, append-only attempt history, lifecycle service, REST/WS adapters, CLI, and mira-ui vertical slice are implemented in the current worktree. Schema generation and legacy conversion remain follow-up work.

## Decision

The first modular workflow release uses versioned JSON documents and Project-local files. Each ModuleRun owns an authoritative `module.json`. The existing `.mira/project.json` remains the Project manifest and gains a nested `workflow` section. SQLite is hidden behind a future repository implementation, not introduced now.

## Invariants

1. Exactly one `module.json` is authoritative for one ModuleRun.
2. `project.json.workflow.module_index` is a disposable overview cache, never the authority for a Run transition.
3. A Run references immutable Artifacts by ID. It never embeds another Run's mutable state.
4. All mutation commands pass through the engine and acquire the existing `project.json.lock` sidecar.
5. Every mutable JSON document has a monotonic `revision`; callers provide `expected_revision` when changing it.
6. A command ID is applied at most once. Repeating the same command returns the recorded result rather than repeating its side effect.
7. Files are written to a temporary sibling, flushed, synced, and replaced on the same filesystem; the parent directory is synced where the platform supports it.
8. WebSocket events are published only after the authoritative snapshot is durable.
9. Unknown additive fields survive read-write cycles. Unknown enum values or unsupported major versions fail visibly.
10. Startup recovery may repair indexes and audit logs, but may not invent scientific outputs.

## Canonical layout

```text
<project>/.mira/
  project.json
  project.json.lock
  events.jsonl
  modules/
    <run-id>/
      module.json
      checkpoint.json
      attempts/
        <attempt-id>.json
  artifacts/
    <artifact-id>/
      artifact.json
      payload.<ext>
  staging/
    <attempt-id>/
      completion-manifest.json
      outputs/...
  recovery/
    orphans/...
  backups/
    metadata-v<old>-<timestamp>/...
```

Run IDs, Attempt IDs, Artifact IDs, Command IDs, and Event IDs are opaque UUID4 strings in v1. Human-readable labels such as `Planning 2` are presentation metadata and are not identity. Timestamps are UTC RFC 3339 strings. Persisted paths are relative to the Project root and must resolve inside it after normalization.

## Schema ownership

Pydantic 2 models are the source for runtime validation and generated JSON Schema. Models use strict field types plus `extra="allow"` so compatible unknown additive fields survive read-write cycles instead of disappearing. Generated schemas are committed under `mira_engine/workflows/schemas/`, included in release packages, and protected by a no-drift test so mira-ui and external clients can validate the same contract. The following versions remain distinct:

- `project.json.schema_version`: physical Project-manifest format;
- `workflow.contract_version`: workflow API and cross-repo compatibility contract;
- each document's `schema_version`: physical format for that document type;
- each Artifact's `artifact_schema_version`: semantic format of its payload.

## `project.json` workflow section

Existing fields such as `id`, `project_dir`, `display_name`, `run_mode`, `agent_profile`, `contract_version`, `automation_policy`, `created_at`, and `updated_at` remain readable.

```json
{
  "schema_version": 2,
  "id": "PRJ-001",
  "project_dir": "/absolute/path/used-only-locally",
  "display_name": "Protein folding study",
  "run_mode": "manual",
  "agent_profile": "research",
  "contract_version": 2,
  "created_at": "2026-07-22T10:00:00Z",
  "updated_at": "2026-07-22T10:20:00Z",
  "workflow": {
    "contract_version": 1,
    "status": "active",
    "revision": 12,
    "last_project_sequence": 42,
    "handoff_policy": {
      "mode": "manual",
      "interrupt_grace_seconds": 10,
      "rules": []
    },
    "selected_run_id": "opaque-run-id",
    "module_index": [
      {
        "run_id": "opaque-run-id",
        "module_type": "planning",
        "label": "Planning 2",
        "status": "waiting_input",
        "revision": 4,
        "updated_at": "2026-07-22T10:20:00Z"
      }
    ]
  }
}
```

Allowed Project workflow states in v1 are `active` and `archived`. Archiving prevents new Runs and new commands but preserves read/export access. Project deletion remains an explicit filesystem-level user operation outside this contract.

## `module.json`

Common lifecycle fields stay outside module-specific data so the workflow service can manage every module uniformly.

```json
{
  "schema_version": 1,
  "workflow_contract_version": 1,
  "run_id": "opaque-run-id",
  "project_id": "PRJ-001",
  "module_type": "planning",
  "label": "Planning 2",
  "status": "waiting_input",
  "revision": 4,
  "created_at": "2026-07-22T10:10:00Z",
  "updated_at": "2026-07-22T10:20:00Z",
  "created_by": { "kind": "user" },
  "inputs": [
    {
      "artifact_id": "artifact-direction-a",
      "expected_type": "direction_candidate",
      "supported_schema": ">=1,<2"
    }
  ],
  "outputs": [],
  "attempt": {
    "current_attempt_id": "opaque-attempt-id",
    "number": 1,
    "lease_owner": null,
    "lease_expires_at": null
  },
  "pending_control": null,
  "conversation_key": "ui:PRJ-001:run:<run-id>",
  "checkpoint_ref": "checkpoint.json",
  "last_project_sequence": 42,
  "last_run_sequence": 17,
  "last_transition": {
    "event_id": "opaque-event-id",
    "event_type": "run.waiting_for_input",
    "from": "running",
    "to": "waiting_input",
    "occurred_at": "2026-07-22T10:20:00Z"
  },
  "applied_commands": [
    {
      "command_id": "opaque-command-id",
      "command_type": "submit_response",
      "result_revision": 4
    }
  ],
  "module_data": {
    "input_request": {},
    "current_prompt": {},
    "suggested_next_actions": []
  }
}
```

`module_data` is validated by a module-specific discriminated model selected by `module_type`. Executors may update only `module_data`, checkpoints, and staged outputs. The workflow service alone updates common lifecycle fields.

`applied_commands` is bounded to the newest 100 entries in the snapshot. Older immutable command results remain in the Project `events.jsonl`, so duplicate detection outside the snapshot window scans the event file before accepting the command.

## Artifact document

```json
{
  "schema_version": 1,
  "artifact_id": "opaque-artifact-id",
  "artifact_type": "approved_plan",
  "artifact_schema_version": 1,
  "media_type": "application/json",
  "byte_size": 18420,
  "sha256": "hex-digest",
  "payload_ref": "artifacts/<artifact-id>/payload.json",
  "created_at": "2026-07-22T10:30:00Z",
  "created_by": {
    "kind": "module_run",
    "run_id": "opaque-run-id",
    "attempt_id": "opaque-attempt-id"
  },
  "derived_from": ["artifact-direction-a"]
}
```

External imports use `created_by.kind = external_import` plus source filename and import timestamp. Absolute source paths, credentials, provider request bodies, and secrets must not enter Artifact metadata.

## Event and checkpoint documents

Every Project `events.jsonl` line is a complete JSON object with `schema_version`, `event_id`, monotonic `project_sequence`, optional `run_id` and monotonic per-Run `run_sequence`, `event_type`, `occurred_at`, Project/Run revision, command ID, and a typed payload. It provides one durable order across Run creation, handoffs, Artifacts, recovery, and lifecycle transitions. Events are audit and replay records, not the lifecycle authority. Project workflow metadata stores the latest Project sequence; each Run snapshot stores its latest Project and Run sequences.

High-frequency transport messages such as token deltas, activity pings, and animation progress are not written to this log. Durable workflow events and transient stream messages use separate WebSocket envelope types.

`checkpoint.json` contains `schema_version`, `executor_type`, `executor_version`, `run_id`, `attempt_id`, `created_at`, `compatible_module_revision`, conversation cursor, resumable tool state, and module-specific checkpoint data. Secrets and raw provider credentials are forbidden. An incompatible executor version moves the Run to `paused` with a visible recovery reason.

## Mutation protocol

```text
[API/CLI command]
       |
       v
[acquire project.json.lock]
       |
       +--> reload + validate project/module/artifact refs
       +--> reject stale expected_revision
       +--> return prior result for duplicate command_id
       +--> validate legal state transition
       +--> reserve next Project/Run sequence under the lock
       |
       v
[write authoritative snapshot with sequence + last_transition]
[flush -> fsync -> os.replace -> sync parent dir where supported]
       |
       v
[append/flush/fsync event, then refresh disposable project index]
       |
       v
[release lock] -> [publish WebSocket event]
```

If the snapshot succeeds and event append fails, `last_transition` lets recovery recreate the missing event at its reserved sequence. If event append succeeds but the Project watermark/index refresh fails, recovery advances them from the log and snapshots. The engine does not expose a later sequence until the missing reserved event has been repaired, so clients never accept a permanent sequence gap. If the index refresh alone fails, the next read rebuilds it by scanning `modules/*/module.json`. None of these projection failures rolls back the authoritative ModuleRun state.

For a running pause or cancellation request, `module.json.pending_control` is set to `pause` or `cancel` and its request event becomes durable before the command returns. The executor clears it when it reaches a safe boundary and commits the resulting state. Engine restart therefore preserves pending control intent.

### Creating a Run

1. Validate selected input ArtifactRefs and module-specific minimum input set.
2. Create a complete Run directory under `staging/`.
3. Acquire the Project lock and revalidate the Project revision and inputs.
4. Rename the staged directory into `modules/<run-id>/` on the same filesystem.
5. Refresh the Project index and release the lock.
6. Publish `run.created` only after the directory is durable.

An unindexed Run directory is discoverable and added during reconciliation. An index entry with no Run directory is removed and reported as damaged metadata.

### Completing a Run

1. Executor writes payloads and `completion-manifest.json` under the Attempt staging directory.
2. Workflow service validates hashes, schemas, provenance, and the Run's current Attempt.
3. Under the Project lock, rename immutable Artifact directories into place.
4. Atomically replace `module.json` with ArtifactRefs and `completed` status.
5. Append the completion event, refresh the Project index, and remove the staging manifest.

If a crash occurs after step 3 but before step 4, recovery uses the manifest to finish only when Run ID, Attempt ID, expected revision, and every hash still match. Otherwise it quarantines the unreferenced Artifacts for explicit inspection.

## Recovery protocol

Startup recovery runs before accepting mutation commands:

1. Validate `project.json`; unsupported versions open read-only with a visible compatibility error.
2. Scan Run directories and validate each `module.json` independently.
3. Rebuild the Project index when IDs, revisions, statuses, or timestamps differ.
4. Repair a truncated final JSONL line, recreate missing reserved transition events, and advance a stale Project sequence watermark.
5. Move expired `running` leases to `paused` with `engine_restart` as the reason.
6. Reconcile completion manifests and orphan Artifact directories.
7. Leave corrupted Runs isolated and visible; healthy Runs remain usable.

Recovery produces a report stored outside ModuleRun scientific data and exposed through the API/UI. It never deletes an unreadable file automatically; it moves recoverable garbage to `recovery/orphans/`.

## Schema migration

- Migrations are explicit `vN -> vN+1` functions and never skip versions.
- Before the first write, copy metadata JSON/JSONL files, but not large payloads, to `backups/metadata-vN-<timestamp>/`.
- Migrate one Project under the Project lock. Validate every produced document before replacing the originals.
- On failure, keep the Project read-only, restore nothing automatically, and show the backup and failing document paths.
- Artifact semantic migrations create a new derived Artifact; they do not rewrite the old payload.
- The engine may read the immediately previous workflow contract during a compatibility window but writes only its current contract.

## Failure and test matrix

| Failure | Required handling | Required test | User-visible result |
|---|---|---|---|
| Lock timeout | Fail closed without writing | competing-process lock test | Retryable `project_busy` error |
| Stale revision | Reject command | two stale writers test | Refresh-and-retry message |
| Duplicate command | Return recorded result | repeat each command type | No duplicate Run/tool/handoff |
| Crash before replace | Ignore/remove temp file | injected crash point | Previous valid state remains |
| Crash after snapshot, before event | Recreate event from snapshot | injected crash point | Recovery notice, no lost state |
| Stale Project index | Rebuild from Run files | corrupt index fixture | Project opens with repaired overview |
| Truncated JSONL tail | Drop only invalid tail | partial-line fixture | Recovery notice, history preserved |
| Missing Artifact payload | Quarantine reference and block consumer | missing-file fixture | Damaged Artifact shown explicitly |
| Unsupported schema | Open affected scope read-only | future-version fixture | Upgrade-required message |
| Expired execution lease | Move Run to paused | restart/clock fixture | Resume or restart controls |
| Path traversal in payload ref | Reject during validation | malicious path fixture | Invalid Project data warning |

```text
STORAGE CODE PATHS                              COVERAGE REQUIRED
load_project
  +-- valid current schema                      unit
  +-- previous schema -> migrate                unit + fixture
  +-- future schema -> read-only                unit + API integration
load_run
  +-- valid run                                 unit
  +-- one corrupt run among healthy runs        integration
mutate_run
  +-- legal transition                          unit
  +-- stale revision / duplicate command        unit + concurrency
  +-- lock timeout                              multiprocess
commit_outputs
  +-- valid manifest                            integration
  +-- crash at every persistence boundary       fault-injection
recover_project
  +-- stale index / event tail / expired lease  fixture + integration
```

## Performance limits for v1

- Project overview may scan Run snapshots when the index is absent or stale; normal reads use the cached index.
- Reconciliation is O(R + A) for Runs and Artifact metadata and must not hash every payload on every startup. Payload hashes are rechecked only for new commits, explicit verification, or suspicious recovery cases.
- Project `events.jsonl` is read from a requested Project sequence offset; the UI does not load an entire long history by default. Run-history queries filter by `run_id` using an in-memory byte-offset index rebuilt at startup rather than persisting a second event log.
- Add a benchmark fixture with 1,000 Runs and 10,000 Artifact metadata files. Initial desktop targets are under 1 second for a cold Project overview rebuild and under 3 seconds for full metadata recovery on each supported OS. Re-evaluate SQLite when either target is missed consistently in representative packaged builds; do not hide the cost by weakening recovery checks.

## Minimal implementation boundary

First storage PR:

1. Add `mira_engine/workflows/models.py` for Pydantic document models and deterministic schema export, plus committed generated schemas with a no-drift test.
2. Add `mira_engine/workflows/store.py` for layout, locking, atomic writes, lifecycle persistence, index rebuild, and recovery.
3. Extend `mira_engine/utils/locks.py` with durable atomic replacement while preserving current callers.
4. Evolve `mira_engine/projects.py` to preserve and expose the nested workflow manifest.
5. Add focused model, store, recovery, migration, concurrency, and crash-point tests.

The storage layer does not itself define executor prompts, UI presentation, or legacy conversion behavior. Those remain consumers of this contract and must not bypass its mutation protocol.

## What already exists

- `mira_engine/utils/locks.py`: reuse `filelock`, fail-closed lock timeouts, and sibling temporary replacement; add durability support.
- `mira_engine/projects.py`: reuse Project identity, `.mira/project.json`, schema version, and merge-preserving metadata behavior.
- `mira_engine/session/manager.py`: reuse its JSONL parsing patterns for conversation storage, but keep workflow lifecycle events separate from chat messages.
- Pydantic 2 dependency: reuse for strict typed parsing and JSON Schema generation.

## Not in scope

- SQLite or a JSON-plus-SQLite cache: no measured need in the first release.
- Multiple engine writers or collaborative live editing: requires a different coordination model.
- Executor extraction and prompt behavior: storage must land without changing scientific behavior.
- REST/WebSocket/UI implementation: checkpoint recovery, structured interaction, and handoff-policy UI are implemented; legacy conversion remains.
- Per-Artifact deletion or retention: provenance safety comes first.
