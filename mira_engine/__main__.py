"""
Entry point for running mira as a module: python -m mira_engine
"""

from mira_engine.cli.commands import app

if __name__ == "__main__":
    app()
