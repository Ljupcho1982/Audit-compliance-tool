"""SMS Workspace desktop launcher.

Starts the local server, opens it in its own app window (Microsoft Edge in --app mode, with a
private profile), and stops the server when that window is closed.

    python desktop.py            private: only this computer can connect
    python desktop.py --share    colleagues on your network can sign in from their browsers

Data lives in %APPDATA%\\SMS Workspace when running as the packaged .exe (override with SMS_DB).
"""
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

APP = "SMS Workspace"
PORT = int(os.environ.get("SMS_PORT") or 8765)
FROZEN = getattr(sys, "frozen", False)


def is_portable() -> bool:
    """Portable = the packaged exe carries a marker (single-file build) or has portable.txt beside it."""
    if not FROZEN:
        return False
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "portable.marker"
    return bundled.exists() or (Path(sys.executable).parent / "portable.txt").exists()


def data_dir() -> Path:
    """Portable: a data folder next to the exe, so the whole app can be moved or copied as a unit.
    Installed/dev: %APPDATA%. If the exe's folder is read-only, portable falls back to %APPDATA%."""
    if is_portable():
        d = Path(sys.executable).parent / "SMS Workspace Data"
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write-test"
            probe.write_text("ok")
            probe.unlink()
            return d
        except OSError:
            pass
    d = Path(os.environ.get("APPDATA") or Path.home()) / APP
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_browser():
    pf, pf86 = os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    for p in (rf"{pf86}\Microsoft\Edge\Application\msedge.exe", rf"{pf}\Microsoft\Edge\Application\msedge.exe",
              rf"{pf}\Google\Chrome\Application\chrome.exe", rf"{pf86}\Google\Chrome\Application\chrome.exe"):
        if Path(p).exists():
            return p
    return shutil.which("msedge") or shutil.which("chrome")


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def is_ours(port: int) -> bool:
    try:
        import httpx
        return "setup_needed" in httpx.get(f"http://127.0.0.1:{port}/api/auth/status", timeout=2).json()
    except Exception:
        return False


def open_window(url: str) -> subprocess.Popen | None:
    browser = find_browser()
    if not browser:
        return None
    return subprocess.Popen([
        browser, f"--app={url}", f"--user-data-dir={data_dir() / 'window-profile'}",
        "--window-size=1320,880", "--no-first-run", "--no-default-browser-check",
        # A private, clean profile: no account sync or extensions (a synced profile can open
        # extension welcome pages), and no background process, so closing the window really exits.
        "--disable-sync", "--disable-extensions", "--disable-background-mode",
        "--disable-features=Translate,msEdgeSidebarV2"])


def main():
    share = "--share" in sys.argv
    if FROZEN:
        # No console in the packaged app: keep a log, and give libraries a stdout to write to.
        log = open(data_dir() / "app.log", "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = log
        os.environ.setdefault("SMS_DB", str(data_dir() / "workspace.db"))
    os.environ["SMS_SHARE"] = "1" if share else "0"
    os.environ["SMS_PORT"] = str(PORT)
    url = f"http://127.0.0.1:{PORT}"

    if port_in_use(PORT):
        if is_ours(PORT):  # already running: just show another window
            open_window(url)
            return
        sys.exit(f"Port {PORT} is used by another program. Set SMS_PORT to a free port.")

    import uvicorn
    from main import app

    server = uvicorn.Server(uvicorn.Config(
        app, host="0.0.0.0" if share else "127.0.0.1", port=PORT, log_level="warning", log_config=None))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(120):
        if getattr(server, "started", False):
            break
        time.sleep(0.25)
    else:
        sys.exit("The server did not start. See app.log in " + str(data_dir()))

    win = open_window(url)
    if win:
        win.wait()  # returns when the app window is closed
    else:
        # No Edge/Chrome found: open the default browser and keep the server alive with a tiny window.
        import tkinter as tk
        import webbrowser
        webbrowser.open(url)
        root = tk.Tk()
        root.title(APP)
        tk.Label(root, text=f"{APP} is running at {url}\nClose this window to stop it.", padx=24, pady=20).pack()
        root.mainloop()
    server.should_exit = True
    time.sleep(1)


if __name__ == "__main__":
    main()
