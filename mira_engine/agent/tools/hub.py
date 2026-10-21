"""Search and install MIRA Hub skills, each in one tool call."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from mira_engine.agent.skill_plugins import SkillPluginError, SkillPluginManager
from mira_engine.agent.tools.base import Tool
from mira_engine.security.host_policy import HostPolicyError, load_host_policy

HUB_URL = "https://mira-intelligence.github.io/mira-hub/"
SOURCE_TYPE = "mirahub"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PAGE_SLUG_RE = re.compile(r"/skills/([a-z0-9][a-z0-9-]{0,63})/?$")
_MAX_ZIP_BYTES = 50 * 1024 * 1024
_SEARCH_LIMIT = 10


class HubInstallError(Exception):
    """A hub install that cannot proceed; the message is shown to the agent."""


def parse_slug(value: str) -> str:
    """Accept a bare slug or a hub skill page URL (…/mira-hub/skills/<slug>/)."""
    text = value.strip()
    if _SLUG_RE.match(text):
        return text
    match = _PAGE_SLUG_RE.search(urlparse(text).path)
    if match:
        return match.group(1)
    raise HubInstallError(f"not a MIRA Hub skill slug or page URL: {value!r}")


async def fetch_index(client: httpx.AsyncClient, hub_url: str) -> list[dict[str, Any]]:
    response = await client.get(urljoin(hub_url, "registry/index.json"))
    response.raise_for_status()
    skills = response.json().get("skills")
    return skills if isinstance(skills, list) else []


def search_index(skills: list[dict[str, Any]], query: str, limit: int = _SEARCH_LIMIT) -> list[dict[str, Any]]:
    """Rank skills by how many query terms appear in their slug, name, abstracts and tags."""
    terms = [t for t in query.lower().split() if t]
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for skill in skills:
        descriptions = skill.get("descriptions") or {}
        haystack = " ".join([
            str(skill.get("slug", "")),
            str(skill.get("name", "")),
            str(descriptions.get("en") or skill.get("description") or ""),
            str(descriptions.get("zh") or ""),
            " ".join(skill.get("tags") or []),
        ]).lower()
        score = sum(term in haystack for term in terms) if terms else 1
        if score:
            ranked.append((score, str(skill.get("slug", "")), skill))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [
        {
            "slug": skill.get("slug"),
            "version": skill.get("version"),
            "description": (skill.get("descriptions") or {}).get("en") or skill.get("description"),
            "description_zh": (skill.get("descriptions") or {}).get("zh"),
            "tags": skill.get("tags"),
        }
        for _, _, skill in ranked[:limit]
    ]


class HubSearchTool(Tool):
    """Find skills published on MIRA Hub."""

    def __init__(self, proxy: str | None = None, hub_url: str = HUB_URL) -> None:
        self.proxy = proxy
        self.hub_url = hub_url if hub_url.endswith("/") else f"{hub_url}/"

    @property
    def name(self) -> str:
        return "hub_search"

    @property
    def description(self) -> str:
        return (
            "Search MIRA Hub, the MIRA skill marketplace, for installable skills (research, "
            "medical, bioinformatics, documents, coding). Returns the best matches with slug, "
            "version, English/Chinese abstract and tags; install one with hub_install."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords in English or Chinese, e.g. 'single-cell rna' or '单细胞'. Empty lists everything.",
                },
            },
            "required": ["query"],
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, query: str, **kwargs: Any) -> str:
        try:
            async with httpx.AsyncClient(proxy=self.proxy, timeout=30.0, follow_redirects=True) as client:
                skills = await fetch_index(client, self.hub_url)
        except httpx.HTTPError as exc:
            return json.dumps({"error": f"MIRA Hub request failed: {exc}"}, ensure_ascii=False)
        matches = search_index(skills, query)
        return json.dumps({"query": query, "total": len(skills), "matches": matches}, ensure_ascii=False)


class HubInstallTool(Tool):
    """Download a published hub skill, verify it, and install it as a skill plugin."""

    def __init__(self, workspace: Path, proxy: str | None = None, hub_url: str = HUB_URL) -> None:
        self.workspace = workspace
        self.proxy = proxy
        self.hub_url = hub_url if hub_url.endswith("/") else f"{hub_url}/"

    @property
    def name(self) -> str:
        return "hub_install"

    @property
    def description(self) -> str:
        return (
            "Install a skill from MIRA Hub (mira-intelligence.github.io/mira-hub) by its slug "
            "or hub page URL. One call downloads the published zip, verifies its sha256 and "
            "installs it as a custom skill plugin for all projects (Settings → Skills & Plugins). "
            "Use this for any MIRA Hub install instead of downloading or unpacking archives "
            "with exec. Reinstalling a slug updates it to the hub's current version."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Hub skill slug (e.g. 'pubmed-search') or its hub page URL.",
                },
            },
            "required": ["slug"],
        }

    async def execute(self, slug: str, **kwargs: Any) -> str:
        try:
            result = await self._install(parse_slug(slug))
        except (HubInstallError, SkillPluginError, HostPolicyError) as exc:
            return json.dumps({"error": str(exc), "slug": slug}, ensure_ascii=False)
        except httpx.HTTPError as exc:
            return json.dumps({"error": f"MIRA Hub request failed: {exc}", "slug": slug}, ensure_ascii=False)
        return json.dumps(result, ensure_ascii=False)

    async def _install(self, slug: str) -> dict[str, Any]:
        policy = load_host_policy()
        if policy.plugin_install_disabled:
            raise HubInstallError(f"installing skill plugins is disabled by the host policy ({policy.source})")

        manager = SkillPluginManager(self.workspace)
        for plugin in manager.list_plugins():
            if plugin["id"] == "builtin-skills":
                # Built-ins win name clashes, so a hub copy would never load.
                if any(skill["id"] == slug for skill in plugin["skills"]):
                    raise HubInstallError(f"'{slug}' is already built into MIRA; nothing to install")
            elif plugin["id"] == slug and plugin["source"].get("type") != SOURCE_TYPE:
                raise HubInstallError(
                    f"a custom plugin '{slug}' that didn't come from MIRA Hub is already installed; "
                    "remove it in Settings → Skills & Plugins first"
                )

        async with httpx.AsyncClient(proxy=self.proxy, timeout=60.0, follow_redirects=True) as client:
            skills = await fetch_index(client, self.hub_url)
            entry = next((s for s in skills if s.get("slug") == slug), None)
            if entry is None:
                raise HubInstallError(f"no skill '{slug}' on MIRA Hub")
            if entry.get("size", 0) > _MAX_ZIP_BYTES:
                raise HubInstallError(f"'{slug}' is larger than {_MAX_ZIP_BYTES // (1024 * 1024)} MB")
            response = await client.get(urljoin(self.hub_url, entry["zip"]))
            response.raise_for_status()
            data = response.content

        digest = hashlib.sha256(data).hexdigest()
        if digest != entry.get("sha256"):
            raise HubInstallError(f"checksum mismatch for '{slug}' (expected {entry.get('sha256')}, got {digest})")

        page_url = urljoin(self.hub_url, f"skills/{slug}/")
        defaults = {
            "name": entry.get("name") or slug,
            "version": entry.get("version") or "",
            "description": (entry.get("descriptions") or {}).get("en") or entry.get("description") or "",
        }
        installed = await asyncio.to_thread(self._install_zip, manager, slug, data, page_url, defaults)
        return {
            "installed": slug,
            "version": entry.get("version"),
            "plugin_id": installed["id"],
            "skills": [s["id"] for s in installed.get("skills", [])],
            "page": page_url,
            "note": "Installed as a custom plugin; the skill is available from the next message.",
        }

    @staticmethod
    def _install_zip(
        manager: SkillPluginManager,
        slug: str,
        data: bytes,
        page_url: str,
        defaults: dict[str, str],
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="mirahub-") as tmp:
            archive = Path(tmp) / f"{slug}.zip"
            archive.write_bytes(data)
            return manager.install_from_zip(
                archive,
                archive_name_hint=archive.name,
                source_type=SOURCE_TYPE,
                source_path=page_url,
                manifest_defaults=defaults,
            )
