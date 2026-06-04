"""Tests for interactive CLI Ctrl+C handling."""

from __future__ import annotations

import asyncio

import pytest

from mira_engine.cli.commands import (
    CLI_CTRL_C_EXIT_HINT,
    PROMPT_CTRL_C_EXIT,
    PROMPT_CTRL_C_IGNORE,
    PROMPT_CTRL_C_SHOW_HINT,
    interrupt_cli_agent_turn,
    resolve_prompt_ctrl_c_action,
    should_cancel_turn_on_sigint,
)


def test_should_cancel_turn_on_sigint_when_agent_busy():
    assert should_cancel_turn_on_sigint(turn_done_set=False) is True
    assert should_cancel_turn_on_sigint(turn_done_set=True) is False


@pytest.mark.parametrize(
    ("turn_done_set", "exit_armed_until", "now", "expected"),
    [
        (False, 0.0, 10.0, PROMPT_CTRL_C_IGNORE),
        (True, 0.0, 10.0, PROMPT_CTRL_C_SHOW_HINT),
        (True, 12.0, 11.0, PROMPT_CTRL_C_EXIT),
        (True, 12.0, 13.0, PROMPT_CTRL_C_SHOW_HINT),
    ],
)
def test_resolve_prompt_ctrl_c_action(turn_done_set, exit_armed_until, now, expected):
    assert (
        resolve_prompt_ctrl_c_action(
            turn_done_set=turn_done_set,
            exit_armed_until=exit_armed_until,
            now=now,
        )
        == expected
    )


def test_recent_turn_sigint_does_not_arm_prompt_exit():
    """After interrupting a turn, the first Ctrl+C at the prompt must show a hint."""
    now = 100.0
    # Simulates old bug: turn interrupt stamped monotonic time recently.
    assert (
        resolve_prompt_ctrl_c_action(
            turn_done_set=True,
            exit_armed_until=0.0,
            now=now,
        )
        == PROMPT_CTRL_C_SHOW_HINT
    )


def test_exit_hint_constant_matches_user_facing_copy():
    assert CLI_CTRL_C_EXIT_HINT == "Press Ctrl+C again to quit"


@pytest.mark.asyncio
async def test_interrupt_cli_agent_turn_unblocks_and_cancels_tasks():
    turn_done = asyncio.Event()
    turn_response: list[str] = ["partial"]
    turn_skills: set[str] = {"demo"}
    interrupted: list[str] = []

    async def slow_work():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise

    dispatch = asyncio.create_task(slow_work())
    await asyncio.sleep(0)

    await interrupt_cli_agent_turn(
        turn_done=turn_done,
        turn_response=turn_response,
        turn_skills=turn_skills,
        dispatch_tasks=[dispatch],
        on_interrupted=lambda: interrupted.append("ok"),
        cancel_timeout=0.5,
    )

    assert turn_done.is_set()
    assert turn_response == []
    assert turn_skills == set()
    assert interrupted == ["ok"]
    assert dispatch.cancelled() or dispatch.done()


@pytest.mark.asyncio
async def test_interrupt_cli_agent_turn_is_idempotent_when_already_done():
    turn_done = asyncio.Event()
    turn_done.set()
    turn_response = ["keep"]
    calls: list[str] = []

    await interrupt_cli_agent_turn(
        turn_done=turn_done,
        turn_response=turn_response,
        turn_skills=set(),
        dispatch_tasks=[],
        on_interrupted=lambda: calls.append("nope"),
    )

    assert turn_response == ["keep"]
    assert calls == []
