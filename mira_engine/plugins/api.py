"""Plugin API versioning (design §3.6).

The plugin contract is versioned independently of the engine but follows the
same major.minor rule: a minor bump only adds, a major bump may break. A
plugin declares the range it supports (``plugin_api``) and is not loaded when
the kernel's version is outside it.
"""

from __future__ import annotations

import re

PLUGIN_API_VERSION = "1.0"

_CLAUSE = re.compile(r"^\s*(>=|<=|==|>|<)?\s*(\d+)(?:\.(\d+))?\s*$")


def _parse(v: str) -> tuple[int, int]:
    major, _, minor = v.partition(".")
    return int(major), int(minor or 0)


def satisfies(spec: str, version: str = PLUGIN_API_VERSION) -> bool:
    """Whether ``version`` matches a spec like ``">=1.0,<2.0"`` (``,`` = and).
    A malformed spec never matches, so a typo fails closed."""
    current = _parse(version)
    clauses = [c for c in spec.split(",") if c.strip()]
    if not clauses:
        return False
    for clause in clauses:
        m = _CLAUSE.match(clause)
        if not m:
            return False
        op, major, minor = m.group(1) or "==", int(m.group(2)), int(m.group(3) or 0)
        target = (major, minor)
        ok = {
            ">=": current >= target,
            ">": current > target,
            "<=": current <= target,
            "<": current < target,
            "==": current == target,
        }[op]
        if not ok:
            return False
    return True
