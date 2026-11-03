# Build on Windows with Python 3.14 x64. Browsers are downloaded at first use.
from pathlib import Path

root = Path(SPECPATH).parent
a = Analysis([str(root / 'switcher.pyw')], pathex=[str(root)],
             binaries=[], datas=[(str(root / 'codex_switcher' / 'dashboard.html'), 'codex_switcher'),
                                (str(root / 'LICENSE'), '.'),
                                (str(root / 'build' / 'app.ico'), 'codex_switcher'),
                                (str(root / 'extension'), 'extension')],
             hiddenimports=['playwright.sync_api', 'PIL.Image', 'PIL.PngImagePlugin', 'zxingcpp'], hookspath=[], hooksconfig={},
             runtime_hooks=[], excludes=['tkinter', 'numpy', 'pandas', 'matplotlib', 'PyQt5', 'PySide6'],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='CodexSwitcher', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          disable_windowed_traceback=False, icon=str(root / 'build' / 'app.ico'),
          version=str(root / 'build' / 'version-info.txt'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='CodexSwitcher')
