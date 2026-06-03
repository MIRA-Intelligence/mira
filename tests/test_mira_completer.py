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


def _completions_with_meta(ws: Path, text: str) -> list[tuple[str, int]]:
    """Return (text, start_position) pairs for a given input string."""
    c = MiraCompleter(ws)
    doc = Document(text=text, cursor_position=len(text))
    return [(comp.text, comp.start_position) for comp in c.get_completions(doc, None)]


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

    def test_pycache_excluded(self, tmp_path):
        ws = _build_workspace(tmp_path, ["__pycache__/mod.cpython-311.pyc", "mod.py"])
        results = _completions(ws, "@")
        assert "mod.py" in results
        assert all("__pycache__" not in r for r in results)

    def test_node_modules_excluded(self, tmp_path):
        ws = _build_workspace(tmp_path, ["node_modules/pkg/index.js", "app.js"])
        results = _completions(ws, "@")
        assert "app.js" in results
        assert all("node_modules" not in r for r in results)

    def test_venv_excluded(self, tmp_path):
        ws = _build_workspace(tmp_path, [".venv/lib/foo.so", "script.py"])
        results = _completions(ws, "@")
        assert "script.py" in results
        assert all(".venv" not in r for r in results)

    def test_results_sorted(self, tmp_path):
        ws = _build_workspace(tmp_path, ["zebra.py", "alpha.py", "middle.py"])
        results = _completions(ws, "@")
        assert results == ["alpha.py", "middle.py", "zebra.py"]

    def test_cache_returns_same_result(self, tmp_path):
        """Scanning twice should return identical results (cached)."""
        ws = _build_workspace(tmp_path, ["a.txt", "b.txt"])
        c = MiraCompleter(ws)
        first = c._scan_files()
        second = c._scan_files()
        assert first == second


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
        results = _completions_with_meta(ws, "@com")
        assert len(results) == 1
        # "@com" is 4 chars, so start_position=-4 replaces "@com" with path
        assert results[0] == ("commands.py", -4)

    def test_start_position_with_at_only(self, tmp_path):
        ws = _build_workspace(tmp_path, ["x.txt"])
        results = _completions_with_meta(ws, "@")
        assert len(results) == 1
        # raw_partial is "" (0 chars) + 1 for "@" = -1
        assert results[0] == ("x.txt", -1)

    def test_start_position_with_text_before_at(self, tmp_path):
        ws = _build_workspace(tmp_path, ["hello.py"])
        results = _completions_with_meta(ws, "run @hello")
        assert len(results) == 1
        # raw_partial "hello" (5 chars) + 1 for "@" = -6
        assert results[0] == ("hello.py", -6)

    def test_start_position_with_whitespace_after_at(self, tmp_path):
        """Whitespace after @ should be included in the replaced span."""
        ws = _build_workspace(tmp_path, ["foo.txt"])
        results = _completions_with_meta(ws, "@ foo")
        assert len(results) == 1
        # raw_partial is " foo" (4 chars) + 1 for "@" = -5
        assert results[0] == ("foo.txt", -5)

    def test_return_type_is_iterable(self, tmp_path):
        """get_completions returns an iterable of Completion objects."""
        ws = _build_workspace(tmp_path, ["a.txt"])
        c = MiraCompleter(ws)
        doc = Document(text="@", cursor_position=1)
        result = list(c.get_completions(doc, None))
        assert len(result) == 1
        from prompt_toolkit.completion import Completion
        assert isinstance(result[0], Completion)
