# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


# Optional: a pre-fetched ``uv`` binary placed under ``bundled/`` (see
# scripts/fetch_uv.py). When present, PyInstaller embeds it next to the
# main executable so ``mira_engine.runtime.python_env.detect_uv`` can find
# it via ``sys._MEIPASS`` even on machines without ``uv`` on PATH.
_UV_BINARY_NAME = "uv.exe" if sys.platform == "win32" else "uv"
_BUNDLED_UV = Path(SPECPATH) / "bundled" / _UV_BINARY_NAME  # noqa: F821 - SPECPATH is injected by PyInstaller
_extra_binaries = [(str(_BUNDLED_UV), ".")] if _BUNDLED_UV.exists() else []


def _bundled_plugin_datas():
    """First-party plugins (repo ``plugins/``) ship as source next to the
    package, where ``mira_engine.plugins.paths.bundled_plugins_dir`` finds them.
    Their third-party imports are invisible to the analysis, so anything a
    plugin needs on the desktop must also be in ``hiddenimports`` below; a
    plugin whose ``requires`` are missing shows up as unavailable."""
    root = Path(SPECPATH) / "plugins"  # noqa: F821 - SPECPATH is injected by PyInstaller
    out = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or "__pycache__" in rel.parts or "tests" in rel.parts:
            continue
        out.append((str(path), str(Path("mira_engine/_bundled_plugins") / rel.parent)))
    return out


a = Analysis(
    ['scripts/mira_engine_entry.py'],
    pathex=[],
    binaries=_extra_binaries,
    datas=(
        collect_data_files('litellm')
        + collect_data_files('jieba')
        + collect_data_files(
            'mira_engine',
            includes=[
                'templates/**/*',
                'channels/ui_assets/**/*',
                # The skills submodule's skills/ is the MIRA Hub catalogue,
                # installed on demand; only builtin_skills/ ships.
                'skills/builtin_skills/**/*',
            ],
        )
        + copy_metadata('mira-engine')
        + _bundled_plugin_datas()
    ),
    hiddenimports=[
        # Built-in channels are found at run time by scanning mira_engine/channels
        # (channels/registry.py, pkgutil), which a frozen bundle can only do for
        # modules that were collected. Listing just `ui` here shipped v0.6.0rc3
        # without channels.community: the desktop app never opened its websocket
        # to the Mira Community, so no notifications reached the inbox.
        *collect_submodules('mira_engine.channels'),
        'tiktoken_ext.openai_public',
        # The CA bundle the executable falls back to when OpenSSL's compiled-in
        # path belongs to the build machine (scripts/mira_engine_entry.py →
        # mira_engine.utils.tls). Collected via PyInstaller's certifi hook.
        'certifi',
        # Out-of-process plugin host (`mira-engine --mira-plugin-host`).
        'mira_engine.plugins.host',
        # Engine modules only the bundled plugins (plugins/web, plugins/mira-hub) import.
        'mira_engine.agent.tools.web',
        'mira_engine.agent.tools.hub',
        # plugins/openai-codex and plugins/github-copilot (OAuth providers).
        'mira_engine.providers.oauth_state',
        'mira_engine.providers.openai_compat_provider',
        'mira_engine.providers.usage',
        *collect_submodules('oauth_cli_kit'),
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Mira Server runs from a pip install on Linux hosts, never from the desktop bundle.
    excludes=['mira_server', 'pam'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

_exe_kwargs = {
    "name": "mira-engine",
    "debug": False,
    "bootloader_ignore_signals": False,
    "strip": False,
    "upx": True,
    "upx_exclude": [],
    "runtime_tmpdir": None,
    "console": True,
    "disable_windowed_traceback": False,
    "argv_emulation": False,
    "target_arch": None,
    "codesign_identity": None,
    "entitlements_file": None,
}

if sys.platform == "win32":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        **_exe_kwargs,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="mira-engine",
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        **_exe_kwargs,
    )
