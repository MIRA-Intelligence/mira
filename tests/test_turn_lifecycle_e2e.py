"""A UI turn end to end: dispatch and agent loop -> UiChannel.send -> the websocket.

Covers a turn that hits the tool-call limit and the "continue" that follows it.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from mira_engine.bus.events import InboundMessage
from mira_engine.providers.base import LLMResponse, ToolCallRequest
from tests.test_agent_loop_core import _EchoTool, _make_real_loop
from tests.test_ui_channel import ui_channel  # noqa: F401  (pytest fixture)


async def test_limit_hit_reaches_client_and_continue_has_context(tmp_path, ui_channel):  # noqa: F811
    loop = _make_real_loop(tmp_path)
    loop.tools.register(_EchoTool())
    loop.max_iterations = 3
    loop.memory_window = 4  # far smaller than one long turn, like 100 vs 400+

    calls = {"n": 0}

    async def chat_stream_with_retry(*, on_content_delta, messages, **_kw):
        calls["n"] += 1
        if calls.get("resume"):
            # The model must see the original request despite the tiny window.
            assert any(m.get("content") == "train all models" for m in messages if m["role"] == "user")
            await on_content_delta("Resuming.")
            return LLMResponse(content="Resuming.")
        await on_content_delta(f"step {calls['n']}")
        return LLMResponse(content=f"step {calls['n']}",
                           tool_calls=[ToolCallRequest(id=f"c{calls['n']}", name="echo", arguments={"value": 1})])

    loop.provider.chat_stream_with_retry = chat_stream_with_retry

    ws = MagicMock(closed=False, send_json=AsyncMock())
    ui_channel._clients["chat-1"] = ws
    ui_channel._turn_event_sockets.add(ws)

    async def run_turn(text):
        ws.send_json.reset_mock()
        msg = InboundMessage(channel="ui", sender_id="u", chat_id="chat-1", content=text,
                             metadata={"_wants_stream": True, "turn_id": f"turn-{text}"})
        await loop._dispatch(msg)
        while not loop.bus.outbound.empty():
            await ui_channel.send(await loop.bus.consume_outbound())
        return [c.args[0] for c in ws.send_json.call_args_list]

    sent = await run_turn("train all models")
    types = [p["type"] for p in sent]
    assert types[0] == "turn_started" and types[-1] == "turn_finished"
    assert sent[-1]["metadata"]["stop_reason"] == "max_iterations"
    assert any(p["type"] == "response" and "limit of 3 tool calls" in p["content"] for p in sent)
    req = SimpleNamespace(match_info={"session_id": "chat-1"})
    assert json.loads((await ui_channel._handle_active_turns(req)).text)["active"] == []

    calls["resume"] = True
    sent = await run_turn("continue")
    assert sent[-1]["metadata"]["stop_reason"] == "completed"
    # Final text was streamed, so no duplicate response is sent.
    assert not any(p["type"] == "response" for p in sent)
