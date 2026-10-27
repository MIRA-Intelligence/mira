from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_frozen_bundle_ships_every_built_in_channel() -> None:
    # channels/registry.py finds built-in channels by scanning the package at
    # run time (pkgutil.iter_modules), which a frozen bundle can only do for
    # modules PyInstaller collected. v0.6.0rc3 listed only channels.ui and so
    # shipped without channels.community: the desktop app had no websocket to
    # the Mira Community and its inbox stayed empty while REST calls worked.
    spec = _read("mira-engine.spec")

    assert "collect_submodules('mira_engine.channels')" in spec
    channel_modules = sorted(
        p.stem for p in (ROOT / "mira_engine" / "channels").glob("*.py") if p.stem not in {"__init__", "base", "manager", "registry"}
    )
    assert {"community", "ui"} <= set(channel_modules), channel_modules

    # The release smoke test runs the frozen gateway and must see both channels come up.
    workflow = _read(".github/workflows/agent-release.yml")
    assert 'for channel in ("ui", "community"):' in workflow
    assert 'f"{channel} channel enabled" not in gateway_log' in workflow


def test_pyinstaller_spec_uses_windows_onedir_only() -> None:
    spec = _read("mira-engine.spec")

    assert 'if sys.platform == "win32":' in spec
    windows_branch = spec.split('if sys.platform == "win32":', 1)[1].split("else:", 1)[0]
    non_windows_branch = spec.split("else:", 1)[1]

    assert "exclude_binaries=True" in windows_branch
    assert "COLLECT(" in windows_branch
    assert "a.binaries" in windows_branch
    assert "a.datas" in windows_branch
    assert 'name="mira-engine"' in windows_branch

    assert "COLLECT(" not in non_windows_branch
    assert "a.binaries" in non_windows_branch
    assert "a.datas" in non_windows_branch


def test_frozen_entry_repairs_the_trust_store_before_anything_talks_tls() -> None:
    # The released macOS executable had OpenSSL's CA path compiled in from the
    # build runner (python.org framework): an empty trust store on users' Macs
    # and CERTIFICATE_VERIFY_FAILED on every aiohttp call. The entry point must
    # fix that before the CLI, the gateway or a plugin host import anything.
    entry = _read("scripts/mira_engine_entry.py")

    assert "from mira_engine.utils.tls import ensure_ca_bundle" in entry
    fix = entry.index("ensure_ca_bundle()")
    assert fix < entry.index("from mira_engine.plugins.host import main")
    assert fix < entry.index("from mira_engine.cli.agent_service import app")

    assert "'certifi'" in _read("mira-engine.spec")


def test_agent_release_uses_windows_onedir_zip_artifact() -> None:
    workflow = _read(".github/workflows/agent-release.yml")

    assert 'Path("dist") / "mira-engine" / exe_name' in workflow
    assert 'src = Path("dist") / "mira-engine"' in workflow
    assert 'exe = src / "mira-engine.exe"' in workflow
    assert "mira-engine-windows-x86_64.zip" in workflow
    assert "mira-engine-windows-x86_64.exe" not in workflow
    assert "path.relative_to(src.parent).as_posix()" in workflow
