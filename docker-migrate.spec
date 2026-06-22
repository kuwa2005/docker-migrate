# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Windows (and cross-platform) one-file executable.
# Build: pyinstaller docker-migrate.spec
# Or use build-windows.ps1 on Windows.

block_cipher = None

a = Analysis(
    ["docker_migrate/__main__.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=["docker_migrate", "docker_migrate.export", "docker_migrate.import_bundle", "docker_migrate.gui", "docker_migrate.conflicts", "docker_migrate.utils"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="docker-migrate",
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
