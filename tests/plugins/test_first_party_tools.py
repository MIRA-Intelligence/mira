"""web_search / web_fetch / hub_search / hub_install come from first-party plugins."""

from __future__ import annotations

from pathlib import Path

from mira_engine.plugins import get_runtime, set_runtime_for_tests
from mira_engine.plugins.state import PluginState


def _loop(tmp_path: Path, **kw):
    from mira_engine.agent.base_loop import BaseAgentLoop
    from mira_engine.bus.queue import MessageBus
    from mira_engine.config.schema import ChannelsConfig, ExecToolConfig
    from mira_engine.providers.base import LLMProvider, LLMResponse
    from mira_engine.session.manager import SessionManager

    class _Provider(LLMProvider):
        async def chat(self, *a, **k):
            return LLMResponse(content="ok")

        def get_default_model(self):
            return "dummy/default"

    return BaseAgentLoop(
        bus=MessageBus(),
        provider=_Provider(),
        workspace=tmp_path / "ws",
        model="dummy/default",
        channels_config=ChannelsConfig(),
        exec_config=ExecToolConfig(timeout=5),
        session_manager=SessionManager(tmp_path / "ws"),
        **kw,
    )


def test_the_loop_gets_web_and_hub_tools_from_their_plugins(tmp_path):
    loop = _loop(tmp_path, brave_api_key="bk-test", web_proxy="http://proxy:3128")
    search = loop.tools.get("web_search")
    assert search.plugin_id == "web"
    assert search._inner.api_key == "bk-test" and search._inner.proxy == "http://proxy:3128"
    assert loop.tools.get("web_fetch")._inner.proxy == "http://proxy:3128"
    install = loop.tools.get("hub_install")
    assert install.plugin_id == "mira-hub" and install._inner.workspace == tmp_path / "ws"
    assert loop.tools.has("hub_search")


def test_disabling_the_web_plugin_removes_its_tools(tmp_path):
    PluginState().set_enabled("web", False)
    set_runtime_for_tests(None)
    assert get_runtime().get("web").status == "disabled"
    loop = _loop(tmp_path)
    assert not loop.tools.has("web_search") and not loop.tools.has("web_fetch")
    assert loop.tools.has("hub_install")


def test_subagents_with_a_role_still_get_web_tools(tmp_path):
    from mira_engine.agent.subagent import ROLE_CONFIGS, SubagentManager
    from mira_engine.config.schema import ExecToolConfig

    role = next(r for r, cfg in ROLE_CONFIGS.items() if "web_search" in (cfg.get("allowed_tools") or ()))
    loop = _loop(tmp_path)
    manager = SubagentManager(
        provider=loop.provider, workspace=tmp_path / "ws", bus=loop.bus, model="dummy/default",
        brave_api_key="bk-sub", exec_config=ExecToolConfig(timeout=5),
    )
    tools = manager._build_tools(tmp_path / "ws", role)
    assert tools.get("web_search")._inner.api_key == "bk-sub"
    assert not tools.has("hub_install")  # not in that role's allowlist
