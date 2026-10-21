"""Entry point used for standalone executable builds."""

import sys


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--mira-plugin-host":
        # The frozen build has no interpreter for `python -m
        # mira_engine.plugins.host`, so the executable serves the host itself
        # (see mira_engine.plugins.paths.host_command).
        from mira_engine.plugins.host import main

        raise SystemExit(main(sys.argv[2:]))

    from mira_engine.cli.agent_service import app

    app()
