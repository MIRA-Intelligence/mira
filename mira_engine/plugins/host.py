"""Out-of-process plugin host (design §3.5, mira#211).

``python -m mira_engine.plugins.host <plugin-dir>`` loads one plugin's tools in
a separate process (sandboxed by the engine when possible) and serves them over
stdio, one JSON object per line:

    -> {"id": 1, "method": "describe"}
    <- {"id": 1, "result": {"tools": [{"name", "description", "parameters", "read_only"}]}}
    -> {"id": 2, "method": "call", "params": {"tool": "x", "args": {...}}}
    <- {"id": 2, "result": "..."}            or  {"id": 2, "error": "..."}

``--describe`` prints the description once and exits (used at load time).

Whatever the plugin does — crash, hang, leak memory — happens in this process,
not in the engine. Plugin output on stdout would corrupt the protocol, so the
real stdout is reserved for it and ``print`` goes to stderr.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, TextIO

_MEMORY_LIMIT_BYTES = int(os.environ.get("MIRA_PLUGIN_MEMORY_LIMIT", str(2 * 1024**3)))


def _limit_resources() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (_MEMORY_LIMIT_BYTES, _MEMORY_LIMIT_BYTES))
    except Exception:  # noqa: BLE001 - best effort (not available everywhere)
        pass


def _context(manifest: Any) -> Any:
    """The plugin's context, from what the engine passed in the environment:
    its scratch dir, the workspace (only when the manifest asks for it) and
    the config values it declared in ``permissions.secrets``."""
    from mira_engine.plugins.context import ConfigView, PluginContext

    try:
        values = json.loads(os.environ.get("MIRA_PLUGIN_CONFIG") or "{}")
    except ValueError:
        values = {}
    workspace = os.environ.get("MIRA_PLUGIN_WORKSPACE")
    data_dir = os.environ.get("MIRA_PLUGIN_DATA_DIR")
    return PluginContext(
        plugin_id=manifest.id,
        tier=os.environ.get("MIRA_PLUGIN_TIER") or "experimental",
        workspace=Path(workspace) if workspace else None,
        config=ConfigView.from_values(values if isinstance(values, dict) else {}),
        data_root=Path(data_dir) if data_dir else None,
    )


def _load_tools(plugin_dir: Path) -> dict[str, Any]:
    from mira_engine.agent.tools.base import Tool
    from mira_engine.plugins.loader import _instantiate, _resolve
    from mira_engine.plugins.manifest import load_manifest

    manifest = load_manifest(plugin_dir)
    ctx = _context(manifest)
    tools: dict[str, Any] = {}
    for ep in manifest.tools:
        tool = _instantiate(_resolve(manifest, ep.entry), Tool, ep.entry, ctx)
        tools[tool.name] = tool  # type: ignore[attr-defined]
    return tools


def _describe(tools: dict[str, Any]) -> dict[str, Any]:
    return {
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
                "read_only": bool(t.read_only),
            }
            for t in tools.values()
        ]
    }


async def _serve(tools: dict[str, Any], out: TextIO) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    def reply(msg: dict[str, Any]) -> None:
        out.write(json.dumps(msg, default=str) + "\n")
        out.flush()

    while line := await reader.readline():
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid = req.get("id")
        try:
            if req.get("method") == "describe":
                reply({"id": rid, "result": _describe(tools)})
            elif req.get("method") == "call":
                params = req.get("params") or {}
                tool = tools.get(params.get("tool"))
                if tool is None:
                    reply({"id": rid, "error": f"unknown tool {params.get('tool')!r}"})
                    continue
                result = await tool.execute(**(params.get("args") or {}))
                reply({"id": rid, "result": result if isinstance(result, str) else json.dumps(result, default=str)})
            else:
                reply({"id": rid, "error": f"unknown method {req.get('method')!r}"})
        except Exception as e:  # noqa: BLE001 - report, keep serving
            reply({"id": rid, "error": f"{type(e).__name__}: {e}"})


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    describe_only = "--describe" in args
    args = [a for a in args if a != "--describe"]
    if len(args) != 1:
        print("usage: python -m mira_engine.plugins.host [--describe] <plugin-dir>", file=sys.stderr)
        return 2
    _limit_resources()
    protocol_out = sys.stdout
    sys.stdout = sys.stderr  # plugin prints must not corrupt the protocol
    try:
        tools = _load_tools(Path(args[0]))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), file=protocol_out, flush=True)
        return 1
    if describe_only:
        print(json.dumps(_describe(tools)), file=protocol_out, flush=True)
        return 0
    asyncio.run(_serve(tools, protocol_out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
