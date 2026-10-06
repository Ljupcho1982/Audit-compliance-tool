"""Shared by the installer and uninstaller."""
import base64
import os
import subprocess
import winreg
from pathlib import Path

APP = "SMS Workspace"
VERSION = "1.0.0"
PUBLISHER = "SMS Workspace"
EXE = "SMS Workspace.exe"
UNINSTALLER = "Uninstall SMS Workspace.exe"
REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SMSWorkspace"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def default_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs" / APP


def start_menu_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP


def desktop_dir() -> Path:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            return Path(os.path.expandvars(winreg.QueryValueEx(k, "Desktop")[0]))
    except OSError:
        return Path.home() / "Desktop"


def data_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / APP


def is_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {EXE}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    return EXE.lower() in out.lower()


def make_shortcut(lnk: Path, target: Path, args: str = "", workdir: Path | None = None, desc: str = ""):
    """Creates a .lnk via the Windows scripting host. Passed as an encoded script, so paths with
    quotes or non-ASCII characters are safe."""
    lnk.parent.mkdir(parents=True, exist_ok=True)

    def q(s):  # PowerShell single-quoted literal
        return "'" + str(s).replace("'", "''") + "'"

    script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({q(lnk)});"
              f"$s.TargetPath={q(target)};$s.Arguments={q(args)};"
              f"$s.WorkingDirectory={q(workdir or target.parent)};$s.Description={q(desc)};"
              f"$s.IconLocation={q(str(target) + ',0')};$s.Save()")
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                   check=True, capture_output=True, creationflags=NO_WINDOW)


def register(install_dir: Path, size_kb: int):
    """Adds the app to Windows 'Installed apps' (current user only, no admin needed)."""
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
        for name, val in (("DisplayName", APP), ("DisplayVersion", VERSION), ("Publisher", PUBLISHER),
                          ("InstallLocation", str(install_dir)),
                          ("DisplayIcon", str(install_dir / EXE)),
                          ("UninstallString", f'"{install_dir / UNINSTALLER}"'),
                          ("QuietUninstallString", f'"{install_dir / UNINSTALLER}" /S')):
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
        winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)


def installed_dir() -> Path | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
            return Path(winreg.QueryValueEx(k, "InstallLocation")[0])
    except OSError:
        return None


def unregister():
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_KEY)
    except OSError:
        pass
