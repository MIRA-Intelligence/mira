"""Remote disable list (design §3.6-7, mira#210).

The plugin registry publishes the plugin versions it has switched off
(``GET <registry>/plugins/disabled``). The engine polls it and records the
list in :class:`~mira_engine.plugins.state.PluginState`; from then on:

* tools and hooks of a listed plugin version stop immediately (their circuit
  breaker reports it as off);
* it is not loaded on the next start;
* a channel it provides keeps running until the gateway restarts (logged).

When the registry drops a plugin from the list, it comes back by itself. The
poller never raises: an unreachable registry just keeps the last known list.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import httpx
from loguru import logger

from mira_engine.plugins.loader import PluginRuntime

Fetch = Callable[[str], Awaitable[dict[str, Any]]]


async def _http_fetch(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


def _normalise(payload: dict[str, Any]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for item in payload.get("disabled") or []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        reasons = item.get("reasons") if isinstance(item.get("reasons"), dict) else {}
        versions = [v for v in item.get("versions") or [] if isinstance(v, str)]
        if versions:
            out[item["id"]] = {v: str(reasons.get(v) or "disabled by the plugin registry") for v in versions}
    return out


class RemoteDisablePoller:
    def __init__(
        self,
        runtime: PluginRuntime,
        registry_url: str,
        interval_s: float = 600,
        fetch: Fetch | None = None,
    ):
        self.runtime = runtime
        self.url = registry_url.rstrip("/") + "/plugins/disabled"
        self.tiers_url = registry_url.rstrip("/") + "/plugins"
        self.interval_s = max(60.0, float(interval_s))
        self._fetch = fetch or _http_fetch
        self._task: asyncio.Task | None = None

    async def poll_once(self) -> list[str]:
        """Fetch and apply the list. Returns ids of loaded plugins newly switched off.
        Also records the tiers the registry issued (applied at the next load)."""
        await self._sync_tiers()
        try:
            disabled = _normalise(await self._fetch(self.url))
        except Exception as e:  # noqa: BLE001 - keep the last known list
            logger.debug("plugin registry unreachable ({}): {}", self.url, e)
            return []
        before = {
            p.id
            for p in self.runtime.loaded()
            if self.runtime.breaker(p.id, p.manifest.version if p.manifest else None).tripped
        }
        if not self.runtime.state.set_remote(disabled):
            return []
        newly: list[str] = []
        for p in self.runtime.loaded():
            if p.id in before:
                continue
            reason = self.runtime.state.remote_reason(p.id, p.manifest.version if p.manifest else None)
            if reason:
                newly.append(p.id)
                logger.warning(
                    "plugin {} {} disabled by the plugin registry: {}{}",
                    p.id,
                    p.manifest.version if p.manifest else "",
                    reason,
                    " — its channel stops at the next gateway restart" if p.channels else "",
                )
        return newly

    async def _sync_tiers(self) -> None:
        try:
            payload = await self._fetch(self.tiers_url)
        except Exception as e:  # noqa: BLE001 - keep the last known tiers
            logger.debug("plugin registry tiers unavailable: {}", e)
            return
        tiers = {
            str(p["id"]): str(p["tier"])
            for p in payload.get("plugins") or []
            if isinstance(p, dict) and p.get("id") and p.get("tier") in ("verified", "community", "experimental")
        }
        if self.runtime.state.set_registry_tiers(tiers):
            logger.info("plugin registry tiers updated; they apply at the next gateway start")

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self.interval_s)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="plugin-remote-disable")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


def poller_from_config(config: Any) -> RemoteDisablePoller | None:
    from mira_engine.plugins import get_runtime

    plugins = getattr(config, "plugins", None)
    if plugins is None or not getattr(plugins, "remote_disable", False) or not getattr(plugins, "registry_url", ""):
        return None
    return RemoteDisablePoller(get_runtime(), plugins.registry_url, plugins.poll_interval_s)
