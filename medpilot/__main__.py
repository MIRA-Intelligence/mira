"""
Entry point for running medpilot as a module: python -m medpilot
"""

from medpilot.cli.commands import app

if __name__ == "__main__":
    app()
