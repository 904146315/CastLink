# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:/workspace/claw/投屏软件/tools/installer_app.py'],
    pathex=['D:/workspace/claw/投屏软件/tools'],
    binaries=[],
    datas=[('C:/Users/xiaos/AppData/Local/Temp/castlink_installer_build/payload.zip', 'payload.zip'), ('C:/Users/xiaos/AppData/Local/Temp/castlink_installer_build/installer_config.json', '.')],
    hiddenimports=[],
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
    name='CastLink-Setup',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
