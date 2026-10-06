"""SMS Workspace - local AI workspace for aviation safety management audits.

Everything runs locally: Ollama for the LLM + embeddings, SQLite for storage.
"""
import contextvars
import io
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import httpx
import numpy as np
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from authcore import CURRENT_USER, DB_PATH, auth_dep, require_admin  # noqa: E402

BASE = Path(__file__).parent
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "qwen3:8b")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")

# ICAO Annex 19 / Doc 9859 SMS framework: 4 components, 12 elements.
REQUIREMENTS = [
    ("1.1", "Safety policy and objectives", "Management commitment",
     "A safety policy signed by the accountable executive, with safety objectives, commitment of resources and a statement of management commitment to safety."),
    ("1.2", "Safety policy and objectives", "Safety accountability and responsibilities",
     "Defined safety accountabilities and responsibilities for management and staff, including the accountable executive and who can make safety decisions."),
    ("1.3", "Safety policy and objectives", "Appointment of key safety personnel",
     "A safety manager or equivalent key safety personnel are appointed, with defined responsibilities for implementing and maintaining the SMS."),
    ("1.4", "Safety policy and objectives", "Coordination of emergency response planning",
     "An emergency response plan that defines roles, responsibilities and coordination with other organisations."),
    ("1.5", "Safety policy and objectives", "SMS documentation",
     "An SMS manual or documentation describing the safety policy, objectives, processes, and records of SMS outputs."),
    ("2.1", "Safety risk management", "Hazard identification",
     "A process for identifying hazards, using reactive and proactive sources such as occurrence reports, audits and voluntary reporting."),
    ("2.2", "Safety risk management", "Safety risk assessment and mitigation",
     "A process to assess the probability and severity of risk, a risk matrix or tolerability criteria, and controls to mitigate unacceptable risk."),
    ("3.1", "Safety assurance", "Safety performance monitoring and measurement",
     "Safety performance indicators and targets, a safety reporting system, investigation of occurrences, and internal audits to verify safety performance."),
    ("3.2", "Safety assurance", "Management of change",
     "A formal process to identify changes that may affect safety risk and to assess the risk before the change is implemented."),
    ("3.3", "Safety assurance", "Continuous improvement of the SMS",
     "Periodic review of SMS effectiveness, management review, and corrective action to improve the system."),
    ("4.1", "Safety promotion", "Training and education",
     "A safety training programme for all staff, matched to their roles, with records of training completed."),
    ("4.2", "Safety promotion", "Safety communication",
     "Ways of communicating safety information to staff, such as bulletins, briefings or newsletters, including why safety actions are taken."),
]
REQ_BY_ID = {r[0]: r for r in REQUIREMENTS}

app = FastAPI(title="SMS Workspace", dependencies=[Depends(auth_dep)])


# ----------------------------------------------------------------- database
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS docs(
                id INTEGER PRIMARY KEY, name TEXT, added TEXT, n_chunks INTEGER);
            CREATE TABLE IF NOT EXISTS chunks(
                id INTEGER PRIMARY KEY, doc_id INTEGER, idx INTEGER, page TEXT,
                text TEXT, emb BLOB);
            CREATE TABLE IF NOT EXISTS findings(
                id INTEGER PRIMARY KEY, title TEXT, description TEXT, requirement TEXT,
                severity TEXT, owner TEXT, due TEXT, status TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS assessments(
                scope TEXT, req_id TEXT, ai_status TEXT, rationale TEXT, quote TEXT,
                quote_verified INTEGER, source_doc TEXT, source_page TEXT,
                final_status TEXT, reviewer_note TEXT, assessed TEXT, model TEXT,
                reviewer TEXT, PRIMARY KEY (scope, req_id));
            CREATE TABLE IF NOT EXISTS audit_log(
                id INTEGER PRIMARY KEY, ts TEXT, action TEXT, detail TEXT, user TEXT);
            """
        )
        if "kind" not in [r["name"] for r in c.execute("PRAGMA table_info(docs)")]:
            c.execute("ALTER TABLE docs ADD COLUMN kind TEXT DEFAULT 'own'")
        if "project_id" not in [r["name"] for r in c.execute("PRAGMA table_info(docs)")]:
            c.execute("ALTER TABLE docs ADD COLUMN project_id INTEGER")
        if "user" not in [r["name"] for r in c.execute("PRAGMA table_info(audit_log)")]:
            c.execute("ALTER TABLE audit_log ADD COLUMN user TEXT")
        if "scope" not in [r["name"] for r in c.execute("PRAGMA table_info(assessments)")]:
            c.executescript(
                """
                ALTER TABLE assessments RENAME TO assessments_old;
                CREATE TABLE assessments(
                    scope TEXT, req_id TEXT, ai_status TEXT, rationale TEXT, quote TEXT,
                    quote_verified INTEGER, source_doc TEXT, source_page TEXT,
                    final_status TEXT, reviewer_note TEXT, assessed TEXT, model TEXT,
                    PRIMARY KEY (scope, req_id));
                INSERT INTO assessments SELECT 'lib', * FROM assessments_old;
                DROP TABLE assessments_old;
                """)
        if "reviewer" not in [r["name"] for r in c.execute("PRAGMA table_info(assessments)")]:
            c.execute("ALTER TABLE assessments ADD COLUMN reviewer TEXT")
        c.commit()


def log(action: str, detail: str = ""):
    with closing(db()) as c:
        u = CURRENT_USER.get()
        c.execute("INSERT INTO audit_log(ts,action,detail,user) VALUES(?,?,?,?)",
                  (time.strftime("%Y-%m-%d %H:%M:%S"), action, detail, u["username"] if u else None))
        c.commit()


init_db()


# ------------------------------------------------------------------ ollama
def embed(texts, kind="document"):
    prefix = "search_document: " if kind == "document" else "search_query: "
    try:
        r = httpx.post(f"{OLLAMA}/api/embed",
                       json={"model": EMBED_MODEL, "input": [prefix + t for t in texts]},
                       timeout=300)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(503, f"Ollama embedding failed ({e}). Is Ollama running and '{EMBED_MODEL}' pulled?")
    return np.array(r.json()["embeddings"], dtype=np.float32)


def chat(system: str, user: str, as_json=False) -> str:
    body = {
        "model": CHAT_MODEL, "stream": False, "think": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"temperature": 0.1, "num_ctx": 8192},
    }
    if as_json:
        body["format"] = "json"
    try:
        r = httpx.post(f"{OLLAMA}/api/chat", json=body, timeout=600)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(503, f"Ollama chat failed ({e}). Is Ollama running and '{CHAT_MODEL}' pulled?")
    return r.json()["message"]["content"]


# --------------------------------------------------------------- documents
def extract_pages(name: str, data: bytes):
    """Return a list of (page_label, text)."""
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return [(f"p.{i + 1}", p.extract_text() or "") for i, p in enumerate(reader.pages)]
    if ext == ".docx":
        from docx import Document
        d = Document(io.BytesIO(data))
        return [("", "\n".join(p.text for p in d.paragraphs))]
    return [("", data.decode("utf-8", errors="replace"))]


def chunk_text(text: str, size=900, overlap=150):
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 2 <= size:
            cur = f"{cur}\n\n{p}" if cur else p
            continue
        if cur:
            chunks.append(cur)
        while len(p) > size:  # very long paragraph
            chunks.append(p[:size])
            p = p[size - overlap:]
        cur = p
    if cur:
        chunks.append(cur)
    return chunks


@app.post("/api/docs")
async def upload_doc(file: UploadFile = File(...), kind: str = Form("own"),
                     project_id: int | None = Form(None)):
    if kind not in KIND_LABEL:
        raise HTTPException(400, "Invalid document type.")
    data = await file.read()
    pages = extract_pages(file.filename, data)
    items = [(label, c) for label, t in pages for c in chunk_text(t)]
    if not items:
        raise HTTPException(400, "No text could be extracted from this file.")
    vecs = embed([t for _, t in items])
    with closing(db()) as c:
        cur = c.execute("INSERT INTO docs(name,added,n_chunks,kind,project_id) VALUES(?,?,?,?,?)",
                        (file.filename, time.strftime("%Y-%m-%d %H:%M"), len(items), kind, project_id))
        doc_id = cur.lastrowid
        c.executemany(
            "INSERT INTO chunks(doc_id,idx,page,text,emb) VALUES(?,?,?,?,?)",
            [(doc_id, i, lbl, t, vecs[i].tobytes()) for i, (lbl, t) in enumerate(items)])
        c.commit()
    log("document_uploaded", f"{file.filename} ({len(items)} chunks)")
    return {"id": doc_id, "name": file.filename, "chunks": len(items)}


@app.get("/api/docs")
def list_docs():
    with closing(db()) as c:
        return [dict(r) for r in c.execute("SELECT * FROM docs ORDER BY id DESC")]


@app.delete("/api/docs/{doc_id}")
def delete_doc(doc_id: int):
    with closing(db()) as c:
        row = c.execute("SELECT name FROM docs WHERE id=?", (doc_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        c.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        c.execute("DELETE FROM docs WHERE id=?", (doc_id,))
        c.commit()
    log("document_deleted", row["name"])
    return {"ok": True}


KIND_LABEL = {"own": "Your document", "regulation": "Binding regulation", "amc": "AMC / guidance"}


def retrieve(query: str, k=5, kinds=None, doc_id=None, project_scope=False):
    """project_scope: False = every document; None = shared library only; int = library + that project."""
    sql = ("SELECT chunks.id, chunks.text, chunks.page, chunks.emb, docs.name, docs.kind "
           "FROM chunks JOIN docs ON docs.id=chunks.doc_id WHERE 1=1")
    args = []
    if kinds:
        sql += f" AND docs.kind IN ({','.join('?' * len(kinds))})"
        args += list(kinds)
    if doc_id:
        sql += " AND docs.id=?"
        args.append(doc_id)
    if project_scope is not False:
        if project_scope is None:
            sql += " AND docs.project_id IS NULL"
        else:
            sql += " AND (docs.project_id IS NULL OR docs.project_id=?)"
            args.append(project_scope)
    with closing(db()) as c:
        rows = c.execute(sql, args).fetchall()
    if not rows:
        return []
    mat = np.vstack([np.frombuffer(r["emb"], dtype=np.float32) for r in rows])
    q = embed([query], "query")[0]
    sims = mat @ q / (np.linalg.norm(mat, axis=1) * np.linalg.norm(q) + 1e-9)
    top = np.argsort(-sims)[:k]
    return [{"doc": rows[i]["name"], "page": rows[i]["page"], "text": rows[i]["text"],
             "kind": rows[i]["kind"], "score": float(sims[i])} for i in top]


def format_sources(hits):
    return "\n\n".join(
        f"[{i + 1}] ({KIND_LABEL.get(h['kind'], 'document')}: {h['doc']}{', ' + h['page'] if h['page'] else ''})\n{h['text']}"
        for i, h in enumerate(hits))


# --------------------------------------------------------------------- Q&A
class Question(BaseModel):
    question: str


ASK_SYSTEM = (
    "You are an assistant for aviation safety management (SMS) audits. Answer ONLY from the "
    "numbered sources provided. Cite sources inline like [1] or [2]. If the sources do not "
    "contain the answer, reply exactly: 'No evidence found in the uploaded documents.' "
    "Never use outside knowledge. Be concise."
)


@app.post("/api/ask")
def ask(q: Question):
    hits = retrieve(q.question)
    if not hits:
        raise HTTPException(400, "Upload documents first.")
    answer = chat(ASK_SYSTEM, f"Sources:\n{format_sources(hits)}\n\nQuestion: {q.question}")
    log("question", q.question)
    return {"answer": answer.strip(), "sources": hits}


# ------------------------------------------------------------ gap analysis
GAP_SYSTEM = (
    "You are an aviation safety management auditor assessing ONE SMS requirement against "
    "evidence excerpts from the organisation's documents. Judge only from the excerpts.\n"
    "Return JSON with keys:\n"
    '  "status": "met" | "partial" | "not_met" | "no_evidence"\n'
    '  "rationale": one or two sentences explaining the judgment\n'
    '  "source": the number of the excerpt that best supports it (integer, or null)\n'
    '  "quote": an EXACT verbatim sentence copied from that excerpt (empty string if none)\n'
    "Use 'met' only if the excerpts clearly satisfy the requirement, 'partial' only if the "
    "specific process or document named in the requirement is actually described but some "
    "elements are missing, 'not_met' if the excerpts show it is absent or inadequate, and "
    "'no_evidence' if the excerpts do not describe it. Related topics (e.g. general risk "
    "assessment) do NOT count as evidence for a different requirement (e.g. management of "
    "change). Be strict: when in doubt, choose the lower status."
)

VERIFY_SYSTEM = (
    "You are a strict second-pass auditor checking ONE aviation SMS requirement against "
    "evidence excerpts. Break the requirement into its 2-4 distinct required elements. For "
    "each element decide whether the excerpts EXPLICITLY state it. A related topic does not "
    "count (e.g. general risk assessment is not management of change; a one-hour induction "
    "is not a training programme with records). Return JSON:\n"
    '{"elements": [{"element": "short name", "present": true|false, '
    '"source": excerpt number or null, "quote": "EXACT verbatim sentence from that excerpt, '
    'or empty string"}]}'
)

ORDER = {"no_evidence": 0, "not_met": 1, "partial": 2, "met": 3}

job = {"running": False, "done": 0, "total": len(REQUIREMENTS), "current": "", "error": ""}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def find_quote(quote: str, hits, prefer=None):
    """Locate a model-supplied quote in the retrieved text.

    Returns (hit_index, actual_sentence) or None. Exact match (ignoring case/punctuation/
    whitespace) first; otherwise the closest source sentence if it shares >=80% of the words.
    The returned sentence always comes from the source, never from the model.
    """
    q = norm(quote or "")
    if len(q) < 12:
        return None
    order = ([prefer] if prefer is not None else []) + [i for i in range(len(hits)) if i != prefer]
    qt = set(q.split())
    best = None
    for i in order:
        text = hits[i]["text"]
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]
        for s in sents:
            ns = norm(s)
            if q in ns or ns in q and len(ns) > 20:
                return i, s
            overlap = len(qt & set(ns.split())) / len(qt)
            if overlap >= 0.8 and (best is None or overlap > best[0]):
                best = (overlap, i, s)
    return (best[1], best[2]) if best else None


def parse_json(raw: str, default):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            return json.loads(m.group(0)) if m else default
        except json.JSONDecodeError:
            return default


def skey(project_id):
    """Storage key for an analysis scope: the shared library, or one project."""
    return "lib" if project_id is None else str(project_id)


def assess_requirement(req, project_id=None):
    rid, comp, element, desc = req
    hits = retrieve(f"{element}. {desc}", k=4, kinds=("own",), project_scope=project_id)
    status, rationale, quote, doc, page, verified = "no_evidence", "No documents uploaded.", "", "", "", 0
    if hits:
        ask = f"Requirement {rid} - {element}: {desc}\n\nEvidence excerpts:\n{format_sources(hits)}"

        # Pass 1: holistic judgment.
        res = parse_json(chat(GAP_SYSTEM, ask, as_json=True), {})
        p1 = res.get("status") if res.get("status") in ORDER else "no_evidence"
        rationale = res.get("rationale", "")
        src = res.get("source")
        prefer = src - 1 if isinstance(src, int) and 1 <= src <= len(hits) else None

        # Pass 2: element-by-element check; every claimed element needs a real source quote.
        v = parse_json(chat(VERIFY_SYSTEM, ask, as_json=True), {})
        elems = v.get("elements") if isinstance(v.get("elements"), list) else []
        found, missing = [], []
        for e in elems:
            if not isinstance(e, dict):
                continue
            loc = find_quote(e.get("quote", ""), hits) if e.get("present") else None
            (found if loc else missing).append((e.get("element", "?"), loc))
        if not elems:
            p2 = "no_evidence"
        elif found and not missing:
            p2 = "met"
        elif found:
            p2 = "partial"
        else:
            p2 = "not_met" if p1 != "no_evidence" else "no_evidence"

        # Final status is the stricter of the two passes.
        status = min(p1, p2, key=ORDER.get)

        # Choose the displayed evidence: pass-1 quote if it checks out, else first verified element.
        loc = find_quote(res.get("quote", ""), hits, prefer) if res.get("quote") else None
        if not loc and found:
            loc = found[0][1]
        if loc:
            hit = hits[loc[0]]
            quote, doc, page, verified = loc[1], hit["doc"], hit["page"], 1
        if status in ("met", "partial") and not verified:
            status = "no_evidence"
        note = f" [Verification: {len(found)}/{len(found) + len(missing)} required elements found"
        if missing:
            note += "; missing: " + ", ".join(m[0] for m in missing)
        note += f". First pass said '{p1}'.]"
        rationale = (rationale + note).strip()
    with closing(db()) as c:
        c.execute(
            "INSERT OR REPLACE INTO assessments(scope,req_id,ai_status,rationale,quote,quote_verified,"
            "source_doc,source_page,final_status,reviewer_note,assessed,model) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (skey(project_id), rid, status, rationale, quote, verified, doc, page,
             None, None, time.strftime("%Y-%m-%d %H:%M"), CHAT_MODEL))
        c.commit()


def run_gap_job(project_id=None):
    try:
        for i, req in enumerate(REQUIREMENTS):
            job["current"] = f"{req[0]} {req[2]}"
            assess_requirement(req, project_id)
            job["done"] = i + 1
        log("gap_analysis_run", f"scope={skey(project_id)} model={CHAT_MODEL}")
    except Exception as e:  # surfaced to the UI
        job["error"] = getattr(e, "detail", str(e))
    finally:
        job["running"] = False


@app.post("/api/gap/run")
def gap_run(project_id: int | None = None):
    if job["running"]:
        raise HTTPException(409, "Analysis already running.")
    job.update(running=True, done=0, current="", error="", scope=skey(project_id))
    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(run_gap_job, project_id), daemon=True).start()
    return {"started": True}


@app.get("/api/gap")
def gap_state(project_id: int | None = None):
    with closing(db()) as c:
        saved = {r["req_id"]: dict(r) for r in c.execute(
            "SELECT * FROM assessments WHERE scope=?", (skey(project_id),))}
    reqs = []
    for rid, comp, element, desc in REQUIREMENTS:
        reqs.append({"id": rid, "component": comp, "element": element, "description": desc,
                     "assessment": saved.get(rid)})
    return {"job": job, "requirements": reqs}


class Review(BaseModel):
    final_status: str
    reviewer_note: str = ""


@app.put("/api/gap/{req_id}")
def gap_review(req_id: str, r: Review, project_id: int | None = None):
    if r.final_status not in ("met", "partial", "not_met", "no_evidence"):
        raise HTTPException(400, "Invalid status")
    with closing(db()) as c:
        who = (CURRENT_USER.get() or {}).get("username")
        cur = c.execute("UPDATE assessments SET final_status=?, reviewer_note=?, reviewer=? WHERE req_id=? AND scope=?",
                        (r.final_status, r.reviewer_note, who, req_id, skey(project_id)))
        if not cur.rowcount:
            raise HTTPException(404, "Run the analysis first.")
        c.commit()
    log("assessment_confirmed", f"{req_id} -> {r.final_status} (scope {skey(project_id)})")
    return {"ok": True}


# ---------------------------------------------------------------- findings
class Finding(BaseModel):
    title: str
    description: str = ""
    requirement: str = ""
    severity: str = "Medium"
    owner: str = ""
    due: str = ""
    status: str = "Open"


@app.get("/api/findings")
def list_findings():
    with closing(db()) as c:
        return [dict(r) for r in c.execute("SELECT * FROM findings ORDER BY id DESC")]


@app.post("/api/findings")
def add_finding(f: Finding):
    with closing(db()) as c:
        cur = c.execute(
            "INSERT INTO findings(title,description,requirement,severity,owner,due,status,created) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (f.title, f.description, f.requirement, f.severity, f.owner, f.due, f.status,
             time.strftime("%Y-%m-%d")))
        c.commit()
    log("finding_created", f.title)
    return {"id": cur.lastrowid}


@app.put("/api/findings/{fid}")
def update_finding(fid: int, f: Finding):
    with closing(db()) as c:
        c.execute(
            "UPDATE findings SET title=?,description=?,requirement=?,severity=?,owner=?,due=?,status=? "
            "WHERE id=?",
            (f.title, f.description, f.requirement, f.severity, f.owner, f.due, f.status, fid))
        c.commit()
    log("finding_updated", f"#{fid} {f.title} [{f.status}]")
    return {"ok": True}


@app.delete("/api/findings/{fid}")
def delete_finding(fid: int):
    with closing(db()) as c:
        c.execute("DELETE FROM findings WHERE id=?", (fid,))
        c.commit()
    log("finding_deleted", f"#{fid}")
    return {"ok": True}


class DraftReq(BaseModel):
    req_id: str
    project_id: int | None = None


@app.post("/api/findings/draft")
def draft_finding(d: DraftReq):
    """Draft a finding from a gap assessment. A person still reviews and saves it."""
    req = REQ_BY_ID.get(d.req_id)
    with closing(db()) as c:
        a = c.execute("SELECT * FROM assessments WHERE req_id=? AND scope=?",
                      (d.req_id, skey(d.project_id))).fetchone()
    if not req or not a:
        raise HTTPException(404, "Run the analysis first.")
    text = chat(
        "You write concise audit findings for aviation safety management systems. Return JSON "
        'with keys "title" (max 12 words) and "description" (2-3 sentences: what was expected, '
        "what was observed, and the gap). Use only the facts provided.",
        f"Requirement {req[0]} - {req[2]}: {req[3]}\nAssessment: {a['ai_status']}\n"
        f"Rationale: {a['rationale']}\nEvidence quote: {a['quote']}", as_json=True)
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        out = {"title": f"{req[2]} - gap", "description": a["rationale"]}
    return {"title": out.get("title", ""), "description": out.get("description", ""),
            "requirement": f"{req[0]} {req[2]}"}


# ------------------------------------------------------------ report & log
@app.get("/api/report", response_class=PlainTextResponse)
def report(project_id: int | None = None):
    label = {"met": "Met", "partial": "Partially met", "not_met": "Not met",
             "no_evidence": "No evidence"}
    with closing(db()) as c:
        saved = {r["req_id"]: r for r in c.execute(
            "SELECT * FROM assessments WHERE scope=?", (skey(project_id),))}
        findings = c.execute("SELECT * FROM findings ORDER BY id").fetchall()
        docs = c.execute(
            "SELECT name FROM docs WHERE kind='own' AND (project_id IS NULL OR project_id=?)",
            (project_id,)).fetchall()
        pname = c.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
    out = ["# SMS Audit Report", f"Scope: {pname['name'] + ' (project + library)' if pname else 'shared library'}",
           f"Generated {time.strftime('%Y-%m-%d %H:%M')}",
           "", "Draft prepared with AI assistance (local model). Statuses marked 'AI' are "
           "unreviewed suggestions.", "", "## Documents reviewed", ""]
    out += [f"- {d['name']}" for d in docs] or ["- (none)"]
    out += ["", "## Requirement assessment", ""]
    for rid, comp, element, _ in REQUIREMENTS:
        a = saved.get(rid)
        if not a:
            out.append(f"### {rid} {element}\nNot yet assessed.\n")
            continue
        final = a["final_status"] or a["ai_status"]
        by = f"confirmed by {a['reviewer'] or 'reviewer'}" if a["final_status"] else "AI, unreviewed"
        out.append(f"### {rid} {element}\n**{label[final]}** ({by})  \n{a['rationale']}")
        if a["quote"]:
            src = f"{a['source_doc']} {a['source_page']}".strip()
            out.append(f"> {a['quote']}  \n> - {src}" + ("" if a["quote_verified"] else " (unverified quote)"))
        if a["reviewer_note"]:
            out.append(f"Reviewer note: {a['reviewer_note']}")
        out.append("")
    from features import report_sections  # imported here: features imports main
    out += [""] + report_sections(project_id) + [""]
    out += ["## Findings", ""]
    for f in findings:
        out.append(f"- **{f['title']}** [{f['severity']}, {f['status']}] "
                   f"{f['requirement']} - owner: {f['owner'] or 'unassigned'}, due: {f['due'] or 'n/a'}")
        if f["description"]:
            out.append(f"  {f['description']}")
    if not findings:
        out.append("- (none)")
    return "\n".join(out)


@app.get("/api/report.docx")
def report_docx(project_id: int | None = None):
    from docx_export import md_to_docx
    log("report_exported", f"docx, scope {skey(project_id)}")
    return Response(
        md_to_docx(report(project_id)),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": 'attachment; filename="sms-audit-report.docx"'})


@app.get("/api/network")
def network_info():
    """Admin only: whether colleagues can connect, and the addresses they would use."""
    require_admin()
    shared = os.environ.get("SMS_SHARE") == "1"
    port = os.environ.get("SMS_PORT") or "8000"
    urls = []
    if shared:
        import socket
        try:
            ips = {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
        except OSError:
            ips = set()
        urls = [f"http://{ip}:{port}" for ip in sorted(ips) if not ip.startswith("127.")]
    return {"shared": shared, "urls": urls}


@app.get("/api/log")
def get_log():
    with closing(db()) as c:
        return [dict(r) for r in c.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200")]


@app.get("/api/status")
def status():
    try:
        tags = httpx.get(f"{OLLAMA}/api/tags", timeout=3).json()["models"]
        names = [m["name"] for m in tags]
        ok = True
    except Exception:
        names, ok = [], False
    return {"ollama": ok, "chat_model": CHAT_MODEL, "embed_model": EMBED_MODEL,
            "chat_ready": any(n.startswith(CHAT_MODEL) for n in names),
            "embed_ready": any(n.startswith(EMBED_MODEL) for n in names)}


from chats import router as chats_router  # noqa: E402  (these need the helpers above)
from features import router  # noqa: E402
from auth import router as auth_router  # noqa: E402

app.include_router(router)
app.include_router(chats_router)
app.include_router(auth_router)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")
