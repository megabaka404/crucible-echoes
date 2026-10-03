# PyInstaller onedir build for the desktop client.
from pathlib import Path
from importlib.metadata import distributions
import os
import re
import sys

from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.win32.versioninfo import FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo

ROOT = Path(SPECPATH).parent
version_match = re.search(r'^version\s*=\s*"(\d+\.\d+\.\d+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.MULTILINE)
if version_match is None:
    raise ValueError("A three-part project version is required for the Windows package")
project_version = version_match.group(1)
version_tuple = (*map(int, project_version.split(".")), 0)
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=version_tuple, prodvers=version_tuple, mask=0x3F, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
    kids=[StringFileInfo([StringTable("040904B0", [
        StringStruct("FileDescription", "Crucible Echoes desktop game"),
        StringStruct("FileVersion", project_version),
        StringStruct("ProductName", "Crucible Echoes"),
        StringStruct("ProductVersion", project_version),
        StringStruct("OriginalFilename", "CrucibleEchoes.exe"),
        StringStruct("LegalCopyright", "MIT License - Crucible Echoes contributors"),
    ])]), VarFileInfo([VarStruct("Translation", [1033, 1200])])],
)
icon_path = os.environ.get("CRUCIBLE_ICON")
if icon_path:
    icon_path = str(Path(icon_path).resolve(strict=True))
hiddenimports = ["desktop.bridge", "desktop.view_model", "desktop.smoke", *collect_submodules("crucible_echoes")]
datas = [
    (str(ROOT / "frontend"), "frontend"),
    (str(ROOT / "src" / "crucible_echoes" / "data"), "crucible_echoes/data"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "README.md"), "docs"),
]
# Retain installed dependency notices in the redistributable directory. Some
# dependencies package notices under dist-info/licenses rather than LICENSE.
for distribution in distributions():
    for relative in distribution.files or ():
        if Path(relative).name.upper().startswith(("LICENSE", "COPYING")):
            notice = Path(distribution.locate_file(relative))
            if notice.is_file():
                datas.append((str(notice), f"licenses/{distribution.metadata['Name']}"))
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.is_file():
    datas.append((str(python_license), "licenses/Python"))

a = Analysis(
    [str(ROOT / "desktop" / "app.py")],
    pathex=[str(ROOT), str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CrucibleEchoes",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    # Set CRUCIBLE_ICON to an existing .ico to customize the executable icon.
    icon=icon_path,
    version=version_info,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    name="CrucibleEchoes",
)
