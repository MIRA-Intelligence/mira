"""Tests for PythonRuntimeConfig and its embedding in ExecToolConfig.

These exercise serialization round-trips, default values, alias acceptance
(camelCase / snake_case), and validation of the closed-set ``manager`` and
``link_mode`` literals.
"""

import pytest
from pydantic import ValidationError

from mira_engine.config.schema import ExecToolConfig, PythonRuntimeConfig


class TestPythonRuntimeConfigDefaults:

    def test_default_factory_disabled(self) -> None:
        """Out-of-the-box config keeps the legacy 'off' behaviour."""
        cfg = PythonRuntimeConfig()
        assert cfg.manager == "off"
        assert cfg.auto_bootstrap is True
        assert cfg.venv_dir == ".venv"
        assert cfg.cache_dir == ""
        assert cfg.link_mode == "hardlink"
        assert cfg.baseline_requirements == []
        assert cfg.python_version == ""

    def test_exec_tool_config_embeds_python(self) -> None:
        cfg = ExecToolConfig()
        assert isinstance(cfg.python, PythonRuntimeConfig)
        assert cfg.python.manager == "off"


class TestPythonRuntimeConfigValidation:

    @pytest.mark.parametrize("manager", ["off", "uv", "system"])
    def test_manager_accepts_known_values(self, manager: str) -> None:
        cfg = PythonRuntimeConfig.model_validate({"manager": manager})
        assert cfg.manager == manager

    def test_manager_rejects_unknown_values(self) -> None:
        with pytest.raises(ValidationError):
            PythonRuntimeConfig.model_validate({"manager": "pdm"})

    @pytest.mark.parametrize("mode", ["hardlink", "clone", "symlink", "copy"])
    def test_link_mode_accepts_known_values(self, mode: str) -> None:
        cfg = PythonRuntimeConfig.model_validate({"linkMode": mode})
        assert cfg.link_mode == mode

    def test_link_mode_rejects_unknown_values(self) -> None:
        with pytest.raises(ValidationError):
            PythonRuntimeConfig.model_validate({"linkMode": "junction"})


class TestPythonRuntimeConfigAliases:

    def test_camel_case_input_accepted(self) -> None:
        cfg = PythonRuntimeConfig.model_validate(
            {
                "manager": "uv",
                "autoBootstrap": False,
                "venvDir": ".envs/proj-A",
                "cacheDir": "/var/cache/mira-uv",
                "linkMode": "clone",
                "baselineRequirements": ["numpy", "pandas"],
                "pythonVersion": "3.11",
            }
        )
        assert cfg.manager == "uv"
        assert cfg.auto_bootstrap is False
        assert cfg.venv_dir == ".envs/proj-A"
        assert cfg.cache_dir == "/var/cache/mira-uv"
        assert cfg.link_mode == "clone"
        assert cfg.baseline_requirements == ["numpy", "pandas"]
        assert cfg.python_version == "3.11"

    def test_snake_case_input_accepted(self) -> None:
        cfg = PythonRuntimeConfig.model_validate(
            {
                "manager": "uv",
                "auto_bootstrap": False,
                "venv_dir": ".envs/proj-A",
                "python_version": "3.12",
            }
        )
        assert cfg.manager == "uv"
        assert cfg.auto_bootstrap is False
        assert cfg.venv_dir == ".envs/proj-A"
        assert cfg.python_version == "3.12"


class TestExecToolConfigEmbedsPython:

    def test_round_trip_camel_case(self) -> None:
        payload = {
            "enable": True,
            "timeout": 60,
            "pathAppend": "",
            "sandbox": "",
            "python": {
                "manager": "uv",
                "autoBootstrap": True,
                "venvDir": ".venv",
                "linkMode": "hardlink",
                "baselineRequirements": ["numpy"],
                "pythonVersion": "3.11",
            },
        }
        cfg = ExecToolConfig.model_validate(payload)
        assert cfg.python.manager == "uv"
        assert cfg.python.baseline_requirements == ["numpy"]

        dumped = cfg.model_dump(by_alias=True)
        assert dumped["python"]["manager"] == "uv"
        assert dumped["python"]["baselineRequirements"] == ["numpy"]
        assert dumped["python"]["pythonVersion"] == "3.11"

    def test_python_field_optional_in_payload(self) -> None:
        """Existing configs that omit ``python`` continue to validate."""
        cfg = ExecToolConfig.model_validate(
            {"enable": True, "timeout": 60, "pathAppend": "", "sandbox": ""}
        )
        assert cfg.python.manager == "off"
