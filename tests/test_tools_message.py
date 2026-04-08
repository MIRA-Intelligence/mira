from __future__ import annotations

from medpilot.agent.tools.message import MessageTool


async def test_message_tool_requires_target_context() -> None:
    tool = MessageTool()
    result = await tool.execute(content="hello")
    assert result == "Error: No target channel/chat specified"


async def test_message_tool_requires_send_callback() -> None:
    tool = MessageTool(default_channel="web", default_chat_id="PRJ-1")
    result = await tool.execute(content="hello")
    assert result == "Error: Message sending not configured"


async def test_message_tool_sends_and_tracks_sent_in_turn() -> None:
    captured = []

    async def _send(msg):
        captured.append(msg)

    tool = MessageTool(send_callback=_send, default_channel="web", default_chat_id="PRJ-1")
    tool.start_turn()
    result = await tool.execute(content="hello", media=["a.png"])
    assert result == "Message sent to web:PRJ-1 with 1 attachments"
    assert tool._sent_in_turn is True
    assert captured[0].metadata["message_id"] is None
    assert captured[0].media == ["a.png"]


async def test_message_tool_does_not_mark_other_targets_as_sent() -> None:
    async def _send(msg):
        return None

    tool = MessageTool(send_callback=_send, default_channel="web", default_chat_id="PRJ-1")
    tool.start_turn()
    result = await tool.execute(content="hello", channel="cli", chat_id="direct")
    assert result == "Message sent to cli:direct"
    assert tool._sent_in_turn is False


async def test_message_tool_surfaces_callback_error() -> None:
    async def _send(_):
        raise RuntimeError("network down")

    tool = MessageTool(send_callback=_send, default_channel="web", default_chat_id="PRJ-1")
    result = await tool.execute(content="hello")
    assert result == "Error sending message: network down"
