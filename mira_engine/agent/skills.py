"""Skills loader for agent capabilities."""

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from mira_engine.agent.skill_plugins import BUILTIN_SKILLS_DIR, SkillPluginError, SkillPluginManager

# ── scan caches ────────────────────────────────────────────────────────
# Building the system prompt, routing skills and resolving always-on skills
# each walk the skill directories and re-read every SKILL.md. The inputs
# almost never change between turns, so the directory index is memoised per
# (roots, builtin dir, plugin manager paths) and invalidated when any root's
# mtime moves or after a short TTL; file contents are memoised on
# (mtime_ns, size). Several SkillsLoader instances exist per process, hence
# the module-level caches.
_INDEX_TTL_S = 5.0
_WHICH_TTL_S = 60.0
_index_cache: dict[tuple[Any, ...], tuple[float, tuple[Any, ...], dict[str, Any]]] = {}
_file_cache: dict[str, tuple[int, int, str, dict | None]] = {}


def _stat_signature(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def invalidate_skill_caches() -> None:
    """Drop every memoised skill scan (tests and plugin install paths)."""
    _index_cache.clear()
    _file_cache.clear()


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    """

    def __init__(
        self,
        workspace: Path,
        builtin_skills_dir: Path | None = BUILTIN_SKILLS_DIR,
        plugin_manager: SkillPluginManager | None = None,
    ):
        self.workspace = workspace
        from mira_engine.utils.helpers import get_mira_dir

        # Backward compatibility:
        # - legacy tests/projects place skills under "<workspace>/skills"
        # - runtime state stores skills under "<workspace>/.mira/skills"
        # Search both, preferring direct workspace path.
        direct_skills = workspace / "skills"
        mira_skills = get_mira_dir(workspace) / "skills"
        roots: list[Path] = []
        for root in (direct_skills, mira_skills):
            if all(existing != root for existing in roots):
                roots.append(root)
        self.workspace_skills_roots = roots
        # Preserve old attribute name for compatibility with existing code.
        self.workspace_skills = roots[0]
        # None explicitly disables builtin skills.
        self.builtin_skills = builtin_skills_dir
        # Test/compat behavior: when caller injects a custom builtin dir, avoid
        # auto-discovering global/plugin skills unless explicitly requested.
        if plugin_manager is not None:
            self.plugin_manager = plugin_manager
        elif builtin_skills_dir is BUILTIN_SKILLS_DIR:
            self.plugin_manager = SkillPluginManager(workspace)
        else:
            self.plugin_manager = None
        # PATH lookups for skill `requires.bins`, memoised per loader so a
        # single prompt build does not re-run `which` dozens of times.
        self._which_cache: dict[str, tuple[float, bool, Any]] = {}

    def _which_cached(self, binary: str) -> bool:
        now = time.monotonic()
        which = shutil.which
        hit = self._which_cache.get(binary)
        # The cached answer is only valid for the same resolver (tests swap it).
        if hit is not None and now - hit[0] < _WHICH_TTL_S and hit[2] is which:
            return hit[1]
        found = which(binary) is not None
        self._which_cache[binary] = (now, found, which)
        return found

    def _list_plugin_skills(self) -> list[dict[str, str]]:
        if self.plugin_manager is None:
            return []
        try:
            return self.plugin_manager.list_enabled_skills()
        except SkillPluginError:
            return []

    def _managed_skill_names(self) -> set[str]:
        if self.plugin_manager is None:
            return set()
        try:
            return self.plugin_manager.get_managed_skill_names()
        except SkillPluginError:
            return set()

    def _plugin_skill_path_by_name(self, name: str) -> str | None:
        entry = self._index()["by_name"].get(name)
        if entry is not None and entry[1] == "plugin":
            return entry[0]
        return None

    def _builtin_skill_path_by_name(self, name: str) -> Path | None:
        entry = self._index()["by_name"].get(name)
        if entry is not None and entry[1] == "builtin":
            return Path(entry[0])
        return None

    # ── directory index ──────────────────────────────────────────────

    def _index_key(self) -> tuple[Any, ...]:
        pm = self.plugin_manager
        return (
            tuple(str(r) for r in self.workspace_skills_roots),
            str(self.builtin_skills) if self.builtin_skills else None,
            (str(pm.global_state_path), str(pm.project_overrides_path), str(pm.plugins_root)) if pm else None,
        )

    def _index_fingerprint(self) -> tuple[Any, ...]:
        paths: list[Path] = list(self.workspace_skills_roots)
        if self.builtin_skills:
            paths.append(self.builtin_skills)
        pm = self.plugin_manager
        if pm is not None:
            paths.extend((pm.global_state_path, pm.project_overrides_path, pm.plugins_root))
        return tuple(_stat_signature(p) for p in paths)

    def _index(self) -> dict[str, Any]:
        key = self._index_key()
        now = time.monotonic()
        cached = _index_cache.get(key)
        if cached is not None and now - cached[0] < _INDEX_TTL_S and cached[1] == self._index_fingerprint():
            return cached[2]
        index = self._build_index()
        _index_cache[key] = (now, self._index_fingerprint(), index)
        return index

    def _build_index(self) -> dict[str, Any]:
        skills: list[dict[str, str]] = []
        by_name: dict[str, tuple[str, str]] = {}

        def add(name: str, path: str, source: str) -> None:
            if name in by_name:
                return
            by_name[name] = (path, source)
            skills.append({"name": name, "path": path, "source": source})

        # Workspace skills (highest priority)
        for root in self.workspace_skills_roots:
            if not root.exists():
                continue
            for skill_file in sorted(root.rglob("SKILL.md")):
                if skill_file.is_file():
                    add(skill_file.parent.name, str(skill_file), "workspace")

        # Plugin skills (global install + scope toggles)
        for plugin_skill in self._list_plugin_skills():
            name = plugin_skill.get("name")
            path = plugin_skill.get("path")
            if isinstance(name, str) and isinstance(path, str):
                add(name, path, "plugin")

        # Built-in skills, minus the ones a plugin manages (possibly disabled)
        managed_names = self._managed_skill_names()
        if self.builtin_skills and self.builtin_skills.exists():
            for skill_file in sorted(self.builtin_skills.rglob("SKILL.md")):
                if not skill_file.is_file():
                    continue
                skill_name = skill_file.parent.name
                if skill_name in managed_names:
                    continue
                add(skill_name, str(skill_file), "builtin")

        return {"skills": skills, "by_name": by_name}

    @staticmethod
    def _read_skill_file(path: Path) -> str | None:
        """Read a SKILL.md, memoised on (mtime, size)."""
        sig = _stat_signature(path)
        if sig is None:
            return None
        key = str(path)
        hit = _file_cache.get(key)
        if hit is not None and hit[0] == sig[0] and hit[1] == sig[1]:
            return hit[2]
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return None
        _file_cache[key] = (sig[0], sig[1], content, SkillsLoader._parse_frontmatter(content))
        return content

    @staticmethod
    def _parse_frontmatter(content: str) -> dict | None:
        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                # Simple YAML parsing
                metadata = {}
                for line in match.group(1).split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        metadata[key.strip()] = value.strip().strip('"\'')
                return metadata
        return None

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source'.
        """
        skills = [dict(s) for s in self._index()["skills"]]
        if filter_unavailable:
            return [s for s in skills if self._check_requirements(self._get_skill_meta(s["name"]))]
        return skills

    def _skill_path(self, name: str) -> Path | None:
        # Direct workspace layout wins even when the index is a few seconds old.
        for root in self.workspace_skills_roots:
            workspace_skill = root / name / "SKILL.md"
            if workspace_skill.exists():
                return workspace_skill
        entry = self._index()["by_name"].get(name)
        if entry is None:
            return None
        return Path(entry[0])

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

        Args:
            name: Skill name (directory name).

        Returns:
            Skill content or None if not found.
        """
        path = self._skill_path(name)
        if path is None:
            return None
        return self._read_skill_file(path)

    def load_skills_for_context(self, skill_names: list[str], *, loaded_skills: list[str] | None = None) -> str:
        """
        Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.
        """
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                if content.strip():
                    parts.append(f"### Skill: {name}\n\n{content}")
                    if loaded_skills is not None:
                        loaded_skills.append(name)

        return "\n\n---\n\n".join(parts) if parts else ""

    def build_skills_summary(self) -> str:
        """
        Build a summary of all skills (name, description, path, availability).

        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed.

        Returns:
            XML-formatted skills summary.
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        def escape_xml(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<skills>"]
        for s in all_skills:
            name = escape_xml(s["name"])
            path = s["path"]
            desc = escape_xml(self._get_skill_description(s["name"]))
            skill_meta = self._get_skill_meta(s["name"])
            available = self._check_requirements(skill_meta)

            lines.append(f"  <skill available=\"{str(available).lower()}\">")
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{desc}</description>")
            lines.append(f"    <location>{path}</location>")

            # Show missing requirements for unavailable skills
            if not available:
                missing = self._get_missing_requirements(skill_meta)
                if missing:
                    lines.append(f"    <requires>{escape_xml(missing)}</requires>")

            lines.append("  </skill>")
        lines.append("</skills>")

        return "\n".join(lines)

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        missing = []
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not self._which_cached(b):
                missing.append(f"CLI: {b}")
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        return ", ".join(missing)

    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # Fallback to skill name

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content

    def _parse_mira_metadata(self, raw: str) -> dict:
        """Parse skill metadata JSON from frontmatter (supports mira and openclaw keys)."""
        try:
            data = json.loads(raw)
            return data.get("mira", data.get("openclaw", {})) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not self._which_cached(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        return True

    def _get_skill_meta(self, name: str) -> dict:
        """Get mira metadata for a skill (cached in frontmatter)."""
        meta = self.get_skill_metadata(name) or {}
        return self._parse_mira_metadata(meta.get("metadata", ""))

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        result = []
        for s in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_mira_metadata(meta.get("metadata", ""))
            if skill_meta.get("always") or meta.get("always"):
                result.append(s["name"])
        return result

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict or None.
        """
        path = self._skill_path(name)
        if path is None:
            return None
        content = self._read_skill_file(path)
        if not content:
            return None
        hit = _file_cache.get(str(path))
        meta = hit[3] if hit is not None else self._parse_frontmatter(content)
        return dict(meta) if meta is not None else None

    def _parse_frontmatter_list(self, raw: str | None) -> list[str]:
        """Parse a JSON array string from frontmatter (scenarios, aliases, etc.)."""
        if not raw:
            return []
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(item) for item in data if isinstance(item, str)]
        except (json.JSONDecodeError, TypeError):
            pass
        return []

    def suggest_skills(
        self,
        query: str,
        *,
        recent: list[str] | None = None,
        limit: int = 3,
    ) -> list[str]:
        """Suggest likely relevant skills for a user query."""
        text = (query or "").strip()
        if not text:
            return []
        if limit < 1:
            limit = 1

        available = self.list_skills(filter_unavailable=True)
        if not available:
            return []

        recent_names = [name for name in (recent or []) if isinstance(name, str)]
        available_names = {s["name"] for s in available}
        recent_set = {n for n in recent_names if n in available_names}
        is_follow_up = self._looks_like_follow_up(text)

        query_tokens = self._tokenize(text)
        query_lc = text.lower()
        scored: list[tuple[int, str]] = []

        for entry in available:
            name = entry["name"]
            meta = self.get_skill_metadata(name) or {}
            desc = str(meta.get("description") or "")
            # Installation paths are not evidence of the user's task.
            base_text = " ".join((name, desc))
            score = self._score_skill_match(
                query_lc=query_lc,
                query_tokens=query_tokens,
                skill_name=name,
                skill_text=base_text,
                meta=meta,
            )

            # History is relevant only when the user asks to resume that task.
            if is_follow_up and name in recent_set:
                score += 25

            if score >= 4:
                scored.append((score, name))

        scored.sort(key=lambda item: (-item[0], item[1]))
        result: list[str] = []
        for _, name in scored:
            if name not in result:
                result.append(name)
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def _looks_like_follow_up(text: str) -> bool:
        # Mentioning "previous"/"之前" is not itself a request to continue.
        # Require an affirmative continuation instruction, not an incidental
        # substring (e.g. "resume" in a CV filename or "don't continue").
        return bool(re.match(
            r"^\s*(?:(?:请(?:你)?|麻烦(?:你)?|帮我|那就|好的)[，,\s]*)*(?:继续|接着|沿用|接上|恢复(?:之前|上次|刚才))"
            r"|^\s*(?:(?:please|ok(?:ay)?|yes|let['’]s)[,\s]+)*(?:continue|resume|carry on|pick up where)(?=\s|[.!?]*$)",
            text, re.IGNORECASE,
        ))

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        lowered = text.lower()
        latin = re.findall(r"[a-z0-9][a-z0-9._+-]*", lowered)
        cjk_chunks = re.findall(r"[\u4e00-\u9fff]+", text)
        tokens: set[str] = set(latin)
        for chunk in cjk_chunks:
            tokens.add(chunk)
            if len(chunk) > 1:
                tokens.update(chunk)
        # Shared grammatical words are not task evidence. Otherwise two words
        # such as "a" and "with" alone reach the automatic loading threshold.
        stop_words = {
            "a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "by", "for", "from", "with",
            "is", "are", "was", "were", "be", "as", "it", "its", "this", "that", "these", "those",
            "i", "me", "my", "you", "your", "we", "our", "can", "could", "would", "should", "please",
        }
        return {t for t in tokens if t and t not in stop_words}

    @staticmethod
    def _skill_aliases(skill_name: str) -> tuple[str, ...]:
        aliases: dict[str, tuple[str, ...]] = {
            "medical-image-analysis": (
                "medical", "imaging", "mri", "ct", "dicom", "nifti", "monai", "artifact", "motion",
                "ghost", "k-space", "2.5d", "3d", "unet", "radiology", "医学", "影像", "伪影", "呼吸", "运动",
                "去伪影",
            ),
            "dicom2nifti": ("dicom", "nifti", "医学", "影像", "转换"),
            "monai": ("monai", "medical", "imaging", "医学", "影像"),
        }
        return aliases.get(skill_name, ())

    def _score_skill_match(
        self,
        *,
        query_lc: str,
        query_tokens: set[str],
        skill_name: str,
        skill_text: str,
        meta: dict | None = None,
    ) -> int:
        score = 0
        name_tokens = self._tokenize(skill_name.replace("-", " "))
        score += len(name_tokens.intersection(query_tokens)) * 4

        skill_tokens = self._tokenize(skill_text)
        score += len(skill_tokens.intersection(query_tokens)) * 2

        # Aliases from frontmatter (highest priority)
        fm_aliases: list[str] = []
        if meta:
            raw_aliases = meta.get("aliases")
            if raw_aliases:
                fm_aliases = self._parse_frontmatter_list(raw_aliases)

        # Scenarios from frontmatter: tokenize and match
        if meta:
            raw_scenarios = meta.get("scenarios")
            if raw_scenarios:
                scenarios = self._parse_frontmatter_list(raw_scenarios)
                for scenario in scenarios:
                    scenario_tokens = self._tokenize(scenario)
                    score += len(scenario_tokens.intersection(query_tokens)) * 2
                    scenario_lc = scenario.lower()
                    if scenario_lc and scenario_lc in query_lc:
                        score += 4

        # Aliases: use frontmatter first, fall back to hardcoded
        if fm_aliases:
            alias_list = fm_aliases
        else:
            alias_list = list(self._skill_aliases(skill_name))

        for alias in alias_list:
            alias_lc = alias.strip().lower()
            if not alias_lc:
                continue
            # Latin aliases must match token boundaries: CT != introduction.
            # CJK phrases and file extensions still work inside natural text.
            prefix = r"(?<![a-z0-9_])" if re.match(r"[a-z0-9_]", alias_lc) else ""
            suffix = r"(?![a-z0-9_])" if re.search(r"[a-z0-9_]$", alias_lc) else ""
            if re.search(prefix + re.escape(alias_lc) + suffix, query_lc):
                score += 6
        return score

def skill_access_dirs(workspace: Path) -> list[Path]:
    """Skill directories tools may read besides ``workspace`` (and the sandbox mounts)."""
    dirs: list[Path] = []
    workspace = workspace.resolve()

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except Exception:
            return
        if resolved != workspace and resolved not in dirs:
            dirs.append(resolved)

    loader = SkillsLoader(workspace)
    for root in loader.workspace_skills_roots:
        add(root)
    if loader.builtin_skills:
        add(loader.builtin_skills)
    for skill in loader.list_skills(filter_unavailable=False):
        skill_path = Path(skill["path"])
        add(skill_path.parent)
        parent = skill_path.parent.parent
        if parent != skill_path.parent:
            add(parent)
    return dirs
