"""web_search / web_fetch as a first-party plugin. The implementations stay in
the engine (mira_engine.agent.tools.web), which knowledge ingest and the UI use
too; this plugin builds them for each agent loop."""

from mira_engine.agent.tools.web import WebFetchTool, WebSearchTool


def make_search(ctx):
    # The loop's Brave key (from tools.web.search.apiKey, or set by the SDK).
    return WebSearchTool(api_key=ctx.host.get("brave_api_key"), proxy=ctx.proxy)


def make_fetch(ctx):
    return WebFetchTool(proxy=ctx.proxy)
