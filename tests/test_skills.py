import json
import os
import shutil
from pathlib import Path

from radiologybot.agent.skills import SkillsLoader


def _write_skill(base: Path, name: str, body: str) -> Path:
    d = base / name
    d.mkdir(parents=True)
    p = d / "SKILL.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_list_skills_workspace_and_builtin(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    _write_skill(ws / "skills", "ws_only", "---\ndescription: W\n---\n")
    _write_skill(builtin, "bi_only", "---\ndescription: B\n---\n")
    loader = SkillsLoader(ws, builtin_skills_dir=builtin)
    names = {s["name"]: s for s in loader.list_skills(filter_unavailable=False)}
    assert "ws_only" in names and names["ws_only"]["source"] == "workspace"
    assert "bi_only" in names and names["bi_only"]["source"] == "builtin"


def test_list_skills_workspace_overrides_builtin(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    _write_skill(ws / "skills", "dup", "workspace wins")
    _write_skill(builtin, "dup", "builtin loses")
    loader = SkillsLoader(ws, builtin_skills_dir=builtin)
    skills = loader.list_skills(filter_unavailable=False)
    dup = [s for s in skills if s["name"] == "dup"]
    assert len(dup) == 1
    assert dup[0]["source"] == "workspace"
    assert str(ws / "skills" / "dup" / "SKILL.md") == dup[0]["path"]


def test_load_skill_priority_and_missing(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    _write_skill(ws / "skills", "both", "from workspace")
    _write_skill(builtin, "both", "from builtin")
    _write_skill(builtin, "builtin_only", "only builtin")
    loader = SkillsLoader(ws, builtin_skills_dir=builtin)
    assert loader.load_skill("both") == "from workspace"
    assert loader.load_skill("builtin_only") == "only builtin"
    assert loader.load_skill("missing") is None


def test_load_skills_for_context_strips_frontmatter(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    body = "### Body\n\nhello"
    _write_skill(
        ws / "skills",
        "one",
        f"---\ndescription: D\nmetadata: '{json.dumps({'radiologybot': {}})}'\n---\n\n{body}",
    )
    builtin = tmp_path / "empty_builtin"
    builtin.mkdir()
    loader = SkillsLoader(ws, builtin_skills_dir=builtin)
    out = loader.load_skills_for_context(["one"])
    assert "### Skill: one" in out
    assert "---\ndescription:" not in out
    assert body in out


def test_build_skills_summary_xml_escape_and_requires(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    bad_bin = "nonexistent_cli_skill_req_zzzzz"
    meta = json.dumps({"radiologybot": {"requires": {"bins": [bad_bin]}}})
    _write_skill(
        ws / "skills",
        "esc&me",
        f"---\ndescription: A & B < C\nmetadata: '{meta}'\n---\n\nx",
    )
    _write_skill(builtin, "ok_skill", "---\ndescription: Fine\n---\n")
    loader = SkillsLoader(ws, builtin_skills_dir=builtin)
    xml = loader.build_skills_summary()
    assert xml.startswith("<skills>")
    assert xml.endswith("</skills>")
    assert "esc&amp;me" in xml
    assert "A &amp; B &lt; C" in xml
    assert 'available="false"' in xml
    assert "<requires>" in xml
    assert f"CLI: {bad_bin}" in xml


def test_strip_frontmatter(tmp_path: Path) -> None:
    loader = SkillsLoader(tmp_path, builtin_skills_dir=None)
    with_fm = "---\nx: 1\n---\n\nhello\n"
    assert loader._strip_frontmatter(with_fm) == "hello"
    assert loader._strip_frontmatter("no front") == "no front"


def test_parse_radiologybot_metadata() -> None:
    loader = SkillsLoader(Path("/tmp"), builtin_skills_dir=None)
    rb = json.dumps({"radiologybot": {"always": True}})
    assert loader._parse_radiologybot_metadata(rb) == {"always": True}
    oc = json.dumps({"openclaw": {"foo": 1}})
    assert loader._parse_radiologybot_metadata(oc) == {"foo": 1}
    assert loader._parse_radiologybot_metadata("not json") == {}


def test_check_requirements() -> None:
    loader = SkillsLoader(Path("/tmp"), builtin_skills_dir=None)
    assert loader._check_requirements({}) is True
    assert shutil.which("sh")
    assert loader._check_requirements({"requires": {"bins": ["sh"]}}) is True
    assert loader._check_requirements({"requires": {"bins": ["nonexistent_bin_xyz_abc_123"]}}) is False
    var = "RADIOLOGYBOT_SKILLS_TEST_UNSET_ENV_VAR"
    assert var not in os.environ
    assert loader._check_requirements({"requires": {"env": [var]}}) is False


def test_get_skill_metadata_frontmatter_and_plain(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _write_skill(ws / "skills", "fm", "---\ndescription: Hi\n---\n\nbody")
    _write_skill(ws / "skills", "plain", "no yaml here")
    loader = SkillsLoader(ws, builtin_skills_dir=None)
    meta = loader.get_skill_metadata("fm")
    assert meta is not None
    assert meta.get("description") == "Hi"
    assert loader.get_skill_metadata("plain") is None


def test_get_always_skills(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    meta_always = json.dumps({"radiologybot": {"always": True}})
    _write_skill(
        ws / "skills",
        "always_rb",
        f"---\ndescription: A\nmetadata: '{meta_always}'\n---\n\n",
    )
    _write_skill(ws / "skills", "always_key", "---\nalways: true\ndescription: B\n---\n\n")
    _write_skill(ws / "skills", "normal", "---\ndescription: C\n---\n\n")
    loader = SkillsLoader(ws, builtin_skills_dir=None)
    got = set(loader.get_always_skills())
    assert "always_rb" in got
    assert "always_key" in got
    assert "normal" not in got
