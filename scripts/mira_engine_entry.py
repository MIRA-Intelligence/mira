"""Entry point used for standalone executable builds."""

import sys


if __name__ == "__main__":
    # The executable ships OpenSSL from the build machine, whose compiled-in
    # CA path does not exist here (python.org Python on the macOS runner):
    # without this, every HTTPS call through Python's default SSL context
    # fails with CERTIFICATE_VERIFY_FAILED. Must run before any network
    # library creates a context (see mira_engine.utils.tls).
    from mira_engine.utils.tls import ensure_ca_bundle

    ensure_ca_bundle()

    if len(sys.argv) > 1 and sys.argv[1] == "--mira-plugin-host":
        # The frozen build has no interpreter for `python -m
        # mira_engine.plugins.host`, so the executable serves the host itself
        # (see mira_engine.plugins.paths.host_command).
        from mira_engine.plugins.host import main

        raise SystemExit(main(sys.argv[2:]))

    from mira_engine.cli.agent_service import app

    app()
