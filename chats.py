"""Projects and saved chat history.

A project groups chats and can hold its own documents. Documents with no project belong to
the shared library and are visible to every chat.
"""
import json
import time
from contextlib import closing

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from authcore import current_user, require_admin
from main import ASK_SYSTEM, chat, db, format_sources, log, retrieve

router = APIRouter(prefix="/api")

with closing(db()) as _c:
    _c.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects(
            id INTEGER PRIMARY KEY, name TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS chats(
            id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT, created TEXT, updated TEXT,
            user_id INTEGER);
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY, chat_id INTEGER, role TEXT, content TEXT,
            sources TEXT, ts TEXT);
        """
    )
    if "user_id" not in [r["name"] for r in _c.execute("PRAGMA table_info(chats)")]:
        _c.execute("ALTER TABLE chats ADD COLUMN user_id INTEGER")
    _c.commit()


def uid():
    return current_user()["id"]


def own_chat(c, cid):
    """Chats are private: another user's chat looks like it doesn't exist."""
    row = c.execute("SELECT * FROM chats WHERE id=? AND user_id=?", (cid, uid())).fetchone()
    if not row:
        raise HTTPException(404)
    return row


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------- projects
class Name(BaseModel):
    name: str


@router.get("/projects")
def projects_list():
    with closing(db()) as c:
        return [dict(r) for r in c.execute(
            "SELECT p.id, p.name, p.created,"
            " (SELECT COUNT(*) FROM chats WHERE project_id=p.id AND user_id=?) AS n_chats,"
            " (SELECT COUNT(*) FROM docs WHERE project_id=p.id) AS n_docs"
            " FROM projects p ORDER BY p.id DESC", (uid(),))]


@router.post("/projects")
def project_create(n: Name):
    name = n.name.strip()
    if not name:
        raise HTTPException(400, "Name required.")
    with closing(db()) as c:
        cur = c.execute("INSERT INTO projects(name,created) VALUES(?,?)", (name, now()))
        c.commit()
    log("project_created", name)
    return {"id": cur.lastrowid}


@router.put("/projects/{pid}")
def project_rename(pid: int, n: Name):
    with closing(db()) as c:
        c.execute("UPDATE projects SET name=? WHERE id=?", (n.name.strip(), pid))
        c.commit()
    return {"ok": True}


@router.delete("/projects/{pid}")
def project_delete(pid: int):
    """Admin only: deletes the project, everyone's chats in it, and its documents. Library untouched."""
    require_admin()
    with closing(db()) as c:
        row = c.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404)
        c.execute("DELETE FROM messages WHERE chat_id IN (SELECT id FROM chats WHERE project_id=?)", (pid,))
        c.execute("DELETE FROM chats WHERE project_id=?", (pid,))
        c.execute("DELETE FROM chunks WHERE doc_id IN (SELECT id FROM docs WHERE project_id=?)", (pid,))
        c.execute("DELETE FROM docs WHERE project_id=?", (pid,))
        c.execute("DELETE FROM assessments WHERE scope=?", (str(pid),))
        c.execute("DELETE FROM risks WHERE scope=?", (str(pid),))
        c.execute("DELETE FROM occurrences WHERE scope=?", (str(pid),))
        c.execute("DELETE FROM projects WHERE id=?", (pid,))
        c.commit()
    log("project_deleted", row["name"])
    return {"ok": True}


# ------------------------------------------------------------------------- chats
class NewChat(BaseModel):
    project_id: int | None = None


@router.get("/chats")
def chats_list(project_id: int | None = None):
    sql = ("SELECT c.id, c.project_id, c.title, c.updated, p.name AS project "
           "FROM chats c LEFT JOIN projects p ON p.id=c.project_id WHERE c.user_id=?")
    args = [uid()]
    if project_id is not None:
        sql += " AND c.project_id=?"
        args.append(project_id)
    with closing(db()) as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY c.updated DESC, c.id DESC LIMIT 100", args)]


@router.post("/chats")
def chat_create(n: NewChat):
    with closing(db()) as c:
        if n.project_id is not None and not c.execute(
                "SELECT 1 FROM projects WHERE id=?", (n.project_id,)).fetchone():
            raise HTTPException(404, "Project not found.")
        cur = c.execute("INSERT INTO chats(project_id,title,created,updated,user_id) VALUES(?,?,?,?,?)",
                        (n.project_id, "New chat", now(), now(), uid()))
        c.commit()
    return {"id": cur.lastrowid}


@router.get("/chats/{cid}")
def chat_get(cid: int):
    with closing(db()) as c:
        own_chat(c, cid)
        ch = c.execute("SELECT c.*, p.name AS project FROM chats c "
                       "LEFT JOIN projects p ON p.id=c.project_id WHERE c.id=?", (cid,)).fetchone()
        msgs = [dict(m) for m in c.execute("SELECT * FROM messages WHERE chat_id=? ORDER BY id", (cid,))]
    for m in msgs:
        m["sources"] = json.loads(m["sources"]) if m["sources"] else []
    return {**dict(ch), "messages": msgs}


@router.put("/chats/{cid}")
def chat_rename(cid: int, n: Name):
    with closing(db()) as c:
        own_chat(c, cid)
        c.execute("UPDATE chats SET title=? WHERE id=?", (n.name.strip() or "Untitled", cid))
        c.commit()
    return {"ok": True}


@router.delete("/chats/{cid}")
def chat_delete(cid: int):
    with closing(db()) as c:
        own_chat(c, cid)
        c.execute("DELETE FROM messages WHERE chat_id=?", (cid,))
        c.execute("DELETE FROM chats WHERE id=?", (cid,))
        c.commit()
    return {"ok": True}


class Ask(BaseModel):
    question: str


HISTORY_TURNS = 6  # most recent messages given to the model for follow-ups


@router.post("/chats/{cid}/ask")
def chat_ask(cid: int, a: Ask):
    q = a.question.strip()
    if not q:
        raise HTTPException(400, "Empty question.")
    with closing(db()) as c:
        ch = own_chat(c, cid)
        prior = [dict(m) for m in c.execute(
            "SELECT role, content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (cid, HISTORY_TURNS))][::-1]

    # A short follow-up ("and who approves it?") retrieves poorly alone, so add the last question.
    last_q = next((m["content"] for m in reversed(prior) if m["role"] == "user"), "")
    search = f"{last_q} {q}" if last_q and len(q.split()) < 8 else q
    hits = retrieve(search, k=5, project_scope=ch["project_id"])
    if not hits:
        raise HTTPException(400, "No documents available. Add some to the library or this project.")

    history = "\n".join(f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in prior)
    prompt = ((f"Conversation so far (for context only; answer from the sources):\n{history}\n\n"
               if history else "") + f"Sources:\n{format_sources(hits)}\n\nQuestion: {q}")
    answer = chat(ASK_SYSTEM, prompt).strip()

    with closing(db()) as c:
        c.execute("INSERT INTO messages(chat_id,role,content,sources,ts) VALUES(?,?,?,?,?)",
                  (cid, "user", q, None, now()))
        c.execute("INSERT INTO messages(chat_id,role,content,sources,ts) VALUES(?,?,?,?,?)",
                  (cid, "assistant", answer, json.dumps(hits), now()))
        title = q[:60] + ("…" if len(q) > 60 else "") if ch["title"] == "New chat" else ch["title"]
        c.execute("UPDATE chats SET title=?, updated=? WHERE id=?", (title, now(), cid))
        c.commit()
    log("question", f"[chat {cid}] {q}")
    return {"answer": answer, "sources": hits, "title": title}
