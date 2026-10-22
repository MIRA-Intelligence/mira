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
    # Requests are read in a worker thread: the event loop can't read stdin
    # itself on Windows (connect_read_pipe is POSIX-only), and a thread has
    # no line-length limit, so a call with large arguments gets through.
    stdin = sys.stdin.buffer

    def reply(msg: dict[str, Any]) -> None:
        out.write(json.dumps(msg, default=str) + "\n")
        out.flush()

    while line := await loop.run_in_executor(None, stdin.readline):
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


def _guard(plugin_dir: Path) -> None:
    """Run the rest of this process as the plugin (``plugins.permissions``):
    the tripwire backs up the sandbox, and is all there is without one — e.g.
    starting programs without ``shell: true``."""
    from mira_engine.plugins import permissions
    from mira_engine.plugins.manifest import load_manifest

    manifest = load_manifest(plugin_dir)
    env = os.environ
    grant = permissions.grant_for(
        manifest,
        env.get("MIRA_PLUGIN_TIER") or "experimental",
        workspace=Path(env["MIRA_PLUGIN_WORKSPACE"]) if env.get("MIRA_PLUGIN_WORKSPACE") else None,
        data_dir=Path(env["MIRA_PLUGIN_DATA_DIR"]) if env.get("MIRA_PLUGIN_DATA_DIR") else None,
        mira_home=Path(env["MIRA_PLUGIN_MIRA_HOME"]) if env.get("MIRA_PLUGIN_MIRA_HOME") else None,
    )
    if grant is not None:
        permissions.install()
        permissions._current.set(grant)


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
    plugin_dir = Path(args[0])
    if describe_only:
        tools = _start(plugin_dir, protocol_out)
        if tools is None:
            return 1
        print(json.dumps(_describe(tools)), file=protocol_out, flush=True)
        return 0
    return asyncio.run(_run(plugin_dir, protocol_out))


def _start(plugin_dir: Path, out: TextIO) -> dict[str, Any] | None:
    """Arm the plugin's grant and load its tools; on failure report on the
    protocol stream and return None."""
    try:
        _guard(plugin_dir)
        return _load_tools(plugin_dir)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), file=out, flush=True)
        return None


async def _run(plugin_dir: Path, out: TextIO) -> int:
    # The event loop exists before the plugin's grant is armed: on Windows
    # asyncio's Proactor loop connects a loopback socketpair for its own
    # wake-ups, which a plugin without network permission may not do. The
    # grant is set in this task's context, which every plugin task inherits.
    tools = _start(plugin_dir, out)
    if tools is None:
        return 1
    await _serve(tools, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
