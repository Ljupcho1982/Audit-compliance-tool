# Working on SMS Workspace on Windows, and pushing to GitHub

Everything here is for **Windows 10/11 with PowerShell**. Git is already installed on this
computer (`git --version` shows 2.x).

> **PowerShell tip:** Windows PowerShell 5.1 does not understand `&&`. Put commands on separate
> lines, or join them with `;`.

---

## 1. The project at a glance

Project folder: `C:\Users\Ljupcho\Desktop\Ilche\sms-workspace`

| File | What it is |
|---|---|
| `main.py` | The server (FastAPI): documents, Q&A, gap analysis, report |
| `features.py` | Check manual, occurrence reports, risk assessments |
| `chats.py` | Projects and saved chats |
| `auth.py`, `authcore.py` | Login, users, sessions |
| `desktop.py` | Desktop launcher (opens the app window, runs the server) |
| `static/index.html` | The whole user interface (one file) |
| `build_portable.py` | Builds the portable app (`dist\portable\SMS-Workspace-Portable.exe`) |
| `build_installer.py` | Builds the optional installer |
| `workspace.db` | **Your local data** (never commit this) |

---

## 2. Everyday workflow

**1. Start Ollama** (the AI runs locally). Check it is up and has the models:

```powershell
ollama list
```

You need `qwen3:8b` and `nomic-embed-text`. If one is missing: `ollama pull qwen3:8b`.

**2. Open the project and run the server:**

```powershell
cd C:\Users\Ljupcho\Desktop\Ilche\sms-workspace
python -m uvicorn main:app --port 8000 --reload
```

Open http://localhost:8000. `--reload` restarts the server when you save a Python file. For
changes to `static\index.html`, just refresh the browser.

**3. Edit, refresh, test.** Stop the server with `Ctrl+C`.

**4. Run it as the desktop app instead** (same code, own window):

```powershell
python desktop.py
```

**5. Build the portable app to share:**

```powershell
pip install pyinstaller pillow
python build_portable.py
```

Output: `dist\portable\SMS-Workspace-Portable.exe` and `dist\SMS-Workspace-Portable.zip`.
Close any running copy of the app first, or the build cannot replace the exe.

### Using a throwaway database while testing

The app uses `workspace.db` in the project folder. To experiment without touching it:

```powershell
$env:SMS_DB = "$env:TEMP\scratch.db"
python -m uvicorn main:app --port 8001
```

(`$env:SMS_DB` only lasts for that PowerShell window.)

---

## 3. First-time GitHub setup (one time only)

### 3.1 Tell Git who you are

```powershell
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

Use the email on your GitHub account (or the `...@users.noreply.github.com` address from
GitHub → Settings → Emails if you want to keep your email private).

### 3.2 Create the empty repository on GitHub

1. Go to https://github.com/new
2. Name it (for example `sms-workspace`). Choose **Private** unless you want it public.
3. **Do not** tick "Add a README", ".gitignore" or a licence. Leave it empty.
4. Click **Create repository**, then copy the address shown, like
   `https://github.com/YOUR-NAME/sms-workspace.git`.

### 3.3 Check the safety net before the first commit

The `.gitignore` file in this folder keeps your data and build output out of GitHub. Confirm it
works **before** you add anything:

```powershell
cd C:\Users\Ljupcho\Desktop\Ilche\sms-workspace
git init -b main
git add .
git status
```

Read the list under "Changes to be committed". It must **not** contain:

- `workspace.db` (your documents, chats and password hashes)
- `SMS Workspace Data\`
- `dist\`, `build\`, `exports\`
- any `.env` file

If any appear, stop and fix `.gitignore` first (see "Troubleshooting" below). Once real data
is pushed to GitHub, treat it as exposed, even if you delete it later.

### 3.4 First commit and push

```powershell
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR-NAME/sms-workspace.git
git push -u origin main
```

The first push opens a browser window (Git Credential Manager, included with Git for Windows).
Sign in to GitHub there and approve. After that, Windows remembers you.

---

## 4. Everyday Git: save and push your work

```powershell
cd C:\Users\Ljupcho\Desktop\Ilche\sms-workspace
git status                      # what changed
git diff                        # see the changes line by line (press q to leave)
git add .                       # stage everything not ignored
git commit -m "Short description of what changed and why"
git push                        # send to GitHub
```

Good habits:

- Commit small and often, with a message that says **why**, not just "update".
- Run `git status` before every commit and look for anything you did not expect.
- Run the app after changes and before pushing.

### Getting changes from GitHub (another computer, or a teammate)

```powershell
git pull
```

On a **new** computer:

```powershell
git clone https://github.com/YOUR-NAME/sms-workspace.git
cd sms-workspace
pip install -r requirements.txt
```

You start with an empty database there and create the admin account again.

### Trying something risky: use a branch

```powershell
git switch -c my-experiment     # create and switch to a new branch
# ...edit, commit as usual...
git push -u origin my-experiment
git switch main                 # go back to the stable version
```

On github.com you can then open a **Pull request** to review and merge the branch.

---

## 5. Sharing the app: use Releases, not the repository

Do **not** commit `dist\` or the `.exe`. Binaries bloat the repository and GitHub rejects files
over 100 MB. Instead:

1. Build: `python build_portable.py`
2. On GitHub: **Releases → Draft a new release**
3. Tag it (for example `v1.0.0`), then drag in `dist\SMS-Workspace-Portable.zip`
4. **Publish release.** Send people the release link.

Because the app is unsigned, Windows SmartScreen warns on first run ("More info", then "Run
anyway"). Mention that when you share it.

---

## 6. Troubleshooting

**`The token '&&' is not a valid statement separator`**
PowerShell 5.1: use a new line or `;` instead of `&&`.

**Port 8000 (or 8765) is already in use**
Another copy is running. Find and stop it:
```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```
Use `8765` for the desktop/portable app.

**`workspace.db` or another data file shows up in `git status`**
It was not ignored. If it is **not yet committed**, check that `.gitignore` is in the same folder
and spelled exactly `.gitignore`. If it **was already committed**:
```powershell
git rm --cached workspace.db
git commit -m "Stop tracking the database"
git push
```
That stops tracking it going forward, but the old commits still contain it. If it held real
data and the repository is (or was) public, assume the data and any passwords are exposed.
Change them, and ask for help removing it from history.

**Push rejected: `non-fast-forward` or `fetch first`**
GitHub has commits you do not have. Run `git pull`, fix any conflicts, then `git push`.

**Push rejected: file larger than 100 MB**
You added a build file. Remove it (`git rm --cached <file>`), make sure it is covered by
`.gitignore`, and amend the commit with `git commit --amend` before pushing again.

**Authentication failed / asks for a password**
GitHub no longer accepts account passwords on the command line. Use the browser sign-in that
Git Credential Manager offers, or a personal access token (GitHub → Settings → Developer
settings → Personal access tokens). To retry the sign-in, open **Windows Credential Manager →
Windows Credentials**, remove the `git:https://github.com` entry, and push again.

**`warning: LF will be replaced by CRLF`**
Harmless on Windows. Git is converting line endings.

**`fatal: detected dubious ownership`**
```powershell
git config --global --add safe.directory C:/Users/Ljupcho/Desktop/Ilche/sms-workspace
```

**The build fails because the exe is in use**
Close the app (and any running `SMS-Workspace-Portable.exe`), then build again.

**The app says "Ollama not running" or "Missing model"**
Start Ollama and run `ollama pull qwen3:8b` and `ollama pull nomic-embed-text`.

---

## 7. Quick reference

| I want to... | Command |
|---|---|
| Run the app for development | `python -m uvicorn main:app --port 8000 --reload` |
| Run it as the desktop app | `python desktop.py` |
| Build the portable app | `python build_portable.py` |
| See what changed | `git status` |
| Save my work | `git add .` then `git commit -m "message"` |
| Send it to GitHub | `git push` |
| Get the latest from GitHub | `git pull` |
| Undo edits to one file (not yet committed) | `git restore path\to\file` |
| See history | `git log --oneline` |
