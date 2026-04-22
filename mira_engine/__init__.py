"""
mira - A lightweight AI agent framework
"""

from importlib import metadata as importlib_metadata

from mira_engine.mira_engine import Mira, RunResult

try:
    __version__ = importlib_metadata.version("mira-engine")
except importlib_metadata.PackageNotFoundError:
    __version__ = "0.0.0"

__logo__ = "🐈"

__all__ = ["__version__", "__logo__", "Mira", "RunResult"]
