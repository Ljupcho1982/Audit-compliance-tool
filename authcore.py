"""Authentication core: password hashing, sessions, and the request guard.

Kept free of imports from main.py so main can attach the guard when it builds the app.
Set SMS_DB to point at a different database file (used for testing).
"""
import contextvars
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

from fastapi import HTTPException, Request

DB_PATH = Path(os.environ.get("SMS_DB") or Path(__file__).parent / "workspace.db")
SESSION_SECONDS = 7 * 24 * 3600
COOKIE = "sid"

# The signed-in user (dict with id, username, role) for the request being handled.
CURRENT_USER = contextvars.ContextVar("current_user", default=None)


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    c = _conn()
    try:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE,
                pw TEXT, role TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS sessions(
                token_hash TEXT PRIMARY KEY, user_id INTEGER, expires REAL);
            """
        )
        c.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        c.commit()
    finally:
        c.close()


init()


# ---------------------------------------------------------------- passwords
def hash_pw(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


_DUMMY = hash_pw("dummy-password-for-timing")


def verify_pw(pw: str, stored: str | None) -> bool:
    """Always does one hash so a missing user takes as long as a wrong password."""
    try:
        _, salt, want = (stored or _DUMMY).split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got.hex(), want) and stored is not None
    except ValueError:
        return False


def check_username(u: str) -> str:
    u = u.strip()
    if not re.fullmatch(r"[A-Za-z0-9._@-]{3,40}", u):
        raise HTTPException(400, "Username must be 3-40 characters: letters, digits, . _ @ -")
    return u


def check_password(p: str):
    if len(p) < 10:
        raise HTTPException(400, "Password must be at least 10 characters.")


# ----------------------------------------------------------------- sessions
def _hash_token(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    c = _conn()
    try:
        c.execute("INSERT INTO sessions VALUES(?,?,?)",
                  (_hash_token(token), user_id, time.time() + SESSION_SECONDS))
        c.commit()
    finally:
        c.close()
    return token


def end_session(token: str):
    c = _conn()
    try:
        c.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))
        c.commit()
    finally:
        c.close()


def user_for_token(token: str | None):
    if not token:
        return None
    c = _conn()
    try:
        row = c.execute(
            "SELECT u.id, u.username, u.role FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires>?", (_hash_token(token), time.time())).fetchone()
    finally:
        c.close()
    return dict(row) if row else None


# -------------------------------------------------------------- request guard
async def auth_dep(request: Request):
    """Applied to every route. /api/* needs a session, except /api/auth/*."""
    path = request.url.path
    if not path.startswith("/api/"):
        return
    # Cross-site writes are refused outright (SameSite=Lax on the cookie is the first defence).
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
            raise HTTPException(403, "Cross-origin request blocked.")
    user = user_for_token(request.cookies.get(COOKIE))
    if user:
        CURRENT_USER.set(user)
    if not user and not path.startswith("/api/auth/"):
        raise HTTPException(401, "Login required.")


def current_user():
    return CURRENT_USER.get()


def require_admin():
    u = CURRENT_USER.get()
    if not u or u["role"] != "admin":
        raise HTTPException(403, "Administrators only.")
    return u
