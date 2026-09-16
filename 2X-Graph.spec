# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files

selenium_data = collect_data_files('selenium')


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=selenium_data,
    hiddenimports=[
        'trenball',
        'trenball.capture',
        'trenball.config',
        'trenball.region_selector',
        'trenball.trend_decoder',
        'trenball.trend_tracker',
        'trenball.telegram_alerts',
        'trenball.selenium_collector',
        'selenium',
        'selenium.webdriver',
        'mss',
        'cv2',
        'numpy',
        'PIL',
    ],
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
    name='2X-Graph',
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
