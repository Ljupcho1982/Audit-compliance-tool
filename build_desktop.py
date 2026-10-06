"""Build the Windows desktop app into dist/SMS Workspace/ (and a .zip).

    pip install pyinstaller pillow
    python build_desktop.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
ICON = HERE / "build" / "app.ico"


def make_icon():
    ICON.parent.mkdir(exist_ok=True)
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, 248, 248), radius=56, fill=(31, 79, 216, 255))
    # a shield with a check mark: safety management
    d.polygon([(128, 44), (200, 72), (200, 132), (128, 212), (56, 132), (56, 72)], fill=(255, 255, 255, 255))
    d.line([(96, 126), (122, 152), (166, 100)], fill=(31, 79, 216, 255), width=16, joint="curve")
    img.save(ICON, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])


def main():
    make_icon()
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--noconsole", "--onedir",
           "--name", "SMS Workspace", "--icon", str(ICON),
           "--add-data", f"{HERE / 'static'};static",
           "--collect-submodules", "uvicorn",
           "--hidden-import", "docx_export",
           "--distpath", str(HERE / "dist"), "--workpath", str(HERE / "build" / "pyi"),
           "--specpath", str(HERE / "build"),
           str(HERE / "desktop.py")]
    subprocess.run(cmd, check=True, cwd=HERE)
    out = HERE / "dist" / "SMS Workspace"

    # Launchers next to the exe: normal (private) and shared-on-network.
    (out / "SMS Workspace (share).bat").write_text(
        '@echo off\r\nstart "" "%~dp0SMS Workspace.exe" --share\r\n', encoding="ascii")
    (out / "README.txt").write_text(
        "SMS Workspace\r\n=============\r\n"
        "Double-click 'SMS Workspace.exe'. The first time, create the administrator account.\r\n\r\n"
        "Needs Ollama (https://ollama.com) running, with these models pulled:\r\n"
        "    ollama pull qwen3:8b\r\n    ollama pull nomic-embed-text\r\n\r\n"
        "Colleagues: start with 'SMS Workspace (share).bat', then give them the address shown in\r\n"
        "the Users tab. Connections on a network are not encrypted; use a trusted network or VPN.\r\n\r\n"
        "Your data is stored in %APPDATA%\\SMS Workspace (back this folder up).\r\n",
        encoding="ascii")
    zip_path = shutil.make_archive(str(HERE / "dist" / "SMS-Workspace-win64"), "zip", out.parent, out.name)
    print("Built:", out)
    print("Zip:  ", zip_path)


if __name__ == "__main__":
    main()
