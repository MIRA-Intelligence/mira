import pytest
from unittest.mock import patch

@pytest.fixture(autouse=True)
def mock_gateway_failsafe():
    """Globally mock the gateway failsafe check to avoid PID/port collision issues in tests."""
    with patch("medpilot.cli.commands._gateway_failsafe_check") as mock:
        yield mock
