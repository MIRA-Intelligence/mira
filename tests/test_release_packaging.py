from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


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


def test_agent_release_uses_windows_onedir_zip_artifact() -> None:
    workflow = _read(".github/workflows/agent-release.yml")

    assert 'Path("dist") / "mira-engine" / exe_name' in workflow
    assert 'src = Path("dist") / "mira-engine"' in workflow
    assert 'exe = src / "mira-engine.exe"' in workflow
    assert "mira-engine-windows-x86_64.zip" in workflow
    assert "mira-engine-windows-x86_64.exe" not in workflow
    assert "path.relative_to(src.parent).as_posix()" in workflow
