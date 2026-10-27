"""hub_search / hub_install as a first-party plugin, built for each agent loop
over the engine's implementation (mira_engine.agent.tools.hub)."""

from mira_engine.agent.tools.hub import HubInstallTool, HubSearchTool


def make_search(ctx):
    return HubSearchTool(proxy=ctx.proxy)


def make_install(ctx):
    return HubInstallTool(workspace=ctx.workspace, proxy=ctx.proxy)
