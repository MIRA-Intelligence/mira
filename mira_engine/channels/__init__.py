"""Chat channels module with plugin architecture."""

from mira_engine.channels.base import BaseChannel
from mira_engine.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
