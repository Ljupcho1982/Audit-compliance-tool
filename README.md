# SMS Workspace

Local AI workspace for aviation safety management (ICAO Annex 19 SMS) audits.
Runs fully offline with Ollama. No data leaves the machine.

## Portable app (what you share)
    pip install -r requirements.txt pyinstaller pillow
    python build_portable.py         # -> dist/portable/SMS-Workspace-Portable.exe  and  dist/SMS-Workspace-Portable.zip

One exe, nothing to install, nothing written to the registry or Start Menu. Data lives in a
`SMS Workspace Data` folder beside the exe: move or back up the two together. Colleagues
connect via `SMS Workspace (share).bat` (address shown in the Users tab). First start takes
a few seconds while it unpacks. Unsigned, so SmartScreen may warn ("More info" > "Run anyway").

## Installer (optional alternative)
    pip install -r requirements.txt pyinstaller pillow
    python build_installer.py        # -> dist/SMS-Workspace-Setup.exe  (one file, ~77 MB)

Send that one file. It installs for the current user (no administrator rights), adds Start Menu
shortcuts (plus an optional desktop one) and an entry in Windows "Installed apps", and updates
in place when run again; user data is never touched by an update. `SMS-Workspace-Setup.exe /S`
installs silently (`/D=C:\folder` picks the location). The uninstaller asks whether to keep
your data. The installer is unsigned, so Windows SmartScreen may warn on first run
("More info" > "Run anyway"); signing needs a code-signing certificate.

## Desktop app only (portable)
    python build_desktop.py          # -> dist/SMS Workspace/  and  dist/SMS-Workspace-win64.zip

Run `SMS Workspace.exe`. It opens in its own window and stops when you close it.
Data is stored in `%APPDATA%\SMS Workspace` (back it up). Needs Ollama running with
`qwen3:8b` and `nomic-embed-text` pulled. `SMS Workspace (share).bat` lets colleagues on
the network sign in from their browsers (the address is shown in the Users tab); traffic is
not encrypted, so use a trusted network or VPN.

## Run from source
    ollama pull qwen3:8b
    ollama pull nomic-embed-text
    pip install -r requirements.txt
    python -m uvicorn main:app --port 8000      # or: python desktop.py [--share]

Override models with CHAT_MODEL / EMBED_MODEL, the database file with SMS_DB.

## Users and login
- First visit: create the administrator. Until then the API refuses every request.
- Admins add users (Users tab) and can reset passwords, change roles, delete users, delete projects.
  Members can use everything else. There is no self-signup.
- Chats are private to each user. Documents, projects, assessments, risks, occurrences and
  findings are shared. The audit trail, gap confirmations and risk decisions record who acted.
- Passwords are hashed with scrypt; sessions are HttpOnly SameSite cookies (7 days); cross-origin
  writes are refused; 5 failed logins lock that username+address for 15 minutes.
  Set COOKIE_SECURE=1 if you put it behind HTTPS.

## What it does
- Library: upload PDF/DOCX/TXT/MD as your documents, binding regulation or AMC/guidance.
- Chat with projects and saved history; answers cite sources or say no evidence was found.
- Check manual against a requirement (aligned / drifted / gap) with cited passages.
- Gap analysis against the 12 SMS elements, with a verification pass; a person confirms each status.
- Occurrence reports: CSV import, 12-month chart, similar-report search, monthly safety pack draft.
- Risk assessment drafting against your own matrix; the meeting decides.
- Findings tracker, audit trail, and an audit report as Markdown or Word.
- Projects scope the checks, analyses, risks and occurrence reports to library + project.

Try it with the files in sample_docs/ (a fictional operator with deliberate gaps).
