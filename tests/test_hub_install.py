import hashlib
import io
import json
import zipfile
from functools import partial
from pathlib import Path

import httpx
import pytest

from mira_engine.agent import skill_plugins as skill_plugins_mod
from mira_engine.agent.skill_plugins import SkillPluginManager
from mira_engine.agent.tools import hub as hub_mod
from mira_engine.agent.tools.hub import (
    HubInstallError,
    HubInstallTool,
    HubSearchTool,
    parse_slug,
    search_index,
)
from mira_engine.security import host_policy

HUB = "https://hub.test/mira-hub/"


def _zip(slug: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{slug}/SKILL.md", f"---\nname: {slug}\ndescription: Demo.\n---\n\n# {slug}\n")
        zf.writestr(f"{slug}/scripts/run.py", "print('hi')\n")
    return buf.getvalue()


def _serve(monkeypatch: pytest.MonkeyPatch, slug: str, data: bytes, *, sha256: str | None = None) -> list[str]:
    requested: list[str] = []
    index = {
        "skills": [{
            "slug": slug,
            "name": slug,
            "version": "1.2.0",
            "description": "Demo.",
            "descriptions": {"en": "Demo skill.", "zh": "演示技能。"},
            "zip": f"/mira-hub/registry/skills/{slug}-1.2.0.zip",
            "sha256": sha256 or hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/mira-hub/registry/index.json":
            return httpx.Response(200, json=index)
        if request.url.path == f"/mira-hub/registry/skills/{slug}-1.2.0.zip":
            return httpx.Response(200, content=data)
        return httpx.Response(404)

    monkeypatch.setattr(
        hub_mod.httpx, "AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
    )
    return requested


@pytest.fixture
def manager_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    global_workspace = tmp_path / "global-workspace"
    global_workspace.mkdir()
    monkeypatch.setattr(skill_plugins_mod, "get_workspace_path", lambda _workspace: global_workspace)
    return tmp_path / "project"


def test_parse_slug_accepts_slug_and_page_url() -> None:
    assert parse_slug("pubmed-search") == "pubmed-search"
    assert parse_slug("https://hub.mira-intelligence.com/skills/scanpy/") == "scanpy"
    assert parse_slug("https://mira-intelligence.github.io/mira-hub/skills/scanpy/") == "scanpy"
    with pytest.raises(HubInstallError):
        parse_slug("../etc/passwd")


async def test_installs_hub_skill_as_custom_plugin(monkeypatch: pytest.MonkeyPatch, manager_workspace: Path) -> None:
    requested = _serve(monkeypatch, "demo-skill", _zip("demo-skill"))
    tool = HubInstallTool(workspace=manager_workspace, hub_url=HUB)

    result = json.loads(await tool.execute(slug="demo-skill"))

    assert result["installed"] == "demo-skill"
    assert result["version"] == "1.2.0"
    assert requested == [f"{HUB}registry/index.json", f"{HUB}registry/skills/demo-skill-1.2.0.zip"]
    plugin = next(p for p in SkillPluginManager(manager_workspace).list_plugins() if p["id"] == "demo-skill")
    assert plugin["source"] == {"type": "mirahub", "path": f"{HUB}skills/demo-skill/"}
    assert plugin["version"] == "1.2.0"
    assert plugin["description"] == "Demo skill."
    assert [s["id"] for s in plugin["skills"]] == ["demo-skill"]
    assert plugin["skills"][0]["enabled"]["effective"] is True
    assert Path(plugin["install_path"], "demo-skill", "scripts", "run.py").is_file()


async def test_reinstall_replaces_previous_hub_install(monkeypatch: pytest.MonkeyPatch, manager_workspace: Path) -> None:
    _serve(monkeypatch, "demo-skill", _zip("demo-skill"))
    tool = HubInstallTool(workspace=manager_workspace, hub_url=HUB)
    await tool.execute(slug="demo-skill")

    result = json.loads(await tool.execute(slug="demo-skill"))

    assert "error" not in result
    ids = [p["id"] for p in SkillPluginManager(manager_workspace).list_plugins()]
    assert ids.count("demo-skill") == 1


async def test_rejects_checksum_mismatch(monkeypatch: pytest.MonkeyPatch, manager_workspace: Path) -> None:
    _serve(monkeypatch, "demo-skill", _zip("demo-skill"), sha256="0" * 64)

    result = json.loads(await HubInstallTool(workspace=manager_workspace, hub_url=HUB).execute(slug="demo-skill"))

    assert "checksum mismatch" in result["error"]
    assert all(p["id"] != "demo-skill" for p in SkillPluginManager(manager_workspace).list_plugins())


async def test_unknown_slug(monkeypatch: pytest.MonkeyPatch, manager_workspace: Path) -> None:
    _serve(monkeypatch, "demo-skill", _zip("demo-skill"))

    result = json.loads(await HubInstallTool(workspace=manager_workspace, hub_url=HUB).execute(slug="missing"))

    assert "no skill 'missing'" in result["error"]


async def test_refuses_builtin_skill_names(monkeypatch: pytest.MonkeyPatch, manager_workspace: Path) -> None:
    requested = _serve(monkeypatch, "github", _zip("github"))

    result = json.loads(await HubInstallTool(workspace=manager_workspace, hub_url=HUB).execute(slug="github"))

    assert "already built into MIRA" in result["error"]
    assert requested == []


async def test_keeps_a_local_plugin_with_the_same_id(monkeypatch: pytest.MonkeyPatch, manager_workspace: Path, tmp_path: Path) -> None:
    local = tmp_path / "demo-skill"
    (local / "demo-skill").mkdir(parents=True)
    (local / "demo-skill" / "SKILL.md").write_text("---\nname: demo-skill\n---\n", encoding="utf-8")
    SkillPluginManager(manager_workspace).install_from_directory(local)
    _serve(monkeypatch, "demo-skill", _zip("demo-skill"))

    result = json.loads(await HubInstallTool(workspace=manager_workspace, hub_url=HUB).execute(slug="demo-skill"))

    assert "didn't come from MIRA Hub" in result["error"]


async def test_host_policy_can_forbid_install(monkeypatch: pytest.MonkeyPatch, manager_workspace: Path) -> None:
    host_policy.POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    host_policy.POLICY_PATH.write_text("[tools]\nplugin_install = false\n")
    host_policy.reset_cache()
    requested = _serve(monkeypatch, "demo-skill", _zip("demo-skill"))

    result = json.loads(await HubInstallTool(workspace=manager_workspace, hub_url=HUB).execute(slug="demo-skill"))

    assert "disabled by the host policy" in result["error"]
    assert requested == []


def test_search_index_ranks_by_matching_terms() -> None:
    skills = [
        {"slug": "scanpy", "descriptions": {"en": "Single-cell RNA-seq analysis.", "zh": "单细胞 RNA-seq 分析。"}, "tags": ["domain:genomics"]},
        {"slug": "pydeseq2", "descriptions": {"en": "Bulk RNA-seq differential expression.", "zh": "差异表达。"}, "tags": []},
        {"slug": "pydicom", "descriptions": {"en": "DICOM files.", "zh": "DICOM 文件。"}, "tags": []},
    ]

    assert [m["slug"] for m in search_index(skills, "single-cell rna")] == ["scanpy", "pydeseq2"]
    assert [m["slug"] for m in search_index(skills, "单细胞")] == ["scanpy"]
    assert len(search_index(skills, "")) == 3
    assert search_index(skills, "single-cell")[0]["description_zh"] == "单细胞 RNA-seq 分析。"


async def test_hub_search_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, "demo-skill", _zip("demo-skill"))

    result = json.loads(await HubSearchTool(hub_url=HUB).execute(query="演示"))

    assert result["total"] == 1
    assert [m["slug"] for m in result["matches"]] == ["demo-skill"]
