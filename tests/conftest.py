import os
import pytest

@pytest.fixture(autouse=True)
def skip_gateway_failsave():
    """Globally skip gateway fail-safe checks during tests to prevent port/PID collision issues."""
    os.environ["MEDPILOT_SKIP_GATEWAY_FAILSAVE"] = "1"
    yield
    # We don't necessarily need to unset it as it's a global test setting
