from __future__ import annotations

import mira_engine.providers.github_copilot_provider as github_provider


def test_github_copilot_storage_prepares_oauth_state(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        github_provider,
        "ensure_oauth_state_dirs_for_runtime",
        lambda: calls.append("prepare"),
    )

    storage = github_provider._storage()

    assert storage is not None
    assert calls == ["prepare"]
