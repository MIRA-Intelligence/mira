"""User-selected, Run-scoped filesystem inputs (not general shell authority)."""

import os
from pathlib import Path
from typing import Any


def resolve_input_path(raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ValueError("input path must be a non-empty filesystem path")
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        raise ValueError("input path must be absolute on the gateway machine")
    path = path.resolve(strict=True)
    if path.is_file():
        with path.open("rb"):
            pass
    elif path.is_dir():
        with os.scandir(path) as entries:
            next(entries, None)
    else:
        raise ValueError("input path must be a regular file or directory")
    return path


def normalize_input_paths(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 32 or any(not isinstance(p, str) for p in value):
        raise ValueError("external_read_paths must be a list of at most 32 filesystem paths")
    return list(dict.fromkeys(str(resolve_input_path(raw)) for raw in value))


def run_read_paths(run: Any) -> tuple[Path, ...]:
    """Never inherit grants from parent artifacts or other module types."""
    if run.module_type != "experiment":
        return ()
    paths = run.module_data.get("external_read_paths", [])
    if not isinstance(paths, list):
        return ()
    return tuple(Path(p) for p in paths if isinstance(p, str) and Path(p).is_absolute())
