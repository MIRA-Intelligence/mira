import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from mira_engine.agent.research_loop import ResearchAgentLoop
from mira_engine.agent.runner import AgentRunner, AgentRunSpec
from mira_engine.agent.token_usage import TokenUsageStore, record_token_usage, track_token_usage
from mira_engine.agent.tools.registry import ToolRegistry
from mira_engine.bus.events import InboundMessage
from mira_engine.bus.queue import MessageBus
from mira_engine.config.schema import ChannelsConfig
from mira_engine.providers.base import LLMProvider
from mira_engine.session.manager import SessionManager
from mira_engine.workflows import JsonWorkflowRepository, WorkflowService


class UsageProvider(LLMProvider):
    async def chat(self, **kwargs):
        from mira_engine.providers.nvidia_provider import NvidiaProvider
        return NvidiaProvider._parse_success({
            "choices": [{"message": {"content": "Measured response"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500,
                      "prompt_tokens_details": {"cached_tokens": 800}},
        })

    def get_default_model(self):
        return "dummy/audit"


def test_store_restart_normalization_and_isolation(tmp_path):
    store = TokenUsageStore(tmp_path)
    assert store.snapshot("one")["calls"] == 0
    assert not store.path.exists()
    store.record("one", "run-a", "a", {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 7})
    store.record("one", "run-b", "b", {"input_tokens": 20, "output_tokens": 3})
    store.record("two", "", "a", {"total_tokens": 999})
    store.record("one", "run-a", "a", {})
    result = TokenUsageStore(tmp_path).snapshot("one")
    assert result == dict(input_tokens=30, output_tokens=8, total_tokens=38, cached_tokens=7, calls=3, unreported_calls=1,
                         cache_reported_calls=1)
    assert store.snapshot("two")["total_tokens"] == 999


def test_cache_reporting_distinguishes_unknown_zero_and_partial(tmp_path):
    store = TokenUsageStore(tmp_path)
    first = store.record("one", "", "model", {"prompt_tokens": 100})
    assert first["cache_reported_calls"] == 0
    second = store.record("one", "", "model", {"prompt_tokens": 100, "cached_tokens": 0})
    assert second["cache_reported_calls"] == 1
    third = store.record("one", "", "model", {"prompt_tokens": 100, "cached_tokens": 80})
    assert third["cache_reported_calls"] == 2
    assert third["cached_tokens"] == 80
    assert third["input_tokens"] == third["total_tokens"] == 300


def test_old_database_is_readable_and_cache_history_stays_unknown(tmp_path):
    import sqlite3
    store = TokenUsageStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TABLE usage (session TEXT, run TEXT, model TEXT, input INTEGER, output INTEGER, "
                   "total INTEGER, cached INTEGER, calls INTEGER, missing INTEGER, PRIMARY KEY(session,run,model))")
        db.execute("INSERT INTO usage VALUES('one','','model',100,10,110,50,2,0)")
    assert store.snapshot("one")["cache_reported_calls"] == 0
    result = store.record("one", "", "model", {"prompt_tokens": 100, "cached_tokens": 80})
    assert result["cache_reported_calls"] == 1
    assert result["calls"] == 3
    assert result["cached_tokens"] == 130


async def test_concurrent_updates_are_atomic(tmp_path):
    store = TokenUsageStore(tmp_path)
    await asyncio.gather(*(asyncio.to_thread(store.record, "one", "run", "model", {"total_tokens": 5}) for _ in range(30)))
    assert store.snapshot("one")["total_tokens"] == 150
    assert store.snapshot("one")["calls"] == 30


@pytest.mark.parametrize("module", [None, "initiation", "planning", "experiment", "delivery"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_live_usage_all_modules_and_chat(tmp_path, monkeypatch, module, streaming):
    monkeypatch.setenv("MIRA_HOME", str(tmp_path / "home"))
    project_dir = tmp_path / "PRJ-audit"
    metadata = {"loop_mode": "normal"}
    if module:
        (project_dir / ".mira").mkdir(parents=True)
        (project_dir / ".mira/project.json").write_text(json.dumps({"schema_version": 1, "id": "PRJ-audit"}))
        service = WorkflowService(JsonWorkflowRepository(project_dir, "PRJ-audit"))
        run_id = service.create_run(command_id="create", module_type=module, enqueue=True, module_data={"intent": "Audit"}).run_id
        service.claim_run(run_id, lease_owner="audit")
        metadata = {"project_id": "PRJ-audit", "project_dir": str(project_dir), "workflow_run_id": run_id}
    loop = ResearchAgentLoop(bus=MessageBus(), provider=UsageProvider(), workspace=tmp_path,
                             model="dummy/audit", channels_config=ChannelsConfig(), session_manager=SessionManager(tmp_path))
    # Two turns; updates must arrive even when responses are streamed, without double counting the nested Base loop.
    for _ in range(2):
        await loop._process_message(InboundMessage(channel="ui", sender_id="u", chat_id="PRJ-audit", content="Audit", metadata=metadata),
                                    on_stream=AsyncMock() if streaming else None)
    events = []
    while loop.bus.outbound_size:
        event = await loop.bus.consume_outbound()
        if event.metadata.get("_usage_only"):
            events.append(event.metadata["token_usage"])
    assert [event["total_tokens"] for event in events] == [1500, 3000]
    assert [event["cached_tokens"] for event in events] == [800, 1600]
    assert [event["cache_reported_calls"] for event in events] == [1, 2]
    result = TokenUsageStore(project_dir if module else tmp_path).snapshot("PRJ-audit")
    assert result["total_tokens"] == 3000
    assert result["calls"] == 2


async def test_runner_delegation_context_isolation_and_unscoped_calls(tmp_path):
    class Owner:
        workspace = tmp_path
        bus = MessageBus()

        def _project_ref_from_metadata(self, metadata):
            return None

        @track_token_usage
        async def run(self, msg):
            spec = AgentRunSpec(initial_messages=[{"role": "user", "content": "Check"}],
                                tools=ToolRegistry(), model="dummy/audit", max_iterations=1, max_tool_result_chars=1000)
            await asyncio.create_task(AgentRunner(UsageProvider()).run(spec))

    owner = Owner()
    await asyncio.gather(*(owner.run(InboundMessage(channel="ui", sender_id="u", chat_id=chat, content="Check")) for chat in ("a", "b")))
    await record_token_usage({"total_tokens": 999})  # No active session: do not misattribute.
    for chat in ("a", "b"):
        assert TokenUsageStore(tmp_path).snapshot(chat)["total_tokens"] == 1500


async def test_accounting_failure_does_not_fail_turn(tmp_path, monkeypatch):
    from mira_engine.agent.token_usage import UsageContext, _current
    context = UsageContext(TokenUsageStore(tmp_path), "test", "", MessageBus(), {})
    token = _current.set(context)
    monkeypatch.setattr(context.store, "record", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    try:
        await record_token_usage({"total_tokens": 1})
        assert context.bus.outbound_size == 0
    finally:
        _current.reset(token)
