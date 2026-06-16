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
