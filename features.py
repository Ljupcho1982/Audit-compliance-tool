"""Extra workspace features: manual-vs-requirement checks, occurrence analysis, risk drafting.

Everything here produces DRAFTS. A person confirms or decides.
"""
import csv
import io
import json
import time
from collections import Counter
from contextlib import closing
from datetime import datetime

import numpy as np
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from authcore import current_user
from main import chat, db, embed, format_sources, log, parse_json, retrieve, skey

router = APIRouter(prefix="/api")

with closing(db()) as _c:
    _c.executescript(
        """
        CREATE TABLE IF NOT EXISTS occurrences(
            id INTEGER PRIMARY KEY, date TEXT, category TEXT, text TEXT, emb BLOB);
        CREATE TABLE IF NOT EXISTS risks(
            id INTEGER PRIMARY KEY, activity TEXT, hazard TEXT, consequence TEXT,
            severity INTEGER, probability INTEGER, rationale TEXT, status TEXT,
            note TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        """
    )
    if "scope" not in [r["name"] for r in _c.execute("PRAGMA table_info(occurrences)")]:
        _c.execute("ALTER TABLE occurrences ADD COLUMN scope TEXT DEFAULT 'lib'")
    if "scope" not in [r["name"] for r in _c.execute("PRAGMA table_info(risks)")]:
        _c.execute("ALTER TABLE risks ADD COLUMN scope TEXT DEFAULT 'lib'")
    if "decided_by" not in [r["name"] for r in _c.execute("PRAGMA table_info(risks)")]:
        _c.execute("ALTER TABLE risks ADD COLUMN decided_by TEXT")
    _c.commit()


# ------------------------------------------------- check a manual against a rule
CHECK_SYSTEM = (
    "You compare ONE section of an operator's own document against a requirement. Use ONLY "
    "the numbered sources. Sources labelled 'Your document' are what the operator wrote; "
    "'Binding regulation' and 'AMC / guidance' are the rules.\n"
    "Return JSON with keys:\n"
    '  "verdict": "aligned" | "drifted" | "gap"\n'
    '  "summary": 2-4 sentences, citing sources inline like [1] [2]. Say what the operator\'s '
    "document says, what the requirement asks, and what is missing or differs.\n"
    "Definitions: aligned = the operator's text covers what the requirement asks. drifted = it "
    "covers the topic but is narrower, vaguer, or leaves out how it is done. gap = the "
    "operator's documents do not address it. If no operator text is relevant, use 'gap'. "
    "Never rely on outside knowledge of the regulation."
)


class Check(BaseModel):
    requirement: str
    doc_id: int | None = None
    project_id: int | None = None  # None = shared library only


@router.post("/check")
def check(c: Check):
    own = retrieve(c.requirement, k=3, kinds=("own",), doc_id=c.doc_id, project_scope=c.project_id)
    if not own:
        raise HTTPException(400, "Upload your manual first (type: Your document).")
    rules = retrieve(c.requirement, k=3, kinds=("regulation", "amc"), project_scope=c.project_id)
    hits = own + rules
    ask = (f"Requirement as stated by the user:\n{c.requirement}\n\n"
           f"Sources:\n{format_sources(hits)}")
    res = parse_json(chat(CHECK_SYSTEM, ask, as_json=True), {})
    verdict = res.get("verdict") if res.get("verdict") in ("aligned", "drifted", "gap") else "gap"
    log("manual_check", f"{c.requirement[:80]} -> {verdict}")
    return {"verdict": verdict, "summary": res.get("summary", ""), "sources": hits,
            "has_rules": bool(rules)}


# -------------------------------------------------------------------- occurrences
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d", "%d-%m-%Y")


def parse_date(s: str):
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s.strip(), f).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def find_col(headers, *needles):
    for h in headers:
        if any(n in h.lower() for n in needles):
            return h
    return None


def occ_scope(project_id):
    """SQL filter for the reports a scope can see: the library, plus the project's own."""
    if project_id is None:
        return "scope='lib'", []
    return "(scope='lib' OR scope=?)", [str(project_id)]


@router.post("/occ/import")
async def occ_import(file: UploadFile = File(...), project_id: int | None = None):
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    heads = reader.fieldnames or []
    dcol = find_col(heads, "date")
    tcol = find_col(heads, "desc", "narrative", "summary", "text", "report")
    ccol = find_col(heads, "categ", "type")
    if not dcol or not tcol:
        raise HTTPException(400, f"CSV needs a date column and a description column. Found: {heads}")
    rows, skipped = [], 0
    for r in reader:
        d, t = parse_date(r.get(dcol) or ""), (r.get(tcol) or "").strip()
        if d and t:
            rows.append((d, (r.get(ccol) or "").strip() if ccol else "", t))
        else:
            skipped += 1
    if not rows:
        raise HTTPException(400, "No usable rows (check the date format, e.g. 2026-03-14).")
    vecs = np.vstack([embed([t for _, _, t in rows[i:i + 32]]) for i in range(0, len(rows), 32)])
    with closing(db()) as c:
        c.executemany("INSERT INTO occurrences(date,category,text,emb,scope) VALUES(?,?,?,?,?)",
                      [(d, cat, t, vecs[i].tobytes(), skey(project_id)) for i, (d, cat, t) in enumerate(rows)])
        c.commit()
    log("occurrences_imported", f"{file.filename}: {len(rows)} rows, {skipped} skipped (scope {skey(project_id)})")
    return {"imported": len(rows), "skipped": skipped}


@router.delete("/occ")
def occ_clear(project_id: int | None = None):
    """Deletes only the reports imported into this scope (never the library's from a project)."""
    with closing(db()) as c:
        c.execute("DELETE FROM occurrences WHERE scope=?", (skey(project_id),))
        c.commit()
    log("occurrences_cleared", f"scope {skey(project_id)}")
    return {"ok": True}


@router.get("/occ/summary")
def occ_summary(project_id: int | None = None):
    where, args = occ_scope(project_id)
    with closing(db()) as c:
        rows = c.execute(f"SELECT date, category, scope FROM occurrences WHERE {where}", args).fetchall()
    mine = sum(1 for r in rows if r["scope"] == skey(project_id))
    if not rows:
        return {"total": 0, "all_time": 0, "mine": 0, "months": [], "categories": []}
    last = max(r["date"] for r in rows)[:7]
    y, m = int(last[:4]), int(last[5:7])
    months = []
    for _ in range(12):
        months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    months.reverse()
    per = Counter(r["date"][:7] for r in rows)
    cats = Counter(r["category"] or "Uncategorised" for r in rows if r["date"][:7] in months)
    return {"total": sum(per[mm] for mm in months), "all_time": len(rows), "mine": mine,
            "months": [{"m": mm, "n": per.get(mm, 0)} for mm in months],
            "categories": cats.most_common(8)}


class Similar(BaseModel):
    text: str
    project_id: int | None = None


@router.post("/occ/similar")
def occ_similar(s: Similar):
    with closing(db()) as c:
        where, args = occ_scope(s.project_id)
        rows = c.execute(f"SELECT date, category, text, emb FROM occurrences WHERE {where}", args).fetchall()
    if not rows:
        raise HTTPException(400, "Import occurrence reports first.")
    mat = np.vstack([np.frombuffer(r["emb"], dtype=np.float32) for r in rows])
    q = embed([s.text], "query")[0]
    sims = mat @ q / (np.linalg.norm(mat, axis=1) * np.linalg.norm(q) + 1e-9)
    top = np.argsort(-sims)[:6]
    return [{"date": rows[i]["date"], "category": rows[i]["category"], "text": rows[i]["text"],
             "score": float(sims[i])} for i in top]


class Pack(BaseModel):
    month: str  # YYYY-MM
    project_id: int | None = None


PACK_SYSTEM = (
    "You draft a monthly safety pack for an aviation safety meeting from occurrence report data. "
    "Use ONLY the data given; do not invent causes, numbers or events. These are counts of "
    "reports, not of events. Write in Markdown with these sections: ## Summary, ## Reports of "
    "note, ## Compared with last month, ## Questions for the meeting. Be concise and factual. "
    "Mention report dates when referring to a specific report."
)


@router.post("/occ/pack")
def occ_pack(p: Pack):
    y, m = int(p.month[:4]), int(p.month[5:7])
    prev = f"{y - 1:04d}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"
    where, wargs = occ_scope(p.project_id)
    with closing(db()) as c:
        cur = c.execute(f"SELECT date, category, text FROM occurrences WHERE {where} AND date LIKE ? ORDER BY date",
                        wargs + [p.month + "%"]).fetchall()
        n_prev = c.execute(f"SELECT COUNT(*) FROM occurrences WHERE {where} AND date LIKE ?",
                           wargs + [prev + "%"]).fetchone()[0]
        prev_cats = Counter(r["category"] or "Uncategorised" for r in c.execute(
            f"SELECT category FROM occurrences WHERE {where} AND date LIKE ?", wargs + [prev + "%"]))
    if not cur:
        raise HTTPException(404, "No reports in that month.")
    cats = Counter(r["category"] or "Uncategorised" for r in cur)
    lines = "\n".join(f"- {r['date']} [{r['category'] or 'n/a'}] {r['text'][:300]}" for r in cur[:40])
    ask = (f"Month: {p.month}\nReports this month: {len(cur)}\nReports last month ({prev}): {n_prev}\n"
           f"By category this month: {dict(cats)}\nBy category last month: {dict(prev_cats)}\n\n"
           f"Reports (first 40 shown):\n{lines}")
    log("safety_pack_drafted", p.month)
    return {"month": p.month, "draft": chat(PACK_SYSTEM, ask).strip()}


# ------------------------------------------------------------------ risk matrix
DEFAULT_MATRIX = {
    "severity": ["Negligible", "Minor", "Major", "Hazardous", "Catastrophic"],
    "probability": ["Extremely improbable", "Improbable", "Remote", "Occasional", "Frequent"],
    "intolerable_from": 15,
    "acceptable_below": 5,
}


def get_matrix():
    with closing(db()) as c:
        row = c.execute("SELECT value FROM settings WHERE key='matrix'").fetchone()
    return json.loads(row["value"]) if row else DEFAULT_MATRIX


def rate(sev: int, prob: int, mx=None):
    mx = mx or get_matrix()
    score = sev * prob
    if score >= mx["intolerable_from"]:
        return score, "Intolerable"
    if score < mx["acceptable_below"]:
        return score, "Acceptable"
    return score, "Tolerable - mitigate"


class Matrix(BaseModel):
    severity: list[str]
    probability: list[str]
    intolerable_from: int
    acceptable_below: int


@router.get("/matrix")
def matrix_get():
    return get_matrix()


@router.put("/matrix")
def matrix_put(m: Matrix):
    if len(m.severity) != 5 or len(m.probability) != 5 or any(not x.strip() for x in m.severity + m.probability):
        raise HTTPException(400, "Provide exactly 5 non-empty labels for severity and probability.")
    if not 1 <= m.acceptable_below <= m.intolerable_from <= 25:
        raise HTTPException(400, "Thresholds must satisfy 1 <= acceptable_below <= intolerable_from <= 25.")
    with closing(db()) as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES('matrix',?)", (m.model_dump_json(),))
        c.commit()
    log("risk_matrix_changed")
    return {"ok": True}


RISK_SYSTEM = (
    "You draft aviation safety risk assessments. For the activity given, propose 2-4 distinct "
    "hazards. Use the operator's own risk matrix: severity and probability are integers 1-5 "
    "where the labels are listed lowest to highest below. Base probability on the occurrence "
    "history and documents provided where relevant; if there is no relevant history say so in "
    "the rationale and choose conservatively. Return JSON: "
    '{"risks": [{"hazard": str, "consequence": str, "severity": 1-5, "probability": 1-5, '
    '"rationale": "one or two sentences"}]}. These are proposals only.'
)


class Draft(BaseModel):
    activity: str
    project_id: int | None = None  # None = shared library only


@router.post("/risk/draft")
def risk_draft(d: Draft):
    mx = get_matrix()
    ctx = []
    own = retrieve(d.activity, k=3, kinds=("own",), project_scope=d.project_id)
    if own:
        ctx.append("Operator documents:\n" + format_sources(own))
    with closing(db()) as c:
        where, wargs = occ_scope(d.project_id)
        occ = c.execute(f"SELECT date, category, text, emb FROM occurrences WHERE {where}", wargs).fetchall()
    if occ:
        mat = np.vstack([np.frombuffer(r["emb"], dtype=np.float32) for r in occ])
        q = embed([d.activity], "query")[0]
        sims = mat @ q / (np.linalg.norm(mat, axis=1) * np.linalg.norm(q) + 1e-9)
        ctx.append("Possibly related past occurrence reports:\n" + "\n".join(
            f"- {occ[i]['date']} [{occ[i]['category']}] {occ[i]['text'][:250]}"
            for i in np.argsort(-sims)[:5]))
    scale = (f"Severity labels (1-5): {mx['severity']}\nProbability labels (1-5): {mx['probability']}")
    res = parse_json(chat(RISK_SYSTEM, f"Activity: {d.activity}\n\n{scale}\n\n" + "\n\n".join(ctx),
                          as_json=True), {})
    out = []
    with closing(db()) as c:
        for r in (res.get("risks") or [])[:4]:
            try:
                sev = min(5, max(1, int(r.get("severity"))))
                prob = min(5, max(1, int(r.get("probability"))))
            except (TypeError, ValueError):
                continue
            cur = c.execute(
                "INSERT INTO risks(activity,hazard,consequence,severity,probability,rationale,"
                "status,note,created,scope) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (d.activity, str(r.get("hazard", "")), str(r.get("consequence", "")), sev, prob,
                 str(r.get("rationale", "")), "Proposed", "", time.strftime("%Y-%m-%d"), skey(d.project_id)))
            out.append(cur.lastrowid)
        c.commit()
    if not out:
        raise HTTPException(502, "The model returned no usable risks. Try again or rephrase the activity.")
    log("risk_drafted", f"{d.activity} ({len(out)} proposed)")
    return {"ids": out}


@router.get("/risks")
def risks_list(project_id: int | None = None):
    mx = get_matrix()
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM risks WHERE scope=? ORDER BY id DESC", (skey(project_id),))]
    for r in rows:
        r["score"], r["rating"] = rate(r["severity"], r["probability"], mx)
        r["severity_label"] = mx["severity"][r["severity"] - 1]
        r["probability_label"] = mx["probability"][r["probability"] - 1]
    return rows


class Decision(BaseModel):
    severity: int
    probability: int
    note: str = ""


@router.put("/risks/{rid}")
def risk_decide(rid: int, d: Decision):
    if not (1 <= d.severity <= 5 and 1 <= d.probability <= 5):
        raise HTTPException(400, "Severity and probability must be 1-5.")
    with closing(db()) as c:
        cur = c.execute("UPDATE risks SET severity=?, probability=?, note=?, status='Decided', decided_by=? WHERE id=?",
                        (d.severity, d.probability, d.note, (current_user() or {}).get("username"), rid))
        if not cur.rowcount:
            raise HTTPException(404)
        c.commit()
    log("risk_decided", f"#{rid} -> {d.severity}x{d.probability}")
    return {"ok": True}


@router.delete("/risks/{rid}")
def risk_delete(rid: int):
    with closing(db()) as c:
        c.execute("DELETE FROM risks WHERE id=?", (rid,))
        c.commit()
    log("risk_deleted", f"#{rid}")
    return {"ok": True}


# ------------------------------------------------------------- audit report sections
def report_sections(project_id=None):
    """Markdown for the audit report: risk assessments and occurrence data in the given scope."""
    mx = get_matrix()
    out = ["## Risk assessments", ""]
    risks = sorted(risks_list(project_id), key=lambda r: -r["score"])
    out.append(f"Scored as severity x probability on the organisation's matrix: intolerable from "
               f"{mx['intolerable_from']}, acceptable below {mx['acceptable_below']}. "
               "'Proposed' ratings are unreviewed AI drafts; 'Decided' ratings were recorded by a person.")
    out.append("")
    for r in risks:
        who = (f"decided, recorded by {r['decided_by']}" if r["decided_by"] else "decided") \
            if r["status"] == "Decided" else "PROPOSED, AI draft, not yet decided"
        out.append(f"- **{r['hazard']}** - {r['rating']} ({r['severity_label']} x {r['probability_label']}"
                   f" = {r['score']}; {who})")
        out.append(f"  Activity: {r['activity']}. Consequence: {r['consequence']}")
        if r["rationale"] and r["status"] != "Decided":
            out.append(f"  AI rationale: {r['rationale']}")
        if r["note"]:
            out.append(f"  Decision note: {r['note']}")
    if not risks:
        out.append("- (none)")

    out += ["", "## Occurrence reports", ""]
    s = occ_summary(project_id)
    if not s["all_time"]:
        out.append("- (no occurrence reports imported)")
        return out
    first, last = s["months"][0]["m"], s["months"][-1]["m"]
    out.append(f"{s['total']} reports from {first} to {last} ({s['all_time']} in scope in total). "
               "These are counts of reports, not of events.")
    out += ["", "| Month | Reports |", "|---|---|"]
    out += [f"| {m['m']} | {m['n']} |" for m in s["months"]]
    out += ["", "By category: " + ", ".join(f"{c} {n}" for c, n in s["categories"]), ""]
    return out
