#!/usr/bin/env python3
"""Download a pinned ``uv`` binary into ``bundled/`` for PyInstaller packaging.

This script is invoked at build time before ``pyinstaller mira-engine.spec``.
It fetches an ``uv`` executable matching the *target* host OS/arch from the
public ``astral-sh/uv`` GitHub release, verifies its SHA-256, and writes it
to ``<repo>/bundled/uv`` (POSIX) or ``<repo>/bundled/uv.exe`` (Windows).

The bundled binary is a runtime fallback for ``mira_engine.runtime.python_env.detect_uv``: when the engine runs as a one-file PyInstaller executable on a
machine that has no ``uv`` on PATH, the engine still has a usable ``uv`` and
can therefore bootstrap a per-project venv on first launch.

Usage::

    python scripts/fetch_uv.py                       # auto-detect host
    python scripts/fetch_uv.py --target macos-arm64  # explicit
    python scripts/fetch_uv.py --version 0.5.4       # pin a release

Exits non-zero on any failure (download, checksum mismatch, unsupported
target). Designed to be safe to re-run; the existing binary is replaced
atomically.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable

REPO = "astral-sh/uv"

# Minimum uv version we are willing to bundle. Mirrors
# ``mira_engine.runtime.python_env.MIN_UV_VERSION``.
MIN_VERSION: tuple[int, int, int] = (0, 5, 0)

# Mapping from friendly target names to uv's release asset triples.
# The order of fallback candidates matters for ``--target host`` resolution.
TARGETS: dict[str, dict[str, str]] = {
    "macos-arm64": {
        "asset": "uv-aarch64-apple-darwin.tar.gz",
        "binary": "uv",
    },
    "macos-x86_64": {
        "asset": "uv-x86_64-apple-darwin.tar.gz",
        "binary": "uv",
    },
    "linux-x86_64": {
        "asset": "uv-x86_64-unknown-linux-gnu.tar.gz",
        "binary": "uv",
    },
    "linux-aarch64": {
        "asset": "uv-aarch64-unknown-linux-gnu.tar.gz",
        "binary": "uv",
    },
    "windows-x86_64": {
        "asset": "uv-x86_64-pc-windows-msvc.zip",
        "binary": "uv.exe",
    },
    "windows-arm64": {
        "asset": "uv-aarch64-pc-windows-msvc.zip",
        "binary": "uv.exe",
    },
}


def detect_host_target() -> str:
    """Resolve the target name for the current build host."""
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return "macos-arm64" if machine in {"arm64", "aarch64"} else "macos-x86_64"
    if sys.platform == "win32":
        return "windows-arm64" if machine in {"arm64", "aarch64"} else "windows-x86_64"
    if sys.platform.startswith("linux"):
        return "linux-aarch64" if machine in {"arm64", "aarch64"} else "linux-x86_64"
    raise SystemExit(f"Unsupported host platform: {sys.platform}/{machine}")


def _github_api_request(url: str) -> urllib.request.Request:
    """Build a GitHub API request, adding auth when a token is available.

    GitHub's unauthenticated rate limit (60 req/h per IP) is shared across
    all GitHub Actions runners on the same IP, so the bare ``urlopen`` call
    intermittently fails the macOS / Windows ``Fetch bundled uv binary``
    step with HTTP 403. When ``GITHUB_TOKEN`` (or ``GH_TOKEN``) is exposed
    to the script, attaching it lifts the quota to 5,000 req/h per repo
    and stops the flake.
    """
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return req


def resolve_release_tag(version: str | None) -> str:
    """Return the GitHub release tag (e.g. ``0.5.4``)."""
    if version:
        normalized = version.lstrip("v")
        return normalized
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    with urllib.request.urlopen(_github_api_request(url), timeout=30) as resp:
        payload = json.load(resp)
    tag = (payload.get("tag_name") or "").lstrip("v")
    if not tag:
        raise SystemExit(f"Could not resolve latest release tag from {url}")
    return tag


def parse_version(tag: str) -> tuple[int, int, int]:
    parts = tag.split(".")
    if len(parts) < 3:
        raise SystemExit(f"Unexpected uv version: {tag!r}")
    return tuple(int(part) for part in parts[:3])  # type: ignore[return-value]


def fetch_bytes(url: str) -> bytes:
    print(f"  downloading {url}", flush=True)
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read()


def verify_sha256(blob: bytes, expected: str, asset_name: str) -> None:
    digest = hashlib.sha256(blob).hexdigest()
    if digest != expected:
        raise SystemExit(
            f"SHA-256 mismatch for {asset_name}:\n"
            f"  expected: {expected}\n"
            f"  actual:   {digest}"
        )
    print(f"  sha256 ok ({digest[:12]}…)", flush=True)


def parse_sha256_file(content: bytes, asset_name: str) -> str:
    """Return the digest matching ``asset_name`` from a ``*.sha256`` payload.

    Releases ship per-asset ``<asset>.sha256`` files containing a single
    line ``<hash>  <asset>``; we tolerate either form.
    """
    text = content.decode("utf-8", errors="replace").strip()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 1:
            return parts[0]
        if len(parts) >= 2 and parts[1].lstrip("*") == asset_name:
            return parts[0]
    raise SystemExit(
        f"Could not locate sha256 for {asset_name} in checksum file:\n{text}"
    )


def extract_binary(blob: bytes, asset_name: str, binary_name: str) -> bytes:
    """Pull the single ``uv``/``uv.exe`` file out of the downloaded archive."""
    if asset_name.endswith(".tar.gz"):
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            for member in tar.getmembers():
                if Path(member.name).name == binary_name and member.isfile():
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        break
                    return extracted.read()
        raise SystemExit(f"{binary_name} not found inside {asset_name}")
    if asset_name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for name in zf.namelist():
                if Path(name).name == binary_name:
                    return zf.read(name)
        raise SystemExit(f"{binary_name} not found inside {asset_name}")
    raise SystemExit(f"Unsupported archive format: {asset_name}")


def write_binary_atomic(binary_bytes: bytes, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="uv-", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(binary_bytes)
        if sys.platform != "win32":
            current = os.stat(tmp_path).st_mode
            os.chmod(tmp_path, current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        shutil.move(tmp_path, dest)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def download_target(target: str, tag: str, dest_dir: Path) -> Path:
    spec = TARGETS[target]
    asset_name = spec["asset"]
    binary_name = spec["binary"]
    base = f"https://github.com/{REPO}/releases/download/{tag}"

    archive = fetch_bytes(f"{base}/{asset_name}")
    checksum = fetch_bytes(f"{base}/{asset_name}.sha256")
    expected = parse_sha256_file(checksum, asset_name)
    verify_sha256(archive, expected, asset_name)
    binary_blob = extract_binary(archive, asset_name, binary_name)

    dest = dest_dir / binary_name
    write_binary_atomic(binary_blob, dest)
    return dest


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        default=None,
        choices=[*TARGETS.keys(), "host"],
        help="Target triple (defaults to the build host).",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Pin a uv release (e.g. 0.5.4); defaults to the latest published.",
    )
    parser.add_argument(
        "--dest",
        default="bundled",
        help="Output directory (relative to repo root or absolute).",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    target = detect_host_target() if args.target in (None, "host") else args.target
    tag = resolve_release_tag(args.version)
    version = parse_version(tag)
    if version < MIN_VERSION:
        raise SystemExit(
            f"Refusing to bundle uv {tag}; require >= "
            f"{'.'.join(map(str, MIN_VERSION))}."
        )
    print(f"fetching uv {tag} for {target} -> {args.dest}/", flush=True)

    dest_dir = Path(args.dest)
    if not dest_dir.is_absolute():
        dest_dir = (Path.cwd() / dest_dir).resolve()

    binary_path = download_target(target, tag, dest_dir)
    print(f"  wrote {binary_path}")
    print(f"  ({binary_path.stat().st_size / 1_000_000:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
