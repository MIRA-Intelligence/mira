"""Message bus module for decoupled channel-agent communication."""

from medpilot.bus.events import InboundMessage, OutboundMessage
from medpilot.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
