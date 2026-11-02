"""Reuse existing icon pixels and stamp the executable with the package version."""
from pathlib import Path
import struct
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from codex_switcher import __version__

folder = root / "build"
folder.mkdir(exist_ok=True)
icons = [(size, (root / "extension" / "icons" / f"icon{size}.png").read_bytes())
         for size in (16, 32, 48, 128)]
offset = 6 + 16 * len(icons)
entries, payloads = [], []
for size, payload in icons:
    entries.append(struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(payload), offset))
    payloads.append(payload)
    offset += len(payload)
(folder / "app.ico").write_bytes(struct.pack("<HHH", 0, 1, len(icons)) + b"".join(entries + payloads))
version = tuple(int(x) for x in __version__.split(".")) + (0,)
(folder / "version-info.txt").write_text(f'''VSVersionInfo(
  ffi=FixedFileInfo(filevers={version}, prodvers={version}, mask=0x3f,
    flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('FileDescription', 'Codex Switcher'),
    StringStruct('FileVersion', '{__version__}'),
    StringStruct('ProductName', 'Codex Switcher'),
    StringStruct('ProductVersion', '{__version__}'),
    StringStruct('OriginalFilename', 'CodexSwitcher.exe')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
''', encoding="utf8")
