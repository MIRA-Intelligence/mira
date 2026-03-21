from radiologybot.session.manager import Session


def test_get_history_returns_empty_when_window_has_no_user() -> None:
    session = Session(
        key="cli:test",
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "toolu_123",
                        "type": "function",
                        "function": {"name": "status_check", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_123",
                "name": "status_check",
                "content": "ok",
            },
            {"role": "assistant", "content": "done"},
        ],
    )

    assert session.get_history(max_messages=3) == []


def test_get_history_drops_orphan_tool_results() -> None:
    session = Session(
        key="cli:test",
        messages=[
            {"role": "user", "content": "确认实验进度"},
            {
                "role": "tool",
                "tool_call_id": "toolu_orphan",
                "name": "status_check",
                "content": "ok",
            },
            {"role": "assistant", "content": "我继续看"},
        ],
    )

    assert session.get_history(max_messages=10) == [
        {"role": "user", "content": "确认实验进度"},
        {"role": "assistant", "content": "我继续看"},
    ]


def test_get_history_keeps_valid_tool_roundtrip() -> None:
    session = Session(
        key="cli:test",
        messages=[
            {"role": "user", "content": "确认实验进度"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "toolu_123",
                        "type": "function",
                        "function": {"name": "status_check", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_123",
                "name": "status_check",
                "content": "ok",
            },
            {"role": "assistant", "content": "实验已完成"},
        ],
    )

    assert session.get_history(max_messages=10) == [
        {"role": "user", "content": "确认实验进度"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "toolu_123",
                    "type": "function",
                    "function": {"name": "status_check", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "content": "ok",
            "tool_call_id": "toolu_123",
            "name": "status_check",
        },
        {"role": "assistant", "content": "实验已完成"},
    ]
