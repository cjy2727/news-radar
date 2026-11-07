# Build on Windows: python -m PyInstaller --noconfirm NewsRadar.spec
a = Analysis(['launcher.py'], pathex=[], binaries=[], datas=[('static', 'static')],
             hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='NewsRadar',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False)
