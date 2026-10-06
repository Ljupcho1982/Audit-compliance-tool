"""Build dist/SMS-Workspace-Setup.exe, the single file you hand to other people.

    pip install pyinstaller pillow
    python build_installer.py
"""
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import build_desktop

HERE = Path(__file__).parent
DIST, BUILD = HERE / "dist", HERE / "build"
APP_DIR = DIST / "SMS Workspace"


def pyinstaller(*args):
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--noconsole", "--onefile",
                    "--icon", str(build_desktop.ICON), "--workpath", str(BUILD / "pyi"),
                    "--specpath", str(BUILD), *args], check=True, cwd=HERE)


def main():
    build_desktop.main()  # the app folder (and the portable zip)

    # 1) uninstaller, placed inside the app folder so it ends up in the install directory
    pyinstaller("--name", "Uninstall SMS Workspace", "--distpath", str(BUILD / "uninst"), str(HERE / "uninstall.py"))
    shutil.copy2(BUILD / "uninst" / "Uninstall SMS Workspace.exe", APP_DIR / "Uninstall SMS Workspace.exe")

    # 2) payload: the whole app folder
    payload = BUILD / "payload.zip"
    payload.unlink(missing_ok=True)
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(APP_DIR.rglob("*")):
            z.write(f, Path(APP_DIR.name) / f.relative_to(APP_DIR))

    # 3) the installer, carrying the payload
    pyinstaller("--name", "SMS-Workspace-Setup", "--distpath", str(DIST),
                "--add-data", f"{payload};.", str(HERE / "installer.py"))
    setup = DIST / "SMS-Workspace-Setup.exe"
    print(f"\nInstaller: {setup}  ({setup.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
