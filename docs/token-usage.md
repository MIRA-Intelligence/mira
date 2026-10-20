# Session token accounting

The status bar places ₿ token usage directly after the Skill chip. Quick Chat
uses its active chat ID; a project aggregates all its module Runs. Hover shows
input, output, cached input (already included in input), and returned-call count.
Click the token chip to open the current session's cumulative breakdown, including
cache-read tokens, input not served from cache, token-weighted cache hit rate,
call count and reporting coverage. It updates from the same live snapshot without
polling or additional model calls. Escape, the close button or an outside click
closes it; switching conversation/workspace/backend dismisses the old panel.
These are tokens, not a currency estimate or a provider billing statement.

UI message processing establishes task-local attribution. The main model loop,
delegated AgentRunner calls (including finalization), and conversation memory
calls record provider-reported usage once each. Delegated/background asyncio
tasks inherit their originating conversation, including when another conversation
becomes active. Nested native/Base/research dispatch does not create two counters.
Input plus output is used if the provider omits total usage. Calls with absent or
zero usage are marked unreported; partial totals display `≥`, or `—` if no amount
is known. Network failures or provider-internal retries without returned usage
cannot be reconstructed and are not guaranteed to match billed consumption.

Provider adapters preserve `prompt_tokens_details.cached_tokens`, Responses
`input_tokens_details.cached_tokens`, top-level `cached_tokens`, DeepSeek's
`prompt_cache_hit_tokens`, and Anthropic's `cache_read_input_tokens`. An explicit
zero is preserved; absent/null/invalid fields remain unknown. NVIDIA, custom,
LiteLLM, OpenAI-compatible, Responses streaming/non-streaming and Codex OAuth
paths share this behavior. Cache reads never increase total tokens a second time.

`cache_reported_calls` counts calls with valid cache-read telemetry, separately
from `unreported_calls` (missing overall usage). Missing cache telemetry displays
Unknown or a known lower bound, not a fabricated zero miss. Uncached input and
hit rate are shown only when all recorded calls have complete cache-read data.
Input not served from cache can include cache writes; it is not a price category.
These aggregate counters cannot attribute input tokens to an individual skill.

Counters are stored in `.mira/token-usage.sqlite3` under the project directory,
or workspace for Quick Chat, grouped by session, Run and model. No prompts,
responses, credentials, or per-token records are stored. Historical consumption
before this feature was enabled is not recoverable from these counters. `/new`
does not erase lifetime consumption for the same conversation ID.
Cache coverage uses an additive `cache_reporting` table in the same transaction;
old counters remain readable without migration on GET, and old engines can still
write their original table. Previously dropped cache details cannot be backfilled;
old calls remain unknown even if their stored cache counter was zero.

Each returned model call performs one small SQLite transaction in a worker
thread, then sends one `token_usage` WebSocket event. Events do not append chat
history, set streaming flags, or refresh the workflow. The client only hydrates
`GET /api/sessions/{session_id}/usage` on selection/mount/reconnect; there is no
periodic polling. A monotonic call count prevents stale REST responses or parallel
notifications from rolling totals back. Workspace/backend switches clear cached
snapshots and invalidate pending requests. Restarted clients restore persisted
counters. Storage failure is logged and never aborts the model turn.

This accounting is separate from legacy automation budget enforcement. Project
lifetime usage is not displayed against a single Run's token budget.
