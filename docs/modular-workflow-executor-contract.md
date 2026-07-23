# Modular Workflow Executor Contract

Generated on 2026-07-22  
Parent design: `docs/modular-research-workflow-design.md`  
Storage contract: `docs/modular-workflow-storage-contract.md`  
API contract: `docs/modular-workflow-api-contract.md`  
Status: IMPLEMENTED V1 — hardening items remain
Scope: module execution, pause/resume, interaction, checkpointing, and output publication

Implementation status: four concrete native executors (`mira.initiation`, `mira.planning`, `mira.experiment`, and `mira.delivery`) now own validation, prompts, and tool capabilities independently. ModuleRun messages bypass the legacy `ResearchAgentLoop` orchestration and use only the shared conversation engine. Research, engineer, and team profile policies are composed into each executor without their legacy `set_plan`/`task_plan` ordering; the effective profile is snapshotted on every attempt, team-only tools are capability-gated, and skill audit events carry Project/Run/executor/profile identity. Attempts and checkpoints persist executor identity/version; structured interaction, Artifact publication, completion, and cooperative pause/cancel are wired end to end. Atomic staged-output manifests, heartbeats, forced-control workers, and checkpoint-version migration remain hardening work.

## Decision

Each canonical module is an independent executor behind one cooperative protocol. An executor performs module-specific work, but `WorkflowService` remains the only authority allowed to:

- change Run lifecycle state;
- reserve Project or Run event sequences;
- create attempts and execution leases;
- accept commands and deduplicate command IDs;
- publish durable interactions or Artifacts;
- commit final outputs; or
- evaluate handoff policy.

An executor emits typed signals. The service validates each signal and converts it into snapshots, durable events, transient WebSocket messages, or staged files. This prevents prompt or module code from mutating workflow metadata directly.

```text
Run command -> WorkflowService -> Scheduler -> ModuleExecutor
                   ^                            |
                   |                            v
          snapshots/events/artifacts <- validated signals
```

## Package boundary

```text
mira_engine/
  workflows/
    service.py
    scheduler.py
    controls.py
    signals.py
    repositories.py
  modules/
    base.py
    initiation/
    planning/
    experiment/
    delivery/
```

`mira_engine/modules/base.py` defines the protocol and shared value objects. It must not import UI, HTTP, WebSocket, CLI, or concrete JSON repository classes. Executors depend on narrow context ports supplied by the workflow layer.

## Executor identity and compatibility

Every executor declares:

```json
{
  "module_type": "planning",
  "executor_name": "mira.planning",
  "executor_version": "1.0.0",
  "input_contract_version": 1,
  "checkpoint_contract_version": 1,
  "output_contract_version": 1,
  "capabilities": ["interaction", "checkpoint", "stream_activity"]
}
```

The identity is copied into every attempt and checkpoint. A newer executor may resume an older checkpoint only when it explicitly declares that checkpoint version compatible. Otherwise the service pauses the Run with `checkpoint_incompatible`; it never guesses or silently restarts side effects.

## Protocol

The implementation may use Python protocols and async iterators, but the semantic interface is:

```python
class ModuleExecutor(Protocol):
    descriptor: ExecutorDescriptor

    async def validate_inputs(
        self, context: ValidationContext
    ) -> ValidationResult: ...

    async def execute(
        self, context: ExecutionContext
    ) -> AsyncIterator[ExecutorSignal]: ...
```

`validate_inputs` is deterministic and side-effect free. It validates selected immutable Artifact revisions, user parameters, module-specific schemas, and required local capabilities before the scheduler creates an attempt.

`execute` receives only immutable input descriptors, an attempt-scoped workspace, the latest compatible checkpoint, submitted interaction responses, a conversation port, tool ports, and a cooperative control probe. It cannot receive a repository or mutable `Project`/`ModuleRun` object.

## Attempt lifecycle

An attempt is one scheduler claim of a Run, not the whole lifetime of the Run.

```text
queued/resume/retry
       |
       v
create attempt + lease -> validate -> execute
                              |          |
                         invalid input   +-> waiting_input / paused
                              |          |      lease released
                              v          |
                            failed       +-> completed / failed / cancelled
```

- A new attempt is created whenever execution is claimed after `queued`, `paused`, `waiting_input`, or retryable `failed` state.
- Entering `waiting_input` or `paused` commits a checkpoint and releases the lease.
- Resuming after user input creates a new attempt from that checkpoint; it does not revive an expired process.
- Attempt records are append-only. Run state points to the current/latest attempt.
- A lease heartbeat proves only process ownership. It is not a user-visible progress event.

## Typed executor signals

Only the following signal classes cross the executor boundary in contract version 1:

| Signal | Durable | Meaning |
|---|---:|---|
| `ActivityUpdated` | No | Single-line current operation for the UI; newer activity replaces older activity |
| `StreamDelta` / `StreamEnded` | No | Ephemeral assistant output for the active connection |
| `CheckpointReady` | Yes | Resumable state at a declared safe boundary |
| `InteractionRequested` | Yes | Typed user decision/input is required before work can continue |
| `ArtifactStaged` | Not yet | Candidate output exists in attempt staging; not visible as a Project Artifact |
| `ProgressRecorded` | Yes, sparse | Scientifically meaningful milestone, not token/tool chatter |
| `ExecutionCompleted` | Yes | Final commit manifest proposed for service validation |
| `ExecutionFailed` | Yes | Typed failure, retryability, safe-boundary status, and user-safe explanation |
| `ControlAcknowledged` | Yes | Pending pause/cancel was honored at a checkpoint boundary |

Unknown signal types are rejected and fail the attempt as an executor contract violation. Signals carry no lifecycle status setter and no arbitrary event type string.

## Safe boundaries and external side effects

A safe boundary is a point where all previously reported effects are either durably identified or known not to have happened. Executors must checkpoint:

1. before a non-idempotent, paid, destructive, or long-running external action;
2. after that action returns, including its stable provider/job/result identifier;
3. before requesting user input; and
4. before proposing completion.

Tool calls use `(run_id, attempt_id, operation_id)` as an idempotency key where supported. If a crash occurs after an external action may have started but before its result is durably identified, recovery pauses with `ambiguous_external_effect`. The UI asks the user to inspect, attach, retry, or abandon; the engine does not silently repeat it.

Experiment guardrails remain mandatory inside the Experiment tool port. Moving an experiment into a module executor must not bypass existing confirmation, path, resource, or command restrictions.

## Pause and cancel

`ExecutionControl` exposes the latest durable `pending_control` value without letting the executor mutate it. The executor checks it:

- before starting a tool call;
- after a tool call returns;
- when emitting a checkpoint;
- at bounded intervals during native async work; and
- while waiting on a controllable child process or provider job.

For `pause`, the executor checkpoints and emits `ControlAcknowledged(pause)`. For `cancel`, it best-effort cancels owned external work, checkpoints known effects, and emits `ControlAcknowledged(cancel)`.

If the executor does not acknowledge within the configured grace period, the scheduler detaches or terminates only the attempt-owned worker, records forced control, and rejects late signals using the closed lease token. It must not publish outputs that arrive after forced cancellation.

## Structured interaction

An executor requests interaction with a stable ID and a JSON-schema-compatible response contract:

```json
{
  "interaction_id": "int-uuid",
  "interaction_type": "choose_direction",
  "title": "Choose a research direction",
  "prompt": "Which direction should this Run develop?",
  "response_schema": {
    "type": "object",
    "required": ["direction_artifact_id"],
    "properties": {
      "direction_artifact_id": {"type": "string"},
      "notes": {"type": "string", "maxLength": 4000}
    },
    "additionalProperties": false
  },
  "choices": [],
  "allow_freeform": true
}
```

The service commits the checkpoint and `interaction.requested` event before changing the Run to `waiting_input`. `submit_interaction` validates the response and interaction ID, stores it durably, and queues a new attempt. Duplicate submissions with the same command ID return the original receipt; a second command for an already answered interaction returns `interaction_already_resolved`.

Free-form scientific discussion is represented as a conversational interaction schema, not as an untracked mutation. Conversation text may live in the existing session store, while the Run checkpoint stores the conversation cursor and the durable interaction event stores the decision needed for replay.

## Output staging and atomic publication

Executors write only beneath their attempt-scoped staging directory. `ArtifactStaged` identifies a relative path, Artifact type, schema version, media type, label, content hash, provenance, and source input references. The service verifies the path and hash; the executor cannot point outside staging.

Completion is proposed through a manifest:

```json
{
  "checkpoint_id": "ckpt-uuid",
  "outputs": ["staged-output-1", "staged-output-2"],
  "summary": "Planning completed with an approved experiment specification",
  "suggested_handoffs": [
    {"target_module_type": "experiment", "input_staged_ids": ["staged-output-2"]}
  ]
}
```

Under the Project mutation lock, the service validates the manifest, publishes immutable Artifacts, commits the Run snapshot and completion events, then evaluates handoff policy. A failure before commit leaves the Run non-terminal and staging recoverable or cleanable. An executor never observes a partially published output set.

## Canonical module contracts

The common protocol does not force each module into one prompt or one output shape.

| Module | Minimum direct-entry input | Typical durable outputs | Typical interaction |
|---|---|---|---|
| Initiation | User brief, question, or imported source | literature map, research brief, direction options | select/refine a direction or stop after research |
| Planning | Direction, objective, or user-authored brief | approved plan, experiment specification, risk/acceptance criteria | approve, revise, split, or stop |
| Experiment | Executable specification plus required data/code/environment refs | execution log, result dataset, analysis, failure report | guardrail approval, ambiguity resolution, continue/stop |
| Delivery | Manuscript/draft or selected Project Artifacts plus delivery intent | polished document, report, figures, submission/export package | audience, style, claims, revision approval |

Direct entry is a first-class path. “Minimum input” means sufficient for that executor's validation, not evidence that earlier modules ran. An executor may suggest a missing upstream module but cannot require it when equivalent user-provided input is valid.

## Conversation isolation

Each Run owns a conversation key. A new Run receives only:

- explicitly selected input Artifacts;
- Project-level stable metadata allowed by policy;
- the Run's own conversation history; and
- an optional user-approved handoff summary.

It does not automatically inherit another Run's full transcript or hidden model context. This keeps branches reproducible and prevents accidental coupling through conversation memory.

## Failure taxonomy

Executor failures are typed as:

- `input_invalid` — user-correctable input/schema problem;
- `capability_unavailable` — required provider/tool/runtime absent;
- `checkpoint_incompatible` — explicit migration or restart needed;
- `tool_failed` — known failed effect, optionally retryable;
- `ambiguous_external_effect` — effect may have occurred; user resolution required;
- `executor_contract_violation` — invalid signal/path/manifest;
- `executor_crashed` — worker ended without a terminal signal; and
- `forced_control` — pause/cancel grace expired.

Provider stack traces and secrets remain in local diagnostic logs. Durable events carry stable error code, retryability, safe user message, and an opaque diagnostic ID.

## Conformance suite

Every executor must pass the same fake-service tests:

1. direct entry with minimum valid inputs;
2. invalid input produces no attempt side effects;
3. activity is transient and single-line safe;
4. interaction checkpoints before waiting and resumes in a new attempt;
5. pause and cancel survive engine restart;
6. duplicate command/signal delivery cannot duplicate Artifacts or side effects;
7. expired lease rejects late signals;
8. crash before/after an external side effect follows ambiguity rules;
9. completion publishes all outputs atomically;
10. checkpoint version mismatch pauses visibly;
11. no executor can write outside its staging directory; and
12. no earlier canonical module is required when equivalent direct-entry input is valid.

The first vertical slice proves lifecycle, storage, REST, replay, UI behavior, and Run-scoped conversation isolation. The four canonical ModuleRuns now route through concrete native executors and no longer enter the legacy research/task-plan orchestration. They deliberately reuse `BaseAgentLoop` as a conversation/provider engine; that shared engine is infrastructure, not workflow coordination.

## Extraction order

1. Implement the protocol, registry, scheduler lease, and common executor tests. — Done
2. Extract Initiation with literature work, direction choices, research-only completion, and optional handoff. — Done
3. Extract Planning and structured revision/approval interactions. — Done
4. Extract Delivery with direct entry from user input or selected Artifacts. — Done
5. Extract Experiment with an independent execution-capable tool surface and safe-boundary controls. — Done
6. Add atomic staging manifests, heartbeat/lease expiry, forced-control workers, and ambiguous-side-effect recovery before enabling unattended experiment automation. — Deferred hardening

Prompt redesign is a separate change after behavioral parity. Extraction changes ownership and persistence first; it must not simultaneously rewrite scientific behavior.

## Explicitly deferred

- third-party executor plugins;
- distributed workers or remote leases;
- automatic checkpoint migration across arbitrary executor versions;
- general-purpose DAG scheduling;
- exactly-once external side effects; and
- automatic inheritance of complete conversation history.
