"""Message tool for sending messages to users."""

from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from mira_engine.agent.tools.base import Tool
from mira_engine.bus.events import OutboundMessage


class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
    ):
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._default_message_id = default_message_id
        self._runtime_channel: ContextVar[str | None] = ContextVar("message_channel", default=None)
        self._runtime_chat_id: ContextVar[str | None] = ContextVar("message_chat_id", default=None)
        self._runtime_message_id: ContextVar[str | None] = ContextVar("message_id", default=None)
        self._runtime_sent_in_turn: ContextVar[bool | None] = ContextVar(
            "message_sent_in_turn",
            default=None,
        )
        self._sent_in_turn_fallback = False

    @property
    def _sent_in_turn(self) -> bool:
        runtime_value = self._runtime_sent_in_turn.get()
        if runtime_value is not None:
            return runtime_value
        return self._sent_in_turn_fallback

    @_sent_in_turn.setter
    def _sent_in_turn(self, value: bool) -> None:
        bool_value = bool(value)
        self._sent_in_turn_fallback = bool_value
        self._runtime_sent_in_turn.set(bool_value)

    def set_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Set the current message context."""
        self._default_channel = channel
        self._default_chat_id = chat_id
        self._default_message_id = message_id
        self._runtime_channel.set(channel)
        self._runtime_chat_id.set(chat_id)
        self._runtime_message_id.set(message_id)

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    def start_turn(self) -> None:
        """Reset per-turn send tracking."""
        self._sent_in_turn = False

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return "Send a message to the user. Use this when you want to communicate something."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content to send"
                },
                "channel": {
                    "type": "string",
                    "description": "Optional: target channel (telegram, discord, etc.)"
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional: target chat/user ID"
                },
                "media": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: list of file paths to attach (images, audio, documents)"
                }
            },
            "required": ["content"]
        }

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        media: list[str] | None = None,
        **kwargs: Any
    ) -> str:
        default_channel = self._runtime_channel.get() or self._default_channel
        default_chat_id = self._runtime_chat_id.get() or self._default_chat_id
        default_message_id = self._runtime_message_id.get() or self._default_message_id
        channel = channel or default_channel
        chat_id = chat_id or default_chat_id
        message_id = message_id or default_message_id

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"

        if not self._send_callback:
            return "Error: Message sending not configured"

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            media=media or [],
            metadata={
                "message_id": message_id,
            },
        )

        try:
            await self._send_callback(msg)
            if channel == default_channel and chat_id == default_chat_id:
                self._sent_in_turn = True
            media_info = f" with {len(media)} attachments" if media else ""
            return f"Message sent to {channel}:{chat_id}{media_info}"
        except Exception as e:
            return f"Error sending message: {str(e)}"
