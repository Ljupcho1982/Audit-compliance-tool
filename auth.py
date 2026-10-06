"""Login, logout, first-run setup and user administration."""
import os
import time
from collections import defaultdict
from contextlib import closing

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

import authcore as ac
from main import db, log

router = APIRouter(prefix="/api")

SECURE_COOKIE = os.environ.get("COOKIE_SECURE") == "1"  # set when served over HTTPS
_fails: dict[tuple, list] = defaultdict(list)
MAX_FAILS, WINDOW = 5, 15 * 60


def set_cookie(resp: Response, token: str):
    resp.set_cookie(ac.COOKIE, token, max_age=ac.SESSION_SECONDS, httponly=True,
                    samesite="lax", secure=SECURE_COOKIE, path="/")


def me():
    u = ac.current_user()
    return {"id": u["id"], "username": u["username"], "role": u["role"]} if u else None


def n_users():
    with closing(db()) as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


@router.get("/auth/status")
def status():
    return {"setup_needed": n_users() == 0, "user": me()}


class Creds(BaseModel):
    username: str
    password: str


@router.post("/auth/setup")
def setup(c: Creds, resp: Response):
    """First run only: creates the administrator. Any existing chats are assigned to them."""
    username = ac.check_username(c.username)
    ac.check_password(c.password)
    with closing(db()) as conn:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            raise HTTPException(409, "Setup is already complete.")
        cur = conn.execute("INSERT INTO users(username,pw,role,created) VALUES(?,?,?,?)",
                           (username, ac.hash_pw(c.password), "admin", time.strftime("%Y-%m-%d %H:%M")))
        conn.execute("UPDATE chats SET user_id=? WHERE user_id IS NULL", (cur.lastrowid,))
        conn.commit()
        uid = cur.lastrowid
    set_cookie(resp, ac.create_session(uid))
    log("user_created", f"{username} (admin, first-run setup)")
    return {"id": uid, "username": username, "role": "admin"}


@router.post("/auth/login")
def login(c: Creds, request: Request, resp: Response):
    key = (c.username.strip().lower(), request.client.host if request.client else "")
    now = time.time()
    _fails[key] = [t for t in _fails[key] if now - t < WINDOW]
    if len(_fails[key]) >= MAX_FAILS:
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (c.username.strip(),)).fetchone()
    if not ac.verify_pw(c.password, row["pw"] if row else None):
        _fails[key].append(now)
        raise HTTPException(401, "Wrong username or password.")
    _fails.pop(key, None)
    set_cookie(resp, ac.create_session(row["id"]))
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


@router.post("/auth/logout")
def logout(request: Request, resp: Response):
    tok = request.cookies.get(ac.COOKIE)
    if tok:
        ac.end_session(tok)
    resp.delete_cookie(ac.COOKIE, path="/")
    return {"ok": True}


class PwChange(BaseModel):
    current: str
    new: str


@router.post("/auth/password")
def change_password(p: PwChange):
    u = ac.current_user()
    if not u:
        raise HTTPException(401, "Login required.")
    ac.check_password(p.new)
    with closing(db()) as conn:
        row = conn.execute("SELECT pw FROM users WHERE id=?", (u["id"],)).fetchone()
        if not ac.verify_pw(p.current, row["pw"]):
            raise HTTPException(400, "Current password is wrong.")
        conn.execute("UPDATE users SET pw=? WHERE id=?", (ac.hash_pw(p.new), u["id"]))
        conn.commit()
    log("password_changed", u["username"])
    return {"ok": True}


# -------------------------------------------------------------- administration
class NewUser(BaseModel):
    username: str
    password: str
    role: str = "member"


class UserEdit(BaseModel):
    role: str | None = None
    password: str | None = None


def admins_left(conn, excluding: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND id<>?", (excluding,)).fetchone()[0]


@router.get("/users")
def users_list():
    ac.require_admin()
    with closing(db()) as conn:
        return [dict(r) for r in conn.execute("SELECT id, username, role, created FROM users ORDER BY id")]


@router.post("/users")
def user_create(n: NewUser):
    admin = ac.require_admin()
    username = ac.check_username(n.username)
    ac.check_password(n.password)
    if n.role not in ("admin", "member"):
        raise HTTPException(400, "Role must be admin or member.")
    with closing(db()) as conn:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise HTTPException(409, "That username is taken.")
        cur = conn.execute("INSERT INTO users(username,pw,role,created) VALUES(?,?,?,?)",
                           (username, ac.hash_pw(n.password), n.role, time.strftime("%Y-%m-%d %H:%M")))
        conn.commit()
    log("user_created", f"{username} ({n.role}) by {admin['username']}")
    return {"id": cur.lastrowid}


@router.put("/users/{uid}")
def user_edit(uid: int, e: UserEdit):
    admin = ac.require_admin()
    entries = []  # logged after the commit: log() uses its own connection
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise HTTPException(404)
        if e.role is not None:
            if e.role not in ("admin", "member"):
                raise HTTPException(400, "Role must be admin or member.")
            if row["role"] == "admin" and e.role != "admin" and not admins_left(conn, uid):
                raise HTTPException(400, "There must be at least one administrator.")
            conn.execute("UPDATE users SET role=? WHERE id=?", (e.role, uid))
            entries.append(("user_role_changed", f"{row['username']} -> {e.role} by {admin['username']}"))
        if e.password:
            ac.check_password(e.password)
            conn.execute("UPDATE users SET pw=? WHERE id=?", (ac.hash_pw(e.password), uid))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))  # force re-login everywhere
            entries.append(("password_reset", f"{row['username']} by {admin['username']}"))
        conn.commit()
    for action, detail in entries:
        log(action, detail)
    return {"ok": True}


@router.delete("/users/{uid}")
def user_delete(uid: int):
    admin = ac.require_admin()
    if uid == admin["id"]:
        raise HTTPException(400, "You cannot delete your own account.")
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise HTTPException(404)
        if row["role"] == "admin" and not admins_left(conn, uid):
            raise HTTPException(400, "There must be at least one administrator.")
        # Their private chats go with them; shared work (assessments, risks, findings) stays.
        conn.execute("DELETE FROM messages WHERE chat_id IN (SELECT id FROM chats WHERE user_id=?)", (uid,))
        conn.execute("DELETE FROM chats WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()
    log("user_deleted", f"{row['username']} by {admin['username']}")
    return {"ok": True}
