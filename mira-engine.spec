# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, copy_metadata


a = Analysis(
    ['scripts/mira_engine_entry.py'],
    pathex=[],
    binaries=[],
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
