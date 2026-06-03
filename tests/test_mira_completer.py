"""Tests for MiraCompleter — file-path autocomplete in the CLI."""

from pathlib import Path
from prompt_toolkit.document import Document

from mira_engine.cli.commands import MiraCompleter


def _build_workspace(tmp_path: Path, files: list[str]) -> Path:
    """Create a temp workspace with the given relative file paths."""
    for f in files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    return tmp_path


def _completions(ws: Path, text: str) -> list[str]:
    """Return completion texts for a given input string."""
    c = MiraCompleter(ws)
    doc = Document(text=text, cursor_position=len(text))
    return [comp.text for comp in c.get_completions(doc, None)]


# ---------------------------------------------------------------------------
# Basic behaviour
# ---------------------------------------------------------------------------


class TestBasicBehaviour:
    def test_no_at_returns_empty(self, tmp_path):
        _build_workspace(tmp_path, ["a.txt"])
        assert _completions(tmp_path, "hello") == []

    def test_at_only_returns_all_files(self, tmp_path):
        ws = _build_workspace(tmp_path, ["a.txt", "b.py", "c.md"])
        results = _completions(ws, "@")
        assert sorted(results) == ["a.txt", "b.py", "c.md"]

    def test_empty_workspace(self, tmp_path):
        assert _completions(tmp_path, "@") == []

    def test_nested_files(self, tmp_path):
        ws = _build_workspace(tmp_path, ["src/main.py", "src/utils/helper.py"])
        results = _completions(ws, "@")
        assert sorted(results) == ["src/main.py", "src/utils/helper.py"]

    def test_hidden_files_excluded(self, tmp_path):
        ws = _build_workspace(tmp_path, [".env", "config.json"])
        results = _completions(ws, "@")
        assert ".env" not in results
        assert "config.json" in results

    def test_hidden_dir_excluded(self, tmp_path):
        ws = _build_workspace(tmp_path, [".git/HEAD", "README.md"])
        results = _completions(ws, "@")
        assert ".git/HEAD" not in results
        assert "README.md" in results

    def test_max_50_completions(self, tmp_path):
        files = [f"file_{i:03d}.txt" for i in range(100)]
        _build_workspace(tmp_path, files)
        results = _completions(tmp_path, "@")
        assert len(results) == 50


# ---------------------------------------------------------------------------
# Fuzzy (substring) matching
# ---------------------------------------------------------------------------


class TestFuzzyMatching:
    def test_exact_prefix(self, tmp_path):
        ws = _build_workspace(tmp_path, ["commands.py", "common.py", "other.py"])
        results = _completions(ws, "@com")
        assert sorted(results) == ["commands.py", "common.py"]

    def test_mid_substring(self, tmp_path):
        ws = _build_workspace(tmp_path, ["commands.py", "random.py", "other.py"])
        results = _completions(ws, "@mand")
        assert results == ["commands.py"]

    def test_suffix_substring(self, tmp_path):
        ws = _build_workspace(tmp_path, ["commands.py", "prompts.py"])
        results = _completions(ws, "@pts")
        assert results == ["prompts.py"]

    def test_partial_no_match(self, tmp_path):
        ws = _build_workspace(tmp_path, ["abc.txt"])
        assert _completions(ws, "@xyz") == []

    def test_case_sensitive(self, tmp_path):
        ws = _build_workspace(tmp_path, ["Commands.py", "commands.py"])
        results = _completions(ws, "@Commands")
        assert results == ["Commands.py"]

    def test_substring_in_dir_name(self, tmp_path):
        ws = _build_workspace(tmp_path, ["mira_engine/cli/commands.py"])
        results = _completions(ws, "@cli/")
        assert results == ["mira_engine/cli/commands.py"]

    def test_substring_across_slash(self, tmp_path):
        ws = _build_workspace(tmp_path, ["mira_engine/cli/commands.py"])
        results = _completions(ws, "@cli/co")
        assert results == ["mira_engine/cli/commands.py"]


# ---------------------------------------------------------------------------
# Edge cases with @ position
# ---------------------------------------------------------------------------


class TestAtPosition:
    def test_at_at_start(self, tmp_path):
        ws = _build_workspace(tmp_path, ["foo.txt"])
        assert _completions(ws, "@foo") == ["foo.txt"]

    def test_at_after_text(self, tmp_path):
        ws = _build_workspace(tmp_path, ["foo.txt"])
        assert _completions(ws, "show @foo") == ["foo.txt"]

    def test_last_at_used(self, tmp_path):
        ws = _build_workspace(tmp_path, ["bar.txt"])
        # Two @ signs — the last one should be used
        assert _completions(ws, "hello@x@bar") == ["bar.txt"]

    def test_at_with_space_after(self, tmp_path):
        ws = _build_workspace(tmp_path, ["foo.txt"])
        # partial after stripping whitespace is "foo"
        assert _completions(ws, "@ foo") == ["foo.txt"]

    def test_only_at_no_partial(self, tmp_path):
        ws = _build_workspace(tmp_path, ["a.txt", "b.txt"])
        results = _completions(ws, "@")
        assert sorted(results) == ["a.txt", "b.txt"]


# ---------------------------------------------------------------------------
# Completion metadata (start_position)
# ---------------------------------------------------------------------------


class TestCompletionMetadata:
    def test_start_position_replaces_at_and_partial(self, tmp_path):
        ws = _build_workspace(tmp_path, ["commands.py"])
        c = MiraCompleter(ws)
        doc = Document(text="@com", cursor_position=4)
        comps = list(c.get_completions(doc, None))
        assert len(comps) == 1
        # start_position=-4 means "@com" (4 chars) is replaced with the path
        assert comps[0].start_position == -4
        assert comps[0].text == "commands.py"

    def test_start_position_with_at_only(self, tmp_path):
        ws = _build_workspace(tmp_path, ["x.txt"])
        c = MiraCompleter(ws)
        doc = Document(text="@", cursor_position=1)
        comps = list(c.get_completions(doc, None))
        assert len(comps) == 1
        # partial is "", len+1 = 1, replaces "@" only
        assert comps[0].start_position == -1

    def test_start_position_with_text_before_at(self, tmp_path):
        ws = _build_workspace(tmp_path, ["hello.py"])
        c = MiraCompleter(ws)
        doc = Document(text="run @hello", cursor_position=10)
        comps = list(c.get_completions(doc, None))
        assert len(comps) == 1
        # partial is "hello" (5 chars) + 1 for @ = -6
        assert comps[0].start_position == -6
