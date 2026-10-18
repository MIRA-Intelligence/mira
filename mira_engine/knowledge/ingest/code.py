"""Ingest source trees: extracted zip archives, local directories and git clones."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from ..models import Document
from .documents import extract_document, is_document

SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".mira", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea", ".vscode", ".next",
    "dist", "build", "target", "out", ".gradle", ".cache", "coverage", "site-packages",
})
SKIP_SUFFIXES = frozenset({
    ".lock", ".min.js", ".map", ".pyc", ".so", ".dll", ".dylib", ".exe", ".bin", ".o", ".a",
    ".class", ".jar", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".mp3", ".mp4",
    ".zip", ".gz", ".tar", ".tgz", ".7z", ".whl", ".pkl", ".pt", ".onnx", ".npy", ".parquet",
})
GIT_TIMEOUT_S = 300


def _skipped(rel: PurePosixPath) -> bool:
    name = rel.name.lower()
    return any(name.endswith(s) for s in SKIP_SUFFIXES) or any(p in SKIP_DIRS for p in rel.parts[:-1])


def _git_files(root: Path) -> list[str] | None:
    """Tracked plus untracked-but-not-ignored files, so .gitignore is honoured."""
    if not (root / ".git").exists() or shutil.which("git") is None:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True, timeout=60, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def list_tree(root: Path, max_files: int) -> list[str]:
    """Relative POSIX paths worth indexing under ``root``."""
    candidates = _git_files(root)
    if candidates is None:
        candidates = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            base = Path(dirpath).relative_to(root)
            candidates.extend((base / f).as_posix() for f in sorted(filenames))
    files: list[str] = []
    for rel in candidates:
        if _skipped(PurePosixPath(rel)):
            continue
        path = root / rel
        if path.is_symlink() or not path.is_file():
            continue
        files.append(rel)
        if len(files) >= max_files:
            break
    return files


def extract_tree(root: Path, *, max_files: int, max_file_bytes: int, prefix: str = "") -> list[tuple[Document, bool]]:
    """Documents for a source tree; the flag says whether to chunk it as code."""
    out: list[tuple[Document, bool]] = []
    for rel in list_tree(root, max_files):
        path = root / rel
        if path.stat().st_size > max_file_bytes and not is_document(path):
            continue
        display = f"{prefix}{rel}"
        try:
            doc = extract_document(path, display)
        except ValueError:
            continue  # e.g. a scanned PDF inside a repo; skip rather than fail the source
        if doc is not None and doc.text.strip():
            out.append((doc, not is_document(path)))
    return out


def extract_zip(archive: Path, dest: Path, *, max_files: int, max_total_bytes: int) -> Path:
    """Extract ``archive`` into ``dest`` refusing traversal, links and zip bombs."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    total = 0
    try:
        with zipfile.ZipFile(archive) as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            if len(members) > max_files * 2:
                raise ValueError(f"archive has too many files ({len(members)})")
            for member in members:
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    continue  # symlink entry
                rel = PurePosixPath(member.filename.replace("\\", "/"))
                if rel.is_absolute() or ".." in rel.parts:
                    raise ValueError(f"unsafe zip entry: {member.filename}")
                target = (root / rel).resolve()
                if not target.is_relative_to(root):
                    raise ValueError(f"unsafe zip entry: {member.filename}")
                total += member.file_size
                if total > max_total_bytes:
                    raise ValueError("archive is too large when extracted")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile as exc:
        raise ValueError("invalid zip archive") from exc
    # A zip of one folder: index that folder so paths do not all start with it.
    children = [c for c in root.iterdir() if c.name != "__MACOSX"]
    return children[0] if len(children) == 1 and children[0].is_dir() else root


def validate_git_url(url: str) -> None:
    from mira_engine.security.network import validate_url_target

    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("only https:// git URLs are supported")
    if parsed.username or parsed.password:
        raise ValueError("credentials in the git URL are not allowed")
    ok, reason = validate_url_target(url)
    if not ok:
        raise ValueError(reason)


async def git_clone(url: str, dest: Path, ref: str = "") -> str:
    """Shallow-clone ``url`` into ``dest`` (replacing it); returns the HEAD commit."""
    validate_git_url(url)
    if ref.startswith("-"):
        raise ValueError("invalid git ref")
    if shutil.which("git") is None:
        raise ValueError("git is not installed on the engine host")
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    args = ["git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "http.followRedirects=false",
            "clone", "--depth", "1", "--single-branch", "--no-tags"]
    if ref:
        args += ["--branch", ref]
    args += ["--", url, str(tmp)]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
    proc = await asyncio.create_subprocess_exec(
        *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError("git clone timed out") from None
    if proc.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        message = stderr.decode("utf-8", "replace").strip().splitlines()
        raise ValueError(f"git clone failed: {message[-1] if message else proc.returncode}")
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    head = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30,
    )
    return head.stdout.strip()
