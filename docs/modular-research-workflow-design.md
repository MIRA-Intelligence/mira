# Design: Modular Research Workflows

Generated on 2026-07-22
Repos: `mira` + `mira-ui`
Target branches: `dev`
Status: APPROVED — end-to-end v1 and four native executors implemented; runtime hardening continues
Mode: Builder

## Problem Statement

MIRA currently presents four user-visible stages: Research, Plan, Experiment, and Result. They are not independent capabilities. A single `ResearchAgentLoop`, web-channel prompt contract, and shared `task_plan.json` enforce a mostly linear Research → Plan → Experiment → Result sequence.

This blocks valid research tasks that begin or end elsewhere:

- literature review and research-direction discussion without experiments;
- experiments starting from an existing hypothesis or plan;
- paper polishing or report generation starting from existing materials;
- multiple competing directions, plans, or experiment branches inside one project.

The product should let users start any research activity directly, finish it independently, and explicitly reuse its outputs in later activities.

## What Makes This Cool

The project becomes a living research workspace rather than a four-step wizard. Users can explore three directions from one literature review, plan two of them, run experiments for only one, and produce a paper from any selected evidence without fabricating missing stages.

## Constraints

- MIRA remains a local-first open-source research tool.
- Workflow state should remain human-inspectable, portable with the Project directory, and recoverable without a database administration tool.
- Existing agent tools, WebSocket streaming, project directories, experiment guardrails, manual/auto run modes, and desktop packaging should be reused.
- Existing projects containing only `task_plan.json` must remain readable during migration.
- Module execution must survive engine restarts and must not depend on UI process lifetime.
- The first implementation should not require LangGraph, Prefect, Temporal, or an external database service.
- `mira` and `mira-ui` must ship compatible protocol versions through the existing compatibility gate.

## Premises

1. A Project is a long-lived container. Creating a Project does not run AI work.
2. Initiation, Planning, Experiment, and Delivery are module definitions. Every invocation creates an independent ModuleRun.
3. A Project may contain repeated and branching ModuleRuns of every type.
4. ModuleRuns never mutate another ModuleRun. They exchange immutable, versioned Artifact references.
5. Run relationships may branch and return to an earlier module type, while Artifact derivation remains acyclic.
6. Project policy controls manual or automatic downstream Run creation. Automatic handoffs remain visible, auditable, pausable, and reversible.
7. The product needs a graph-shaped data model, not a graph execution framework in the first release.
8. Legacy projects are migrated with a strangler approach: read adapters first, optional materialization later, no big-bang rewrite.

## Conventional Patterns Reused

- Prefect separates task/flow definitions from stateful runs; MIRA adopts the same definition-versus-run distinction without adopting Prefect as a runtime: https://docs.prefect.io/v3/concepts/states
- MLflow groups multiple runs under a research container and records per-run artifacts and metadata: https://mlflow.org/docs/latest/tracking
- LangGraph confirms that checkpoints and explicit interrupts are the key primitives for resumable human-in-the-loop execution: https://docs.langchain.com/oss/javascript/langgraph/persistence
- W3C PROV supplies the minimal provenance vocabulary MIRA needs: an activity uses entities and generates derived entities: https://www.w3.org/TR/prov-dm/Overview.html

## Approaches Considered

### Approach A: Optional entry points on `task_plan.json`

Add start-stage and skipped-stage fields, split the prompt into four sections, and expose four UI entry buttons. This ships quickly but preserves shared mutable state and becomes difficult to reason about once runs branch or repeat.

### Approach B: Domain model plus gradual replacement

Introduce Project, ModuleRun, Artifact, ArtifactRef, and HandoffPolicy as the source of truth. Wrap current agent behavior behind module executors, reuse existing event streaming and experiment guardrails, and expose legacy `task_plan.json` through an adapter. This is the selected approach.

### Approach C: External graph workflow runtime

Represent modules as LangGraph, Prefect, or Temporal nodes and delegate persistence, retries, and interrupts to that runtime. This provides mature orchestration but couples MIRA's product model and local desktop distribution to unnecessary infrastructure.

## Recommended Approach

Use Approach B with a strangler migration. First establish the new persistent domain model and read APIs. Then move one module at a time behind a common ModuleExecutor boundary. Do not combine structural migration with prompt behavior changes in the same implementation step.

### Product model

```text
Project
  ├── policy: manual_handoff | auto_handoff
  ├── ModuleRun[]
  │     ├── type: initiation | planning | experiment | delivery
  │     ├── status: created | queued | running | waiting_input | paused |
  │     │           completed | failed | cancelled
  │     ├── input ArtifactRef[]
  │     ├── output ArtifactRef[]
  │     └── checkpoint + event stream
  └── Artifact[]
        ├── immutable metadata and typed payload reference
        ├── created_by_run_id or external upload
        └── derived_from ArtifactRef[]
```

Canonical names are used consistently across persistence, API, CLI, and new UI. Legacy labels are translated only at the compatibility boundary:

| Legacy stage | Canonical module | Meaning change |
|---|---|---|
| Research | Initiation | Research and direction exploration become an optional, independently completable Run |
| Plan | Planning | Approval applies to one plan Run, not the whole Project |
| Experiment | Experiment | Each invocation has isolated inputs, execution state, and outputs |
| Result | Delivery | Delivery accepts external materials and does not require prior MIRA Runs |

### Persistence and ownership defaults

Use one Project-level JSON file and one authoritative JSON snapshot for every ModuleRun. Do not replace the old monolithic `task_plan.json` with another monolithic workflow document.

Detailed schemas, mutation ordering, recovery behavior, migration rules, and failure tests are specified in `docs/modular-workflow-storage-contract.md`.

The versioned REST, WebSocket, CLI, command, event, replay, and error contracts are specified in `docs/modular-workflow-api-contract.md`.

The executor lifecycle, safe-boundary, interaction, checkpoint, and output-commit contracts are specified in `docs/modular-workflow-executor-contract.md`.

```text
<project>/.mira/
  project.json                         # Project identity, policy, global lifecycle, rebuildable module index
  project.json.lock                    # existing sidecar lock, reused as the Project mutation lock
  events.jsonl                         # Project-wide durable workflow event order
  modules/
    <run-id>/
      module.json                      # authoritative state for exactly one ModuleRun
      checkpoint.json                  # latest resumable executor checkpoint
      attempts/<attempt-id>.json       # immutable attempt result/error summary
  artifacts/
    <artifact-id>/
      artifact.json                    # immutable typed metadata and provenance
      payload.<ext>                    # content, or a reference to an existing Project file
  staging/<attempt-id>/                # unpublished outputs; safe to reconcile after a crash
```

`project.json` stores Project-wide facts only: schema and contract versions, display metadata, Project lifecycle, handoff policy, selected/default Run, and a rebuildable `module_index`. It may cache each Run's type, status, and revision for fast UI loading, but `modules/<run-id>/module.json` is the source of truth. On a mismatch, the engine rebuilds the index from module files instead of guessing which duplicate value is correct.

Every `module.json` belongs to one invocation, not merely one module type. A Project can therefore contain `planning-01`, `planning-02`, and multiple experiment branches without those Runs overwriting each other.

Minimal Project-level shape, extending the existing manifest without replacing its current fields:

```json
{
  "schema_version": 2,
  "id": "PRJ-001",
  "display_name": "Protein folding study",
  "run_mode": "manual",
  "agent_profile": "research",
  "contract_version": 2,
  "workflow": {
    "contract_version": 1,
    "status": "active",
    "handoff_policy": { "mode": "manual" },
    "selected_run_id": "planning-02",
    "module_index": [
      { "run_id": "initiation-01", "type": "initiation", "status": "completed", "revision": 8 },
      { "run_id": "planning-02", "type": "planning", "status": "waiting_input", "revision": 4 }
    ]
  }
}
```

Minimal per-Run shape:

```json
{
  "schema_version": 1,
  "run_id": "planning-02",
  "module_type": "planning",
  "status": "waiting_input",
  "revision": 4,
  "attempt_id": "attempt-01",
  "input_artifacts": ["artifact-direction-a", "artifact-brief-01"],
  "output_artifacts": [],
  "conversation_key": "ui:PRJ-001:run:planning-02",
  "checkpoint_ref": "checkpoint.json",
  "last_project_sequence": 42,
  "last_run_sequence": 17,
  "last_command_id": "command-uuid"
}
```

The Project index is deliberately disposable. It provides the global overview requested by the UI, while the per-Run file records and manages the real module state. This prevents a partial Project-index write from corrupting a ModuleRun.

- Reuse and extend the existing cross-process lock and atomic replacement helpers in `mira_engine/utils/locks.py`. All workflow mutations acquire the existing `.mira/project.json.lock` sidecar by locking `project.json`, validate an expected revision, write a temporary file, flush and `fsync` it, and atomically replace the target. ProjectRegistry and workflow writes therefore share one lock instead of creating incompatible lock domains.
- The engine is the only writer in the first release. UI and CLI mutate state through engine APIs; they never edit JSON directly. Other processes may inspect files read-only.
- Every ModuleRun owns a primary conversation thread. The Project owns one durable workflow event stream so creation, handoffs, and transitions across different Runs have one recoverable order.
- `module.json` is authoritative for Run lifecycle state and contains the last applied command ID, revision, Project sequence, Run sequence, and transition summary. Project-level `events.jsonl` is the audit/replay projection. If a crash leaves it behind a snapshot, startup recovery appends the missing transition from the snapshot; a malformed trailing line is discarded. WebSocket clients replay from one last acknowledged Project sequence.
- Checkpoints are written by the ModuleExecutor at declared safe boundaries. A checkpoint contains executor version, conversation cursor, approved inputs, resumable tool state, and last committed event sequence.

### JSON versus SQLite decision

| Option | Strengths | Costs and failure modes | Fit for the first modular release |
|---|---|---|---|
| Project + per-Run JSON | Matches the module boundary, easy to inspect/export/debug, reuses current atomic-write and lock code, no schema service or binary store | Cross-file transactions are limited; indexes must be rebuildable; querying thousands of Runs requires scanning | **Recommended** for local, single-engine Projects |
| SQLite metadata + filesystem payloads | Strong transactions, constraints, indexes, and efficient graph queries | Adds database schema migrations, backup/repair behavior, opaque state for users, and filesystem/DB commit coordination | Defer until observed requirements justify it |
| JSON source plus SQLite cache | Keeps inspectability and fast queries | Creates two representations and cache invalidation work before scale requires it | Do not build initially |

SQLite is not rejected permanently. Introduce it behind the same repository interface only when at least one measured trigger appears: multiple engine writers or collaborative editing; Project startup/index rebuild becomes perceptibly slow; typical Projects reach thousands of Runs/Artifacts; cross-Project queries become a core feature; or JSON recovery incidents show the file model is insufficient. This keeps the first release simple without locking the architecture to JSON forever.

### Run lifecycle

| Current state | Command/event | Next state | Guard and recovery rule |
|---|---|---|---|
| `created` | enqueue | `queued` | Inputs and module contract validate |
| `queued` | executor claims | `running` | Engine records an execution attempt and lease before side effects |
| `queued` | pause during interrupt window | `paused` | No executor has claimed the Run; queue claim is revoked atomically |
| `running` | needs user decision | `waiting_input` | Checkpoint and prompt are durable before notification |
| `waiting_input` | submit validated response | `queued` | Response and checkpoint cursor commit together; idempotency key prevents duplicate answers |
| `running` / `waiting_input` | pause | `paused` | Executor reaches a safe boundary; no new tool starts afterward |
| `paused` | resume | `queued` | Creates a new attempt from the latest compatible checkpoint |
| `running` | outputs committed | `completed` | Terminal; outputs and completion event commit together |
| non-terminal | cancel | `cancelled` | Terminal; running tools receive best-effort cancellation and staged outputs are discarded |
| `running` | unrecoverable error | `failed` | Terminal attempt record retains error and checkpoint |
| `failed` | retry | `queued` | New attempt; the previous attempt remains immutable |

`completed` and `cancelled` Runs never reopen. Retrying a completed scientific operation creates a new Run, preserving provenance. On engine startup, an expired `running` lease moves the Run to `paused` with a recovery event. The user may resume from a compatible checkpoint or restart as a new attempt. Commands carry an idempotency key so reconnects cannot enqueue, cancel, answer, or create a handoff twice.

### Artifact contract

An Artifact has an immutable UUID, canonical type, schema version, media type, byte size, SHA-256 content hash, creation time, producing Run or external-import identity, payload path, and `derived_from` references. An `ArtifactRef` contains the UUID plus the expected type and supported schema-version range.

Payloads and a completion manifest are staged under `.mira/staging/<attempt-id>/`, validated and hashed, then renamed into the final Artifact directory. Under the Project lock, the engine atomically writes `artifact.json`, then atomically replaces the producing `module.json` with its output references and terminal state. A crash between these steps can leave only an unreferenced immutable Artifact, which startup reconciliation either finishes linking from the durable completion manifest or moves to an orphan quarantine. Reconciliation flags the inverse case, a module reference whose payload is missing, and never silently invents an output. Cancellation and failed attempts retain logs and checkpoints but publish no staged payload as a Run output.

Readers must reject unsupported major schema versions with a visible compatibility error. Minor additive fields are ignored. Schema migrations create a new Artifact derived from the old one rather than editing the original. Release one supports explicit user deletion of an entire Project only; per-Artifact deletion and retention policies are deferred so provenance cannot be accidentally broken.

### Module contracts

| Module | Minimum accepted input set (one alternative is enough) | Optional context | Primary outputs | Independent completion |
|---|---|---|---|---|
| Initiation | research question; or uploaded references plus a discussion request | constraints, existing SourceCollection | ResearchBrief, DirectionCandidate[], SourceCollection | User can stop after discussion or choose directions |
| Planning | a selected DirectionCandidate; or a user-authored research goal | ResearchBrief, constraints, prior plans | ClarificationSet, ApprovedPlan, ExperimentSpec[] | User can keep a plan without running it |
| Experiment | an ExperimentSpec; or a user-authored hypothesis plus execution request | datasets, code, prior Findings, constraints | ExperimentRunRecord, Metrics, Finding[], produced files | User can stop after any run or create new branches |
| Delivery | source material plus a delivery instruction | ResearchBrief, ApprovedPlan, Findings, code and data files | Paper, Report, Analysis, CodePackage | Can run without any earlier MIRA module |

Modules validate only the inputs they actually need. Missing upstream module history is not an error when the direct input contract is satisfied.

### Handoff model

```text
[ModuleRun completes]
         |
         v
[Typed outputs are committed atomically]
         |
         +--> manual policy: show suggested next actions and wait
         |
         +--> auto policy: record decision --> create downstream Run as queued
                                      |
                                      +--> executor may claim only after grace/checkpoint boundary
```

An automatic handoff records the source Run, selected Artifacts, target module type, policy rule, and AI rationale. It creates a new Run; it never changes the completed source Run.

`HandoffPolicy` is Project-scoped and defaults to manual. A rule matches source module, output Artifact type, optional target module, and action (`suggest` or `enqueue`). Rules are evaluated once after atomic output commit, with explicit user overrides taking precedence over Project defaults. Every evaluation is stored even when no Run is created. An automatically enqueued Run has a durable notification and a default 10-second interrupt window before an executor may claim it; the Project can lengthen or disable that grace period. “Reverse” means cancelling an unstarted downstream Run or recording a compensating disabled decision; source Runs, Artifacts, and prior audit events remain immutable. Release one does not automatically chain beyond one downstream Run without a fresh policy evaluation.

### Module execution boundary

Each module implements the same `ModuleExecutor` contract: validate selected inputs, initialize or restore a checkpoint, yield durable events, request user input, stage outputs, and return a commit manifest. The executor cannot directly mark itself complete or publish Artifacts. A workflow service owns transitions, leases, command idempotency, staging cleanup, and the final locked file commit.

External tool calls must use an attempt-scoped idempotency key where the provider supports it. Non-idempotent work is checkpointed before and after the side effect and surfaced for user confirmation after ambiguous crashes; MIRA must not silently repeat an experiment or paid action.

### User interaction

```text
Empty Project
  [Start Initiation] [Start Planning] [Start Experiment] [Start Delivery]

Project workspace
  Runs/List view                  Selected Run
  ├─ Initiation #1 complete      ├─ conversation and current status
  ├─ Planning #1 waiting         ├─ selected input artifacts
  ├─ Experiment #1 running       ├─ output artifacts
  └─ Delivery #1 complete        └─ suggested next actions

Research graph
  Initiation #1 --Direction A--> Planning #1 --Spec--> Experiment #2
        |                                               |
        +--ResearchBrief------------------------------> Delivery #1
  Uploaded manuscript--------------------------------> Delivery #2
```

The graph is for lineage and navigation. It is not an auto-executing DAG editor.

## Legacy coexistence

| Project state | Read behavior | Write behavior | Rollback |
|---|---|---|---|
| Legacy only (`task_plan.json`) | Adapter exposes one synthetic Run per populated legacy stage and typed synthetic Artifacts | Legacy UI remains writable through the old path; modular UI is read-only until conversion | No new files are required |
| Converted | First modular action materializes synthetic Runs into independent module directories, creates Artifact metadata, and records the original file as a source Artifact | All new work uses workflow APIs; `task_plan.json` becomes read-only | Remove the generated modular directory only before the first new ModuleRun executes |
| Modular | Native workflow read APIs over Project/module JSON | Native workflow write APIs using the Project lock | Copy or restore the self-contained `.mira/` directory |

New Runs may consume materialized legacy Artifacts, but there is no dual write between the two models. Conversion is explicit and preceded by a backup of `.mira/`. The adapter can be removed only after two compatible mira/mira-ui release cycles, migration telemetry or user reports show no blocking failures, and fixture-based migration tests cover every supported legacy schema.

## Release slices and non-goals

### Slice 1: thin vertical foundation

- Project/module JSON repository, Artifact staging/commit, lifecycle service, Run event replay, index reconciliation, and legacy read adapter.
- Initiation as the first extracted executor.
- API/CLI/UI support for creating an empty Project, starting Initiation, pausing/resuming after restart, selecting outputs, and stopping there.
- Feature flag; existing four-stage projects continue on the legacy path.

### Slice 2: arbitrary entry and planning

- External Artifact import, direct Planning and Delivery entry, per-Run conversations, and manual handoffs.
- Explicit legacy conversion and mixed lineage from imported legacy Artifacts.

### Slice 3: experiment isolation and policy automation

- Experiment executor extraction with existing guardrails, automatic queued handoffs, branching navigation, and all four direct-entry journeys.

### Explicit non-goals for the first modular release

- Visual DAG editing or arbitrary user-authored module types.
- Multiple engine processes writing one Project, remote database services, or collaborative live editing.
- Automatic graph-wide scheduling, unlimited automatic chains, or cross-Project Artifact references.
- Per-Artifact retention/deletion, in-place Artifact mutation, or invisible retries of ambiguous external side effects.

## Success Criteria

The following are program-level completion gates for Slice 3. Each earlier slice has the narrower acceptance criteria listed in its own section.

- A user can create an empty Project and directly start any of the four module types.
- Each module can complete without creating downstream modules.
- A Project can hold at least two Runs of the same module type and two branches derived from one Artifact.
- Users can select explicit Artifact inputs before starting a Run and inspect their provenance afterward.
- Manual handoff never creates downstream work without confirmation; auto handoff is fully logged and pausable.
- Restarting the engine during `running` or `waiting_input` restores a consistent Run state and conversation.
- Legacy `task_plan.json` projects open without data loss and remain usable during the migration window.
- CLI/API users can create, inspect, run, pause, resume, and cancel ModuleRuns without mira-ui.
- Existing experiment guardrails continue to enforce one active experiment execution per Experiment Run.
- All state transitions, artifact contract validation, migration paths, and four direct-entry user journeys have automated tests.

## Distribution Plan

No new deployable artifact is introduced. The new protocol ships through the existing `mira` Python package and standalone engine releases, then through compatible `mira-ui` bundle and standalone releases. The compatibility manifest must add a workflow-contract version gate before mira-ui enables modular Projects.

## Repository implementation map

### `mira`

- Add `mira_engine/workflows/` as the new domain boundary: models and schemas, JSON repository, lifecycle service, Artifact store, handoff evaluator, event replay, index reconciliation, and legacy adapter. Keep a repository protocol so a future SQLite implementation does not change executors or APIs.
- Add `mira_engine/modules/` with the common `ModuleExecutor` protocol and one package per canonical module. Adapt existing `ResearchAgentLoop` behavior into the Initiation executor incrementally; do not make `research_loop.py` the new workflow coordinator.
- Keep `projects.py` responsible for Project discovery and filesystem locations. Evolve its existing `.mira/project.json` metadata into the Project-level workflow document through a schema-version migration, while delegating per-Run state to the workflow repository; do not create a second competing Project manifest.
- Extend `channels/ui.py` with versioned `/api/workflow/...` endpoints and Run-scoped WebSocket envelopes. Existing `/api/plan` and project-wide messages remain the legacy compatibility path.
- Extend `session/manager.py` to accept a Run-scoped thread identity while retaining legacy project session keys.
- Replace global prompt rules in `channels/ui_assets/AGENTS_UI.md` and `SKILL_UI.md` only as each executor is extracted. Module contracts belong beside each executor, not in one four-stage prompt.
- Keep `task_plan/guardrails.py` on the Experiment path initially, then expose its checks behind the Experiment executor boundary.

### `mira-ui`

- Split workflow domain types out of `src/types/index.ts` into `src/types/workflow.ts`, generated or contract-tested against the engine schema.
- Introduce a focused `workflowStore` for Project summary, Runs, Artifacts, commands, and event cursors. Keep `projectStore.ts` as the legacy adapter during migration; do not make one store understand both schemas through conditionals everywhere.
- Change `NewProjectModal.tsx` to create an empty container only. Starting a module is a separate action from the empty workspace.
- Replace `PipelineProgress.tsx` as the primary navigator with a Run list/workspace and add a lineage view later. Keep the old pipeline only for legacy projects.
- Make `TaskDetail.tsx` route to a selected Run workspace rather than deriving one active stage from a monolithic `ProjectTask`.
- Scope `AgentPanel.tsx` and chat state by `run_id`; the current one-line live operation indicator remains a projection of the selected Run's event stream.

### Minimal protocol surface

```text
GET    /api/workflow/v1/projects/{project_id}
POST   /api/workflow/v1/projects/{project_id}/commands
POST   /api/workflow/v1/projects/{project_id}/runs
GET    /api/workflow/v1/projects/{project_id}/runs
GET    /api/workflow/v1/projects/{project_id}/runs/{run_id}
POST   /api/workflow/v1/projects/{project_id}/runs/{run_id}/commands
GET    /api/workflow/v1/projects/{project_id}/events?after=<project_sequence>
POST   /api/workflow/v1/projects/{project_id}/artifacts/import
GET    /api/workflow/v1/projects/{project_id}/artifacts/{artifact_id}
```

All mutations use the command endpoint with `command_id`, expected Run version, command type, and payload. Optimistic version checks reject stale UI actions; replayable events carry `workflow_contract_version`, Project sequence, optional Run sequence, `run_id`, event type, and payload schema version.

## Verification strategy

- Repository tests: schema migration, legal/illegal transitions, optimistic version conflicts, idempotent duplicate commands, Artifact type/version validation, and lineage cycle rejection.
- Crash-point tests: before/after Artifact rename, before/after `module.json` replacement, stale `project.json` index recovery, truncated event tail, expired lease recovery, executor checkpoint incompatibility, and WebSocket reconnect replay.
- Legacy fixtures: empty, partially completed, complete, malformed, and multiple-experiment `task_plan.json`; assert that conversion is deterministic and the source remains recoverable.
- Executor contract tests: direct minimum inputs, optional upstream context, `waiting_input`, cancel, retry, and output manifest validation for every module.
- UI tests: empty Project, direct module chooser, selected Artifact inputs, two Runs of the same type, branch navigation, Run-scoped chat, queued interrupt, and compatibility fallback.
- Packaged end-to-end gates: each of the four direct-entry journeys, research-only stop, legacy open/convert, engine restart during `running` and `waiting_input`, and mismatch behavior between mira and mira-ui contract versions.

## Next Steps

1. Write an architecture decision record for Project/per-Run JSON, local single-writer ownership, rebuildable indexes, SQLite adoption triggers, and per-Run conversations.
2. Specify versioned `project.json`, `module.json`, checkpoint and Artifact schemas, lifecycle commands, REST/WebSocket event envelope, and CLI parity.
3. Build Slice 1 end-to-end rather than implementing every backend layer before a usable journey exists.
4. Add crash-point tests around staging, atomic file replacement, index reconciliation, event-tail repair, lease expiry, replay, and idempotent command retries.
5. Validate Slice 1 with legacy fixtures and a packaged mira-ui bundle before extracting the remaining executors.
6. Deliver Slices 2 and 3 behind contract-version gates, extracting Planning, Delivery, then Experiment without changing their scientific prompts in the same commits.

## What I Noticed About the Product Direction

- The request is based on a concrete user complaint: users cannot stop after research discussion or begin from material they already have.
- The phrase “从任意环节开始” implies independent completion and reusable outputs, not merely optional checkboxes in a fixed wizard.
- Choosing repeatable branches over a single skippable chain makes MIRA fit real research, where competing directions and re-planning are normal rather than exceptional.
