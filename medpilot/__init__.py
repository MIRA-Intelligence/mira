"""
medpilot - A lightweight AI agent framework
"""

from importlib import metadata as importlib_metadata

from medpilot.medpilot import MedPilot, RunResult

try:
    __version__ = importlib_metadata.version("medpilot")
except importlib_metadata.PackageNotFoundError:
    __version__ = "0.0.0"

__logo__ = "🐈"

__all__ = ["__version__", "__logo__", "MedPilot", "RunResult"]
