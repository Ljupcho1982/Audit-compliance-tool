"""SMS Workspace setup. Per-user install, no administrator rights needed.

    SMS-Workspace-Setup.exe           interactive
    SMS-Workspace-Setup.exe /S        silent (add /D=C:\\some\\folder to choose the location)
"""
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import install_common as ic

SILENT = any(a.lower() == "/s" for a in sys.argv[1:])
DIR_ARG = next((a[3:].strip('"') for a in sys.argv[1:] if a.upper().startswith("/D=")), None)


def payload() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / "payload.zip"


def ollama_present() -> bool:
    if shutil.which("ollama"):
        return True
    import os
    return (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe").exists()


def install(target: Path, desktop: bool, progress=lambda done, total: None):
    """Replaces the program folder, adds shortcuts and the Installed-apps entry. User data is in
    %APPDATA% and is never touched."""
    if target.exists():
        # Only ever replace a previous install; never wipe an arbitrary folder the user typed.
        if any(target.iterdir()) and not (target / ic.EXE).exists():
            raise RuntimeError(f"{target} already contains other files. Choose an empty folder "
                               f"or a new one, for example {ic.default_dir()}.")
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with zipfile.ZipFile(payload()) as z:
        members = z.infolist()
        total = sum(m.file_size for m in members) or 1
        done = 0
        for m in members:
            # the zip's top folder is the app folder name; install its contents directly into target
            rel = Path(*Path(m.filename).parts[1:]) if len(Path(m.filename).parts) > 1 else None
            if rel is None:
                continue
            dest = target / rel
            if m.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
            done += m.file_size
            progress(done, total)
    exe = target / ic.EXE
    sm = ic.start_menu_dir()
    ic.make_shortcut(sm / f"{ic.APP}.lnk", exe, desc="Aviation safety management audit assistant")
    ic.make_shortcut(sm / f"{ic.APP} (share on network).lnk", exe, "--share",
                     desc="Let colleagues on your network sign in from their browsers")
    ic.make_shortcut(sm / "Uninstall.lnk", target / ic.UNINSTALLER, desc="Uninstall SMS Workspace")
    if desktop:
        ic.make_shortcut(ic.desktop_dir() / f"{ic.APP}.lnk", exe, desc="Aviation safety management audit assistant")
    size_kb = sum(f.stat().st_size for f in target.rglob("*") if f.is_file()) // 1024
    ic.register(target, size_kb)
    return exe


def run_silent():
    target = Path(DIR_ARG) if DIR_ARG else ic.default_dir()
    if ic.is_running():
        sys.exit("SMS Workspace is running. Close it and run setup again.")
    install(target, desktop=False)


def run_gui():
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title(f"{ic.APP} Setup")
    root.geometry("520x330")
    root.resizable(False, False)
    pad = {"padx": 22}

    existing = ic.installed_dir()
    tk.Label(root, text=f"{'Update' if existing else 'Install'} {ic.APP} {ic.VERSION}", font=("Segoe UI", 15, "bold")
             ).pack(anchor="w", pady=(18, 4), **pad)
    tk.Label(root, justify="left", wraplength=470, text=(
        "A local AI workspace for aviation safety management audits. Your documents and data stay "
        "on this computer. Installs for your user account only; no administrator rights needed."
        + ("\n\nYour existing data is kept." if existing else "")), fg="#444").pack(anchor="w", **pad)

    tk.Label(root, text="Install location").pack(anchor="w", pady=(14, 0), **pad)
    path = tk.StringVar(value=str(existing or ic.default_dir()))
    tk.Entry(root, textvariable=path, width=64).pack(anchor="w", **pad)
    desktop = tk.BooleanVar(value=True)
    tk.Checkbutton(root, text="Create a desktop shortcut", variable=desktop).pack(anchor="w", pady=(8, 0), **pad)

    bar = ttk.Progressbar(root, length=476, mode="determinate")
    status = tk.Label(root, text="", fg="#444")
    launch = tk.BooleanVar(value=True)
    btns = tk.Frame(root)
    btns.pack(side="bottom", fill="x", pady=16, padx=22)

    def finish(exe):
        bar.pack_forget()
        status.config(text="Installed.")
        for w in btns.winfo_children():
            w.destroy()
        if not ollama_present():
            tk.Label(root, fg="#a15c00", justify="left", wraplength=470, text=(
                "Ollama was not found. Install it from ollama.com, then run:\n"
                "  ollama pull qwen3:8b\n  ollama pull nomic-embed-text")).pack(anchor="w", **pad)
        tk.Checkbutton(root, text="Start SMS Workspace now", variable=launch).pack(anchor="w", **pad)

        def done():
            if launch.get():
                subprocess.Popen([str(exe)], cwd=str(exe.parent))
            root.destroy()
        ttk.Button(btns, text="Finish", command=done).pack(side="right")

    def go():
        if ic.is_running():
            messagebox.showwarning(ic.APP, "SMS Workspace is running. Close it, then click Install again.")
            return
        target = Path(path.get().strip())
        if not target.name:
            messagebox.showerror(ic.APP, "Choose an install folder.")
            return
        install_btn.config(state="disabled")
        status.pack(anchor="w", **pad)
        bar.pack(anchor="w", pady=4, **pad)
        status.config(text="Installing…")

        def work():
            try:
                exe = install(target, desktop.get(),
                              lambda d, t: root.after(0, lambda: bar.configure(value=d * 100 / t)))
                root.after(0, lambda: finish(exe))
            except Exception as e:  # shown to the user rather than a silent failure
                root.after(0, lambda: (messagebox.showerror(ic.APP, f"Setup failed:\n{e}"), root.destroy()))
        threading.Thread(target=work, daemon=True).start()

    install_btn = ttk.Button(btns, text="Update" if existing else "Install", command=go)
    install_btn.pack(side="right")
    ttk.Button(btns, text="Cancel", command=root.destroy).pack(side="right", padx=8)
    root.mainloop()


if __name__ == "__main__":
    run_silent() if SILENT else run_gui()
