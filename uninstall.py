"""Uninstaller. A running .exe cannot delete its own folder, so it first copies itself to %TEMP%
and finishes the job from there."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import install_common as ic

SILENT = any(a.lower() == "/s" for a in sys.argv[1:])
KEEP_DATA_ARG = "--delete-data"


def remove_everything(install_dir: Path, delete_data: bool):
    for _ in range(20):  # wait for the original uninstaller to exit and release its files
        try:
            shutil.rmtree(install_dir)
            break
        except OSError:
            time.sleep(0.5)
    shutil.rmtree(ic.start_menu_dir(), ignore_errors=True)
    (ic.desktop_dir() / f"{ic.APP}.lnk").unlink(missing_ok=True)
    ic.unregister()
    if delete_data:
        shutil.rmtree(ic.data_dir(), ignore_errors=True)


def main():
    args = sys.argv[1:]
    if "--finish" in args:  # second stage, running from %TEMP%
        remove_everything(Path(args[args.index("--finish") + 1]), KEEP_DATA_ARG in args)
        return

    install_dir = ic.installed_dir() or Path(sys.executable).parent
    if ic.is_running():
        if SILENT:
            sys.exit("SMS Workspace is running. Close it first.")
        import tkinter as tk
        from tkinter import messagebox
        tk.Tk().withdraw()
        messagebox.showwarning(ic.APP, "SMS Workspace is running. Close it, then run the uninstaller again.")
        return

    delete_data = False
    if not SILENT:
        import tkinter as tk
        from tkinter import messagebox
        tk.Tk().withdraw()
        if not messagebox.askyesno(ic.APP, f"Remove {ic.APP} from this computer?"):
            return
        delete_data = messagebox.askyesno(
            ic.APP, f"Also delete your data (documents, chats, users, reports) in\n{ic.data_dir()} ?\n\n"
                    "Choose No to keep it, for example if you plan to reinstall.", default="no")

    tmp = Path(tempfile.mkdtemp()) / "smsw_uninstall.exe"
    shutil.copy2(sys.executable, tmp)
    cmd = [str(tmp), "--finish", str(install_dir)] + ([KEEP_DATA_ARG] if delete_data else [])
    subprocess.Popen(cmd, creationflags=ic.NO_WINDOW | 0x00000008)  # detached, no console
    if not SILENT:
        import tkinter as tk
        from tkinter import messagebox
        tk.Tk().withdraw()
        messagebox.showinfo(ic.APP, "SMS Workspace is being removed.")


if __name__ == "__main__":
    main()
