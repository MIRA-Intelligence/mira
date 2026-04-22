"""Entry point used for standalone executable builds."""

from medpilot.cli.agent_service import app


if __name__ == "__main__":
    app()
