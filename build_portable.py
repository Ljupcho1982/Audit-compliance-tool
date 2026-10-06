"""Build the portable app: dist/portable/SMS-Workspace-Portable.exe (+ share launcher, readme, zip).

One file, nothing to install. It keeps its data in a 'SMS Workspace Data' folder beside the exe,
so the whole thing can live on a USB stick or a shared drive.

    pip install pyinstaller pillow
    python build_portable.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

import build_desktop

HERE = Path(__file__).parent
OUT = HERE / "dist" / "portable"


def main():
    build_desktop.make_icon()
    marker = HERE / "build" / "portable.marker"
    marker.write_text("portable")
    OUT.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--noconsole", "--onefile",
        "--name", "SMS-Workspace-Portable", "--icon", str(build_desktop.ICON),
        "--add-data", f"{HERE / 'static'};static",
        "--add-data", f"{marker};.",
        "--collect-submodules", "uvicorn", "--hidden-import", "docx_export",
        "--distpath", str(OUT), "--workpath", str(HERE / "build" / "pyi-portable"),
        "--specpath", str(HERE / "build"), str(HERE / "desktop.py")], check=True, cwd=HERE)

    (OUT / "SMS Workspace (share).bat").write_text(
        '@echo off\r\nstart "" "%~dp0SMS-Workspace-Portable.exe" --share\r\n', encoding="ascii")
    (OUT / "README.txt").write_text(
        "SMS Workspace (portable)\r\n========================\r\n"
        "Nothing to install. Double-click SMS-Workspace-Portable.exe. The first time, create the\r\n"
        "administrator account.\r\n\r\n"
        "Your data is kept in the 'SMS Workspace Data' folder created next to the exe. To move the\r\n"
        "app, move the exe and that folder together. To back up, copy that folder.\r\n"
        "(If the exe is in a read-only place, data goes to %APPDATA%\\SMS Workspace instead.)\r\n\r\n"
        "Needs, on the computer it runs on:\r\n"
        "  - Windows 10/11 with Microsoft Edge (already included)\r\n"
        "  - Ollama (https://ollama.com) running, with:\r\n"
        "        ollama pull qwen3:8b\r\n        ollama pull nomic-embed-text\r\n\r\n"
        "Colleagues: start with 'SMS Workspace (share).bat' and give them the address shown in the\r\n"
        "Users tab. Network connections are not encrypted; use a trusted network or VPN.\r\n\r\n"
        "The first start takes a few seconds while the app unpacks itself. Windows SmartScreen may\r\n"
        "warn about an unsigned app: choose 'More info', then 'Run anyway'.\r\n", encoding="ascii")
    zip_path = shutil.make_archive(str(HERE / "dist" / "SMS-Workspace-Portable"), "zip", OUT)
    exe = OUT / "SMS-Workspace-Portable.exe"
    print(f"\nPortable app: {exe}  ({exe.stat().st_size / 1e6:.0f} MB)\nZip: {zip_path}")


if __name__ == "__main__":
    main()
