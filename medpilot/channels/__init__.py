"""Chat channels module with plugin architecture."""

from medpilot.channels.base import BaseChannel
from medpilot.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
