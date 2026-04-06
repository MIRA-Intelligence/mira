"""
medpilot - A lightweight AI agent framework
"""

from importlib import metadata as importlib_metadata

try:
    __version__ = importlib_metadata.version("medpilot")
except importlib_metadata.PackageNotFoundError:
    __version__ = "0.0.0"

__logo__ = "🐈"
