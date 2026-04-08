from __future__ import annotations

import json
from pathlib import Path

from medpilot.config import loader
from medpilot.config.schema import Config


def test_get_and_set_config_path(tmp_path: Path, monkeypatch) -> None:
    custom = tmp_path / "custom.json"
    monkeypatch.setattr(loader, "_current_config_path", None)
    loader.set_config_path(custom)
    assert loader.get_config_path() == custom


def test_get_config_path_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setattr(loader, "_current_config_path", None)
    path = loader.get_config_path()
    assert path.name == "config.json"
    assert path.parent.name == ".medpilot"


def test_load_config_missing_file_returns_defaults(tmp_path: Path) -> None:
    cfg = loader.load_config(tmp_path / "missing.json")
    assert isinstance(cfg, Config)
    assert cfg.gateway.port == 18790
    assert cfg.agents.defaults.auto_max_rounds == 30


def test_load_config_invalid_json_prints_warning(tmp_path: Path, capsys) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")
    cfg = loader.load_config(path)
    out = capsys.readouterr().out
    assert isinstance(cfg, Config)
    assert "Warning: Failed to load config" in out
    assert "Using default configuration." in out


def test_load_config_migrates_legacy_restrict_to_workspace(tmp_path: Path) -> None:
    path = tmp_path / "cfg.json"
    path.write_text(
        json.dumps(
            {
                "tools": {
                    "exec": {"timeout": 90, "restrictToWorkspace": False},
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = loader.load_config(path)
    assert cfg.tools.exec.timeout == 90
    assert cfg.tools.restrict_to_workspace is False


def test_save_config_writes_parent_directory(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "config.json"
    cfg = Config()
    loader.save_config(cfg, out)
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "tools" in data


def test_migrate_config_only_moves_when_target_missing() -> None:
    payload = {"tools": {"exec": {"restrictToWorkspace": True}}}
    migrated = loader._migrate_config(payload)
    assert migrated["tools"]["restrictToWorkspace"] is True
    assert "restrictToWorkspace" not in migrated["tools"]["exec"]

    already_new = {"tools": {"restrictToWorkspace": False, "exec": {"restrictToWorkspace": True}}}
    untouched = loader._migrate_config(already_new)
    assert untouched["tools"]["restrictToWorkspace"] is False
    assert untouched["tools"]["exec"]["restrictToWorkspace"] is True
