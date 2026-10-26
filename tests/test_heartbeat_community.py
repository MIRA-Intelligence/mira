"""Heartbeat extra-context wiring tests (#112)."""

from types import SimpleNamespace

from mira_engine.heartbeat.service import HeartbeatService


def _service(tmp_path, **kw):
    return HeartbeatService(
        workspace=tmp_path,
        provider=SimpleNamespace(),
        model="m",
        **kw,
    )


async def test_build_content_combines_file_and_extra(tmp_path):
    (tmp_path / "HEARTBEAT.md").write_text("local tasks", encoding="utf-8")

    async def extra():
        return "community digest"

    svc = _service(tmp_path, extra_context=extra)
    content = await svc._build_content()
    assert "local tasks" in content
    assert "community digest" in content


async def test_build_content_extra_only(tmp_path):
    async def extra():
        return "community digest"

    svc = _service(tmp_path, extra_context=extra)
    content = await svc._build_content()
    assert content.strip() == "community digest"


async def test_build_content_empty_when_nothing(tmp_path):
    svc = _service(tmp_path)
    assert await svc._build_content() == ""


async def test_extra_context_failure_is_swallowed(tmp_path):
    (tmp_path / "HEARTBEAT.md").write_text("local tasks", encoding="utf-8")

    async def boom():
        raise RuntimeError("nope")

    svc = _service(tmp_path, extra_context=boom)
    content = await svc._build_content()
    assert "local tasks" in content


async def test_tick_runs_on_extra_context_only(tmp_path, monkeypatch):
    executed: dict = {}

    async def extra():
        return "[Mira Community] please review proposal p1"

    async def on_execute(tasks):
        executed["tasks"] = tasks
        return ""

    svc = _service(tmp_path, extra_context=extra, on_execute=on_execute)

    async def fake_decide(content):
        assert "p1" in content
        return "run", "review p1"

    monkeypatch.setattr(svc, "_decide", fake_decide)
    await svc._tick()
    assert executed["tasks"] == "review p1"


def _hooked_service(tmp_path, monkeypatch, *, verdict: bool):
    """A service whose gateway decides delivery itself (a chat dedicated to
    background work gets everything); the built-in evaluator must not run."""
    delivered: list[str] = []
    asked: list[tuple[str, str]] = []

    async def extra():
        return "[Mira Community] 1 actionable item"

    async def on_execute(tasks):
        return "I voted on p1."

    async def on_notify(response):
        delivered.append(response)

    async def should_notify(response, task_context):
        asked.append((response, task_context))
        return verdict

    svc = _service(
        tmp_path,
        extra_context=extra,
        on_execute=on_execute,
        on_notify=on_notify,
        should_notify=should_notify,
    )

    async def fake_decide(content):
        return "run", "vote on p1"

    monkeypatch.setattr(svc, "_decide", fake_decide)

    async def evaluator_must_not_run(**_kwargs):
        raise AssertionError("the built-in evaluator must not run when should_notify is set")

    monkeypatch.setattr("mira_engine.utils.evaluator.evaluate_response", evaluator_must_not_run)
    return svc, asked, delivered


async def test_should_notify_replaces_the_built_in_evaluator(tmp_path, monkeypatch):
    svc, asked, delivered = _hooked_service(tmp_path, monkeypatch, verdict=True)
    await svc._tick()
    assert asked == [("I voted on p1.", "vote on p1")]
    assert delivered == ["I voted on p1."]


async def test_should_notify_can_hold_the_result_back(tmp_path, monkeypatch):
    svc, asked, delivered = _hooked_service(tmp_path, monkeypatch, verdict=False)
    await svc._tick()
    assert len(asked) == 1
    assert delivered == []
