from medpilot.session.manager import Session


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


def test_get_history_collapses_consecutive_users() -> None:
    """Consecutive user messages (caused by error responses not being saved)
    must be merged so strict providers like Anthropic don't reject them."""
    session = Session(
        key="cli:test",
        messages=[
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "hello again"},
            {"role": "user", "content": "are you there?"},
            {"role": "assistant", "content": "yes"},
        ],
    )

    history = session.get_history(max_messages=10)
    assert history == [
        {"role": "user", "content": "hello\n\nhello again\n\nare you there?"},
        {"role": "assistant", "content": "yes"},
    ]


def test_get_history_strips_incomplete_tool_calls() -> None:
    """If an assistant has tool_calls but some results are missing (e.g. due
    to a window boundary or crash), strip the tool_calls to avoid API 400."""
    session = Session(
        key="cli:test",
        messages=[
            {"role": "user", "content": "run both checks"},
            {
                "role": "assistant",
                "content": "Running checks...",
                "tool_calls": [
                    {"id": "call_A", "type": "function", "function": {"name": "check_a", "arguments": "{}"}},
                    {"id": "call_B", "type": "function", "function": {"name": "check_b", "arguments": "{}"}},
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_A",
                "name": "check_a",
                "content": "ok",
            },
            # call_B result is missing
            {"role": "user", "content": "what happened?"},
            {"role": "assistant", "content": "looks like check_b failed"},
        ],
    )

    history = session.get_history(max_messages=10)
    # The incomplete assistant should lose its tool_calls, keeping only text.
    assert history[0] == {"role": "user", "content": "run both checks"}
    assert history[1] == {"role": "assistant", "content": "Running checks..."}
    # No tool result in output (orphaned results are dropped).
    assert history[2] == {"role": "user", "content": "what happened?"}
    assert history[3] == {"role": "assistant", "content": "looks like check_b failed"}


def test_get_history_strips_tool_calls_with_no_content() -> None:
    """If an incomplete tool-call assistant had no text content, it should be
    removed entirely rather than leaving an empty assistant message."""
    session = Session(
        key="cli:test",
        messages=[
            {"role": "user", "content": "do something"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_X", "type": "function", "function": {"name": "action", "arguments": "{}"}},
                ],
            },
            # tool result missing
            {"role": "assistant", "content": "sorry, an error occurred"},
        ],
    )

    history = session.get_history(max_messages=10)
    # The empty assistant (after stripping tool_calls) should be merged
    # with the next assistant via collapse.
    assert history == [
        {"role": "user", "content": "do something"},
        {"role": "assistant", "content": "sorry, an error occurred"},
    ]


def test_get_history_consecutive_assistants_merged() -> None:
    """Two consecutive assistant messages (e.g. from partial tool call
    stripping) should be merged."""
    session = Session(
        key="cli:test",
        messages=[
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "step one"},
            {"role": "assistant", "content": "step two"},
        ],
    )

    history = session.get_history(max_messages=10)
    assert history == [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "step one\n\nstep two"},
    ]
