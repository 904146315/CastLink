# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = [('D:/workspace/claw/投屏软件/sender/resources', 'resources')]
binaries = [('D:/workspace/claw/投屏软件/tools/ffmpeg.exe', '.')]
hiddenimports = ['castlink', 'castlink.protocol', 'castlink.control', 'castlink.discovery', 'castlink.identity', 'castlink.config', 'castlink.ffmpeg', 'castlink.padev', 'castlink.winvol', 'pyaudiowpatch']
hiddenimports += collect_submodules('castlink_sender')
tmp_ret = collect_all('pyaudiowpatch')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['D:/workspace/claw/投屏软件/sender/castlink_sender/main.py'],
    pathex=['D:/workspace/claw/投屏软件', 'D:/workspace/claw/投屏软件/sender'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name='CastLink-Sender',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['D:/workspace/claw/投屏软件/sender/resources/app_icon.ico'],
    contents_directory='.',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='CastLink-Sender',
)
