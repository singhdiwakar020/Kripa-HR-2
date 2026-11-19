import io
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Body, Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from openpyxl import Workbook
from sqlalchemy.orm import Session

from . import calls, importer, search
from . import db as dbmod
from .calls_api import router as calls_router
from .plivo_calls import router as plivo_router
from .exam_api import public as exam_public, page_router as exam_page_router, hr as exam_hr
from .llm import LLMUnavailable
from .db import Base, engine, get_db
from .models import AuditLog, BlockedContact, CallJob, CallRecord, Candidate, ExamInvite, ImportBatch, SavedSearch

Base.metadata.create_all(engine)


def _add_missing_columns():
    """create_all never alters existing tables, so columns added later are added here (safe to run every start)."""
    from sqlalchemy import text
    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(question_sets)"))}
        if "about" not in cols:
            conn.execute(text("ALTER TABLE question_sets ADD COLUMN about TEXT DEFAULT ''"))
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(candidates)"))}
        if "candidate_ref" not in cols:
            conn.execute(text("ALTER TABLE candidates ADD COLUMN candidate_ref VARCHAR(100)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_candidates_candidate_ref ON candidates (candidate_ref)"))


_add_missing_columns()


@asynccontextmanager
async def lifespan(_app):
    s = dbmod.SessionLocal()
    try:
        calls.seed_defaults(s)  # a starter question set so HR can try calls straight away
    finally:
        s.close()
    calls.start_scheduler()  # places due calls, retries and recalls
    yield


app = FastAPI(title="Kripa HR - Stage 1", lifespan=lifespan)
app.include_router(calls_router)
app.include_router(plivo_router)  # webhooks Plivo calls during a live call
app.include_router(exam_public)  # the candidates' own test endpoints (login, answers, submit)
app.include_router(exam_page_router)  # the candidates' test page at /exam
app.include_router(exam_hr)  # HR: build tests, invite, results (local only)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# The public (ngrok) address must serve ONLY Plivo's webhooks. Everything else (candidate data, starting calls,
# uploads) is available only when the browser reaches the app on this computer. To use another address on purpose,
# list it in ALLOWED_HOSTS in backend/.env, separated by commas.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "testserver"}


@app.middleware("http")
async def local_only(request: Request, call_next):
    path = request.url.path
    # public on purpose: Plivo's webhooks (signature-checked) and the candidates' test page and endpoints (login-checked).
    # /api/exams/* (with an s) is the HR side and stays local-only.
    if path.startswith("/api/plivo/") or path == "/exam" or path.startswith("/api/exam/"):
        return await call_next(request)
    extra = {h.strip().lower() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()}
    if (request.url.hostname or "").lower() not in LOCAL_HOSTS | extra:
        return JSONResponse({"detail": "Not available on this address."}, status_code=403)
    return await call_next(request)

MAX_UPLOAD = 15 * 1024 * 1024


def audit(db: Session, action: str, record: str = "", detail: str = ""):
    db.add(AuditLog(action=action, record=record, detail=detail))


# ------------------------------------------------------------------ upload
@app.post("/api/imports")
async def upload(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    if len(content) > MAX_UPLOAD:
        raise HTTPException(413, "File is larger than 15 MB.")
    try:
        headers, rows = importer.read_file(file.filename or "upload", content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(400, "Could not read this file. Check that it is a valid .xlsx or .csv.")
    batch = ImportBatch(file_name=file.filename, headers=headers, raw_rows=rows, mapping=importer.auto_map(headers))
    db.add(batch)
    db.commit()
    result = importer.analyse(rows, batch.mapping, db)
    return importer.preview_payload(batch, result)


@app.post("/api/imports/{batch_id}/preview")
def re_preview(batch_id: int, mapping: dict = Body(...), db: Session = Depends(get_db)):
    batch = _batch(db, batch_id)
    batch.mapping = {h: (mapping.get(h) or None) for h in batch.headers}
    db.commit()
    return importer.preview_payload(batch, importer.analyse(batch.raw_rows, batch.mapping, db))


@app.post("/api/imports/{batch_id}/confirm")
def confirm(batch_id: int, db: Session = Depends(get_db)):
    batch = _batch(db, batch_id)
    if batch.status == "confirmed":
        raise HTTPException(400, "This upload was already confirmed.")
    if not any(f == "name" for f in batch.mapping.values()):
        raise HTTPException(400, "Map a column to Name before confirming.")
    result = importer.analyse(batch.raw_rows, batch.mapping, db)
    summary = importer.apply(batch, result, db)
    audit(db, "import_confirmed", f"batch {batch.id}", f"{batch.file_name}: {summary}")
    db.commit()
    return summary


@app.post("/api/reset")
def reset_data(confirm: str = Body(..., embed=True), db: Session = Depends(get_db)):
    """Clear candidates, uploads, call history and saved searches. Keeps question sets and the do-not-contact blocklist.
    A copy of the database is saved first in backend/backups/."""
    if confirm != "DELETE":
        raise HTTPException(400, 'Type DELETE to confirm.')
    import sqlite3
    from datetime import datetime
    backups = dbmod.DB_PATH.parent / "backups"
    backups.mkdir(exist_ok=True)
    target = backups / f"kripa-before-clear-{datetime.now():%Y%m%d-%H%M%S}.db"
    src, dst = sqlite3.connect(dbmod.DB_PATH), sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    stopped = calls.cancel_all(db)  # nothing may ring while the data is being removed
    db.flush()
    known = {(b.email, b.phone) for b in db.query(BlockedContact)}
    kept = 0
    for c in db.query(Candidate).filter(Candidate.do_not_contact.is_(True)):
        if (c.email, c.phone) not in known:
            db.add(BlockedContact(email=c.email, phone=c.phone, name=c.name))
            kept += 1
    removed = db.query(Candidate).count()
    for model in (ExamInvite, CallRecord, CallJob, Candidate, ImportBatch, SavedSearch):  # test logins go with the candidates; tests themselves stay
        db.query(model).delete()
    audit(db, "data_cleared", f"{removed} candidates", f"backup {target.name}; {kept} do-not-contact kept; {stopped} calls stopped")
    db.commit()
    return {"removed": removed, "calls_stopped": stopped, "do_not_contact_kept": kept, "backup": target.name}


@app.get("/api/imports")
def list_imports(db: Session = Depends(get_db)):
    rows = db.query(ImportBatch).filter(ImportBatch.status == "confirmed").order_by(ImportBatch.id.desc()).all()
    return [{"id": b.id, "file_name": b.file_name, "uploaded_at": b.uploaded_at.isoformat(),
             "rows_added": b.rows_added, "rows_updated": b.rows_updated, "rows_skipped": b.rows_skipped,
             "rows_need_fix": b.rows_need_fix, "rows_merged": b.rows_merged} for b in rows]


def _batch(db: Session, batch_id: int) -> ImportBatch:
    b = db.get(ImportBatch, batch_id)
    if not b:
        raise HTTPException(404, "Upload not found.")
    return b


# ------------------------------------------------------------------ search
@app.get("/api/facets")
def get_facets(db: Session = Depends(get_db)):
    return search.facets(db)


@app.get("/api/search")
def do_search(
    q: str = "", role: Optional[str] = None, min_score: Optional[float] = None, max_score: Optional[float] = None,
    status: Optional[str] = None, location: Optional[str] = None,
    min_experience: Optional[float] = None, max_experience: Optional[float] = None,
    max_notice_days: Optional[int] = None, max_expectation: Optional[float] = None,
    ready_to_relocate: Optional[bool] = None, include_relocators: Optional[bool] = None,
    keywords: Optional[str] = None, callable_only: Optional[bool] = None,
    name: Optional[str] = None, text: Optional[str] = None, role_match: Optional[str] = None,
    sort: str = "relevance", limit: int = Query(50, le=200), offset: int = 0,
    db: Session = Depends(get_db),
):
    explicit = {
        "role": role, "min_score": min_score, "max_score": max_score, "status": status, "location": location,
        "min_experience": min_experience, "max_experience": max_experience, "max_notice_days": max_notice_days,
        "max_expectation": max_expectation, "ready_to_relocate": ready_to_relocate or None,
        "name": name, "text": text, "role_match": role_match,
        "include_relocators": include_relocators or None, "callable_only": callable_only or None,
        "keywords": [k.strip() for k in keywords.split(",") if k.strip()] if keywords else None,
    }
    try:
        return search.run_search(db, q, explicit, limit, offset, sort)
    except LLMUnavailable as e:
        raise HTTPException(503, str(e))


@app.get("/api/candidates/{cid}")
def get_candidate(cid: int, db: Session = Depends(get_db)):
    c = db.get(Candidate, cid)
    if not c:
        raise HTTPException(404, "Candidate not found.")
    return search.candidate_detail(c)


@app.patch("/api/candidates/{cid}")
def edit_candidate(cid: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    """HR corrects a phone number. Note: uploading a sheet later overwrites it with the sheet's value."""
    c = db.get(Candidate, cid)
    if not c:
        raise HTTPException(404, "Candidate not found.")
    if "phone" in payload:
        raw = (payload.get("phone") or "").strip()
        phone = importer.norm_phone(raw) if raw else None
        if raw and not phone:
            raise HTTPException(400, "That does not look like a phone number. Use 10 digits, for example 9876543210.")
        if phone:
            other = db.query(Candidate).filter(Candidate.phone == phone, Candidate.id != cid).first()
            if other:
                raise HTTPException(400, f"{other.name} already has this number. Each person needs their own number.")
        c.phone = phone
        keep = [i for i in (c.issues or []) if not i.startswith(("Invalid phone", "International number", "Missing email and phone"))]
        if phone and phone.startswith("+"):
            keep.append("International number: not called automatically")
        if not phone and not c.email:
            keep.append("Missing email and phone: calling blocked")
        c.issues = keep
        audit(db, "phone_edited", f"candidate {cid}", f"now ends {phone[-4:] if phone else 'empty'}")
    db.commit()
    return search.candidate_detail(c)


@app.post("/api/candidates/{cid}/do-not-contact")
def set_dnc(cid: int, value: bool = Body(..., embed=True), db: Session = Depends(get_db)):
    c = db.get(Candidate, cid)
    if not c:
        raise HTTPException(404, "Candidate not found.")
    c.do_not_contact = value
    if value:
        c.status = "Do not contact"
    elif c.status == "Do not contact":
        c.status = "In database"
    audit(db, "do_not_contact", f"candidate {cid}", str(value))
    db.commit()
    return search.candidate_detail(c)


# ------------------------------------------------------------------ saved searches
@app.get("/api/saved-searches")
def saved_list(db: Session = Depends(get_db)):
    return [{"id": s.id, "name": s.name, "query": s.query, "filters": s.filters}
            for s in db.query(SavedSearch).order_by(SavedSearch.id.desc())]


@app.post("/api/saved-searches")
def saved_create(payload: dict = Body(...), db: Session = Depends(get_db)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Give the search a name.")
    s = SavedSearch(name=name, query=payload.get("query") or "", filters=payload.get("filters") or {})
    db.add(s)
    db.commit()
    return {"id": s.id, "name": s.name, "query": s.query, "filters": s.filters}


@app.delete("/api/saved-searches/{sid}")
def saved_delete(sid: int, db: Session = Depends(get_db)):
    s = db.get(SavedSearch, sid)
    if s:
        db.delete(s)
        db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ download in the sheet's columns
EXPORT_COLUMNS = [
    ("Candidate ID", "candidate_ref"), ("Name", "name"), ("Resume Link", "resume_link"), ("Email", "email"), ("Phone", "phone"),
    ("Experience", "experience_text"), ("Company", "company"), ("Location", "location"),
    ("AI Screening Score", "score"), ("Best Match Role", "best_role"), ("Screening Reasoning", "reasoning"),
    ("Status", "status"), ("Communication", "communication"), ("Experience (HR)", "experience_hr"),
    ("Salary In Hand", "salary_in_hand"), ("Expectation", "expectation"), ("Negotiable", "negotiable"),
    ("Current Location", "current_location"), ("Ready To Relocate", "ready_to_relocate"),
    ("Notice Period", "notice_period_text"), ("HR Notes", "hr_notes"),
]


@app.get("/api/export")
def export(ids: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(Candidate)
    if ids:
        q = q.filter(Candidate.id.in_([int(i) for i in ids.split(",") if i.isdigit()]))
    wb = Workbook()
    ws = wb.active
    ws.title = "Career Form"
    ws.append([h for h, _ in EXPORT_COLUMNS])
    for c in q.order_by(Candidate.id):
        ws.append([("Yes" if v else "No") if isinstance(v := getattr(c, f), bool) else v for _, f in EXPORT_COLUMNS])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    audit(db, "export", "candidates", ids or "all")
    db.commit()
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": 'attachment; filename="career-form-export.xlsx"'})
