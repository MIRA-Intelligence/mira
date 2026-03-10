"""Chat channels module with plugin architecture."""

from radiologybot.channels.base import BaseChannel
from radiologybot.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
