from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from mira_engine.utils import helpers
from mira_engine.utils.helpers import (
    detect_image_mime,
    ensure_dir,
    safe_filename,
    split_message,
    sync_workspace_templates,
    timestamp,
)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"\x89PNG\r\n\x1a\n" + b"x", "image/png"),
        (b"\xff\xd8\xff" + b"extra", "image/jpeg"),
        (b"GIF87a" + b"data", "image/gif"),
        (b"GIF89a" + b"data", "image/gif"),
        (
            b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"more",
            "image/webp",
        ),
    ],
)
def test_detect_image_mime_known_signatures(data: bytes, expected: str) -> None:
    assert detect_image_mime(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        b"not an image",
        b"",
        b"ab",
        b"x",
    ],
)
def test_detect_image_mime_unknown_or_too_short(data: bytes) -> None:
    assert detect_image_mime(data) is None


def test_ensure_dir_creates_nested_and_returns_path(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c"
    result = ensure_dir(target)
    assert result == target
    assert target.is_dir()


def test_ensure_dir_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir"
    assert ensure_dir(target) == ensure_dir(target)
    assert target.is_dir()


def test_timestamp_iso_and_today() -> None:
    s = timestamp()
    parsed = datetime.fromisoformat(s)
    assert parsed.date() == date.today()


def test_safe_filename_replaces_unsafe_chars() -> None:
    assert safe_filename('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"


def test_safe_filename_strips_whitespace() -> None:
    assert safe_filename("  hello  ") == "hello"


def test_safe_filename_normal_unchanged() -> None:
    assert safe_filename("report_final_v2") == "report_final_v2"


def test_split_message_empty() -> None:
    assert split_message("") == []


def test_split_message_short_single_chunk() -> None:
    assert split_message("hello", max_len=10) == ["hello"]


def test_split_message_prefers_newline_within_limit() -> None:
    tail = "y" * 800
    content = "first line\n" + tail
    chunks = split_message(content, max_len=500)
    assert chunks[0] == "first line"
    assert all(len(c) <= 500 for c in chunks)
    assert chunks[1].startswith("y")
    assert "".join(chunks) == "first line" + tail


def test_split_message_prefers_space_when_no_newline() -> None:
    content = ("alpha " * 200).strip()
    chunks = split_message(content, max_len=40)
    assert all(len(c) <= 40 for c in chunks)
    assert "".join("".join(chunks).split()) == "".join(content.split())


def test_split_message_hard_cut_when_no_breaks() -> None:
    content = "z" * 100
    chunks = split_message(content, max_len=30)
    assert chunks == ["z" * 30, "z" * 30, "z" * 30, "z" * 10]


def test_split_message_multiple_chunks() -> None:
    content = ("part\n" * 15) + ("x" * 50)
    chunks = split_message(content, max_len=25)
    assert len(chunks) >= 3
    assert all(len(c) <= 25 for c in chunks)
    assert content.count("part") == "".join(chunks).count("part")
    assert content.count("x") == "".join(chunks).count("x")


def test_sync_workspace_templates_fresh_creates_files(tmp_path: Path) -> None:
    added = sync_workspace_templates(tmp_path, silent=True)
    assert added
    assert (tmp_path / "skills").is_dir()
    assert (tmp_path / "memory" / "MEMORY.md").is_file()
    assert (tmp_path / "memory" / "HISTORY.md").is_file()
    assert (tmp_path / "memory" / "HISTORY.md").read_text(encoding="utf-8") == ""
    for name in helpers._RUNTIME_BOOTSTRAP:
        assert not (tmp_path / name).exists()


def test_sync_workspace_templates_skips_bootstrap_md(tmp_path: Path) -> None:
    sync_workspace_templates(tmp_path, silent=True)
    for name in helpers._RUNTIME_BOOTSTRAP:
        assert not (tmp_path / name).exists()


def test_sync_workspace_templates_skips_profile_agents_templates(tmp_path: Path) -> None:
    sync_workspace_templates(tmp_path, silent=True)
    assert not (tmp_path / "AGENTS_EG.md").exists()
    assert not (tmp_path / "AGENTS_RS.md").exists()


def test_sync_workspace_templates_does_not_overwrite_existing(tmp_path: Path) -> None:
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("user-owned\n", encoding="utf-8")
    sync_workspace_templates(tmp_path, silent=True)
    assert hb.read_text(encoding="utf-8") == "user-owned\n"


def test_sync_workspace_templates_creates_skills_dir(tmp_path: Path) -> None:
    sync_workspace_templates(tmp_path, silent=True)
    skills = tmp_path / "skills"
    assert skills.is_dir()


@patch("rich.console.Console")
def test_sync_workspace_templates_silent_suppresses_output(
    mock_console: object,
    tmp_path: Path,
) -> None:
    sync_workspace_templates(tmp_path, silent=True)
    mock_console.assert_not_called()


@patch("rich.console.Console")
def test_sync_workspace_templates_not_silent_uses_console(
    mock_console_cls: object,
    tmp_path: Path,
) -> None:
    sync_workspace_templates(tmp_path, silent=False)
    mock_console_cls.assert_called()
    instance = mock_console_cls.return_value
    assert instance.print.called
