"""Git-backed version control for memory files, using dulwich."""

from __future__ import annotations

import io
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger


@dataclass
class CommitInfo:
    sha: str  # Short SHA (8 chars)
    message: str
    timestamp: str  # Formatted datetime

    def format(self, diff: str = "") -> str:
        """Format this commit for display, optionally with a diff."""
        header = f"## {self.message.splitlines()[0]}\n`{self.sha}` — {self.timestamp}\n"
        if diff:
            return f"{header}\n```diff\n{diff}\n```"
        return f"{header}\n(no file changes)"


class GitStore:
    """Git-backed version control for memory files."""

    def __init__(self, workspace: Path, tracked_files: list[str]):
        self._workspace = workspace
        self._tracked_files = tracked_files

    def is_initialized(self) -> bool:
        """Check if the git repo has been initialized."""
        return (self._workspace / ".git").is_dir()

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self._workspace,
            check=check,
            text=True,
            capture_output=True,
        )

    # -- init ------------------------------------------------------------------

    def init(self) -> bool:
        """Initialize a git repo if not already initialized.

        Creates .gitignore and makes an initial commit.
        Returns True if a new repo was created, False if already exists.
        """
        if self.is_initialized():
            return False

        try:
            self._workspace.mkdir(parents=True, exist_ok=True)
            self._git("init", "-q")

            # Write .gitignore
            gitignore = self._workspace / ".gitignore"
            gitignore.write_text(self._build_gitignore(), encoding="utf-8")

            # Ensure tracked files exist (touch them if missing) so the initial
            # commit has something to track.
            for rel in self._tracked_files:
                p = self._workspace / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                if not p.exists():
                    p.write_text("", encoding="utf-8")

            # Initial commit
            self._git("add", ".gitignore", *self._tracked_files)
            self._git(
                "-c",
                "user.name=mira",
                "-c",
                "user.email=mira@dream",
                "commit",
                "-q",
                "-m",
                "init: mira memory store",
            )
            logger.info("Git store initialized at {}", self._workspace)
            return True
        except Exception:
            logger.warning("Git store init failed for {}", self._workspace)
            return False

    # -- daily operations ------------------------------------------------------

    def auto_commit(self, message: str) -> str | None:
        """Stage tracked memory files and commit if there are changes.

        Returns the short commit SHA, or None if nothing to commit.
        """
        if not self.is_initialized():
            return None

        try:
            status = self._git("status", "--porcelain", check=False)
            if not status.stdout.strip():
                return None

            self._git("add", *self._tracked_files)
            self._git(
                "-c",
                "user.name=mira",
                "-c",
                "user.email=mira@dream",
                "commit",
                "-q",
                "-m",
                message,
            )
            sha = self._git("rev-parse", "--short=8", "HEAD").stdout.strip()
            if not sha:
                return None
            logger.debug("Git auto-commit: {} ({})", sha, message)
            return sha
        except Exception:
            logger.warning("Git auto-commit failed: {}", message)
            return None

    # -- internal helpers ------------------------------------------------------

    def _resolve_sha(self, short_sha: str) -> bytes | None:
        """Resolve a short SHA prefix to the full SHA bytes."""
        try:
            full = self._git("rev-parse", "--verify", f"{short_sha}^{{commit}}", check=False).stdout.strip()
            if not full:
                return None
            return bytes.fromhex(full)
        except Exception:
            return None

    def _build_gitignore(self) -> str:
        """Generate .gitignore content from tracked files."""
        dirs: set[str] = set()
        for f in self._tracked_files:
            parent = str(Path(f).parent)
            if parent != ".":
                dirs.add(parent)
        lines = ["/*"]
        for d in sorted(dirs):
            lines.append(f"!{d}/")
        for f in self._tracked_files:
            lines.append(f"!{f}")
        lines.append("!.gitignore")
        return "\n".join(lines) + "\n"

    # -- query -----------------------------------------------------------------

    def log(self, max_entries: int = 20) -> list[CommitInfo]:
        """Return simplified commit log."""
        if not self.is_initialized():
            return []

        try:
            out = self._git(
                "log",
                f"-n{max_entries}",
                "--format=%H%x1f%s%x1f%ct",
                check=False,
            ).stdout
            entries: list[CommitInfo] = []
            for line in out.splitlines():
                parts = line.split("\x1f")
                if len(parts) != 3:
                    continue
                sha, msg, ts = parts
                entries.append(
                    CommitInfo(
                        sha=sha[:8],
                        message=msg,
                        timestamp=time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts))),
                    )
                )
            return entries
        except Exception:
            logger.warning("Git log failed")
            return []

    def diff_commits(self, sha1: str, sha2: str) -> str:
        """Show diff between two commits."""
        if not self.is_initialized():
            return ""

        try:
            full1 = self._git("rev-parse", "--verify", f"{sha1}^{{commit}}", check=False).stdout.strip()
            full2 = self._git("rev-parse", "--verify", f"{sha2}^{{commit}}", check=False).stdout.strip()
            if not full1 or not full2:
                return ""
            return self._git("diff", full1, full2, "--", *self._tracked_files, check=False).stdout
        except Exception:
            logger.warning("Git diff_commits failed")
            return ""

    def find_commit(self, short_sha: str, max_entries: int = 20) -> CommitInfo | None:
        """Find a commit by short SHA prefix match."""
        for c in self.log(max_entries=max_entries):
            if c.sha.startswith(short_sha):
                return c
        return None

    def show_commit_diff(self, short_sha: str, max_entries: int = 20) -> tuple[CommitInfo, str] | None:
        """Find a commit and return it with its diff vs the parent."""
        commits = self.log(max_entries=max_entries)
        for i, c in enumerate(commits):
            if c.sha.startswith(short_sha):
                if i + 1 < len(commits):
                    diff = self.diff_commits(commits[i + 1].sha, c.sha)
                else:
                    diff = ""
                return c, diff
        return None

    # -- restore ---------------------------------------------------------------

    def revert(self, commit: str) -> str | None:
        """Revert (undo) the changes introduced by the given commit.

        Restores all tracked memory files to the state at the commit's parent,
        then creates a new commit recording the revert.

        Returns the new commit SHA, or None on failure.
        """
        if not self.is_initialized():
            return None

        try:
            full_sha = self._git("rev-parse", "--verify", f"{commit}^{{commit}}", check=False).stdout.strip()
            if not full_sha:
                logger.warning("Git revert: SHA not found: {}", commit)
                return None

            parent = self._git("rev-parse", "--verify", f"{full_sha}^", check=False).stdout.strip()
            if not parent:
                logger.warning("Git revert: cannot revert root commit {}", commit)
                return None

            restored: list[str] = []
            for filepath in self._tracked_files:
                target = self._workspace / filepath
                target.parent.mkdir(parents=True, exist_ok=True)
                show = self._git("show", f"{parent}:{filepath}", check=False)
                if show.returncode == 0:
                    target.write_text(show.stdout, encoding="utf-8")
                    restored.append(filepath)
                elif target.exists():
                    target.write_text("", encoding="utf-8")
                    restored.append(filepath)
            if not restored:
                return None

            # Commit the restored state
            msg = f"revert: undo {commit}"
            return self.auto_commit(msg)
        except Exception:
            logger.warning("Git revert failed for {}", commit)
            return None
