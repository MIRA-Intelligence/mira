from radiologybot.agent.routing import ModelRouter
from radiologybot.config.schema import AgentDefaults


def _defaults(**overrides) -> AgentDefaults:
    base = {
        "model": "anthropic/claude-opus-4-5",
        "small_model": "openai/gpt-4.1-mini",
        "medium_model": "anthropic/claude-sonnet-4-5",
        "large_model": "anthropic/claude-opus-4-5",
        "route_by_complexity": True,
    }
    base.update(overrides)
    return AgentDefaults(**base)


def test_router_falls_back_to_default_model_when_disabled() -> None:
    router = ModelRouter(_defaults(route_by_complexity=False))

    route = router.route([{"role": "user", "content": "hello"}])

    assert route.tier == "default"
    assert route.model == "anthropic/claude-opus-4-5"


def test_router_uses_small_model_for_simple_tasks() -> None:
    router = ModelRouter(_defaults())

    route = router.route([{"role": "user", "content": "summarize this in one sentence"}])

    assert route.tier == "small"
    assert route.model == "openai/gpt-4.1-mini"


def test_router_uses_medium_model_for_code_change_requests() -> None:
    router = ModelRouter(_defaults())

    route = router.route(
        [
            {
                "role": "user",
                "content": "Please debug this error and update two files. Also compare the trade-off before implementing.",
            }
        ]
    )

    assert route.tier == "medium"
    assert route.model == "anthropic/claude-sonnet-4-5"


def test_router_uses_large_model_for_long_multi_step_requests() -> None:
    router = ModelRouter(_defaults())
    long_request = " ".join([
        "Design a multi-step architecture review and refactor plan with benchmarking, analysis, and pipeline changes."
    ] * 20)

    route = router.route([{"role": "user", "content": long_request}], iteration=4)

    assert route.tier == "large"
    assert route.model == "anthropic/claude-opus-4-5"
