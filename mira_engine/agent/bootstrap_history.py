"""Digests of every bootstrap template MIRA has shipped.

`mira onboard` copies AGENTS*.md, SOUL.md, USER.md and TOOLS.md into the
workspace once and never updates them, so template improvements would not
reach existing users. A workspace copy whose digest is listed here was never
edited (by the user or by Dream), and the prompt uses the current template
instead.

When a template changes, add its new digest here; tests/test_bootstrap_history.py
fails until you do.
"""

import hashlib


def template_digest(text: str) -> str:
    """Digest that ignores line endings and trailing whitespace."""
    lines = text.replace("\r\n", "\n").strip().split("\n")
    return hashlib.sha256("\n".join(line.rstrip() for line in lines).encode("utf-8")).hexdigest()[:16]


SHIPPED_TEMPLATE_DIGESTS: dict[str, frozenset[str]] = {
    "AGENTS.md": frozenset({
        "032642da64a81024",
        "0d1c0eb5ca0274a5",
        "15d53dd70ccb6826",
        "2e2a0bd07fdc8ffd",
        "58b240af96bd2f4a",
        "5cd0be90f2bd3320",
        "6b803b5f5aef0e07",
        "7a44f3d19cff6b96",
        "ada6517c073231e3",
        "d96259c220bdc04f",
        "dedba22c59972797",
        "fb7d87813c65f6aa",
        "ff5a24b763a4a9c6",
    }),
    "AGENTS_EG.md": frozenset({
        "049981dda3f49d9c",
        "0fcc1d0a62a282ff",
        "1fdf27cad43e6149",
        "4af3052956b53b77",
        "86305b7307fd1e31",
    }),
    "AGENTS_RS.md": frozenset({
        "21060c8b4bfe18c8",
        "476e0a6eca8a3c44",
        "5022805b9d727654",
        "8c4c09d29b11191d",
        "e5d735e803854405",
        "fcb0ea310d74a5d5",
    }),
    "AGENTS_TM.md": frozenset({
        "3cc5b72b1fa89a7e",
        "a4c1d12a4d66f585",
    }),
    "SOUL.md": frozenset({
        "0a52e844770a7d66",
        "22ae4d9a247d2518",
        "2c237d0aac86c8f4",
        "4c94411ddfd96548",
        "53b3f3e3ba04a401",
        "62b4bde20730637f",
        "b6edd82e8f6a230c",
        "d0142379ab88fa20",
        "d652ea83a0343343",
    }),
    "TOOLS.md": frozenset({
        "36cf2427d4bd9a43",
        "3cb8ae8ee1126b79",
        "4344184ebfdef949",
        "785f87eb88ca24df",
    }),
    "USER.md": frozenset({
        "3100bfa6e6cc8c6f",
        "6ebe6cf326cf7cca",
        "99b2312ff6e22d1c",
        "bc7af6370834899a",
        "cb5c91b6e4dcf274",
        "de0d841b3d92e28f",
    }),
}


def is_shipped_template(name: str, text: str) -> bool:
    """True when ``text`` is an unedited copy of some shipped version of template ``name``."""
    return template_digest(text) in SHIPPED_TEMPLATE_DIGESTS.get(name, frozenset())
