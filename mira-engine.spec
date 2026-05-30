# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata


# Optional: a pre-fetched ``uv`` binary placed under ``bundled/`` (see
# scripts/fetch_uv.py). When present, PyInstaller embeds it next to the
# main executable so ``mira_engine.runtime.python_env.detect_uv`` can find
# it via ``sys._MEIPASS`` even on machines without ``uv`` on PATH.
_UV_BINARY_NAME = "uv.exe" if sys.platform == "win32" else "uv"
_BUNDLED_UV = Path(SPECPATH) / "bundled" / _UV_BINARY_NAME  # noqa: F821 - SPECPATH is injected by PyInstaller
_extra_binaries = [(str(_BUNDLED_UV), ".")] if _BUNDLED_UV.exists() else []


a = Analysis(
    ['scripts/mira_engine_entry.py'],
    pathex=[],
    binaries=_extra_binaries,
    datas=(
        collect_data_files('litellm')
        + collect_data_files(
            'mira_engine',
            includes=[
                'templates/**/*',
                'channels/ui_assets/**/*',
                'skills/**/*',
            ],
        )
        + copy_metadata('mira-engine')
    ),
    hiddenimports=['mira_engine.channels.ui', 'tiktoken_ext.openai_public'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='mira-engine',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
