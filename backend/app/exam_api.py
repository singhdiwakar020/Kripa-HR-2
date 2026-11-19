"""HTTP side of the tests.

/api/exam/*   public: the candidate's own login, questions, answers and submit. Needs the candidate's session token.
/exam         public: the candidate's test page (one HTML file, no HR screens in it).
/api/exams/*  HR only (the local_only middleware keeps these off the public address): build tests, invite, results.
"""
import csv
import io
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from openpyxl import Workbook
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import aigrade, coderun
from . import exam as ex
from . import mailer
from .db import get_db
from .models import AuditLog, Candidate, Exam, ExamInvite, ExamQuestion

public = APIRouter(prefix="/api/exam")
page_router = APIRouter()
hr = APIRouter(prefix="/api/exams")

PAGE = Path(__file__).resolve().parent / "exam_page.html"


def audit(db: Session, action: str, record: str = "", detail: str = ""):
    db.add(AuditLog(action=action, record=record, detail=detail, actor="exam"))


# =================================================================== candidate side
@page_router.get("/exam", response_class=HTMLResponse)
def exam_page():
    html = PAGE.read_text(encoding="utf-8") if PAGE.exists() else "<h1>Test page missing</h1>"
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                       "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "?")


@public.post("/login")
def login(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
    email = str(payload.get("email") or "").strip().lower()
    password = str(payload.get("password") or "")
    ip = "ip:" + _ip(request)
    wait = max(ex.locked(email) if email else 0, ex.locked(ip))
    if wait:
        raise HTTPException(429, f"Too many wrong attempts. Try again in {wait // 60 + 1} minute(s).")
    invites = db.query(ExamInvite).filter(func.lower(ExamInvite.email) == email).order_by(ExamInvite.id.desc()).all() if email else []
    matched = None
    for inv in invites:
        if inv.pw_hash and ex.hmac.compare_digest(ex.hash_password(password, inv.salt), inv.pw_hash):
            matched = inv
            break
    if not invites:
        ex.hash_password(password, "00" * 16)  # same work for unknown emails, so timing does not reveal who is invited
    if not matched:
        if email:
            ex.note_fail(email)
        ex.note_fail(ip, limit=30)
        raise HTTPException(401, "Wrong email or password.")
    ex.clear_fails(email)
    exam = db.get(Exam, matched.exam_id)
    ex.expire_if_overdue(db, matched)
    token = ex.make_token(matched, exam.duration_minutes + 180)
    return {"token": token, "exam": ex.candidate_view(db, matched)}


def current_invite(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)) -> ExamInvite:
    parsed = ex.read_token((authorization or "").removeprefix("Bearer ").strip())
    inv = db.get(ExamInvite, parsed[0]) if parsed else None
    if not inv or inv.pw_hash[:12] != parsed[1]:
        raise HTTPException(401, "Please log in again.")
    return ex.expire_if_overdue(db, inv)


def _saved(inv: ExamInvite) -> dict:
    return inv.answers or {}


@public.get("/me")
def me(inv: ExamInvite = Depends(current_invite), db: Session = Depends(get_db)):
    return ex.candidate_view(db, inv)


@public.post("/start")
def start(inv: ExamInvite = Depends(current_invite), db: Session = Depends(get_db)):
    ex.start(db, inv)
    out = {"exam": ex.candidate_view(db, inv)}
    if inv.status == "in_progress":
        out["questions"] = [ex.question_view(q) for q in ex.questions_of(db, inv.exam_id)]
        out["answers"] = _saved(inv)
    return out


@public.put("/answer")
def answer(payload: dict = Body(...), inv: ExamInvite = Depends(current_invite), db: Session = Depends(get_db)):
    if inv.status != "in_progress":
        raise HTTPException(409, "This test is not running.")
    q = db.get(ExamQuestion, int(payload.get("question_id") or 0))
    if not q or q.exam_id != inv.exam_id:
        raise HTTPException(404, "No such question.")
    new = dict(_saved(inv))
    if q.kind == "mcq":
        choice = sorted({int(i) for i in (payload.get("choice") or []) if isinstance(i, int) and 0 <= i < len(q.options or [])})
        new[str(q.id)] = {"choice": choice}
    else:
        new[str(q.id)] = {"code": str(payload.get("code") or "")[:50_000]}
    inv.answers = new  # a new dict, so the change is noticed and saved
    db.commit()
    return {"saved": True, "server_now": ex.iso(datetime.utcnow())}


@public.post("/violation")
def violation(payload: dict = Body(...), inv: ExamInvite = Depends(current_invite), db: Session = Depends(get_db)):
    if inv.status != "in_progress":
        return {"count": inv.violation_count, "status": inv.status, "ignored": True}
    return ex.record_violation(db, inv, str(payload.get("kind") or "unknown"))


_last_run: dict[int, float] = {}


@public.post("/run")
def run_code(payload: dict = Body(...), inv: ExamInvite = Depends(current_invite), db: Session = Depends(get_db)):
    """Run the candidate's code on the sample test cases only (hidden ones are used when grading)."""
    if inv.status != "in_progress":
        raise HTTPException(409, "This test is not running.")
    q = db.get(ExamQuestion, int(payload.get("question_id") or 0))
    if not q or q.exam_id != inv.exam_id or q.kind != "coding":
        raise HTTPException(404, "No such coding question.")
    if not coderun.available():
        raise HTTPException(503, "Running code is not available right now. Your code is still saved and will be marked.")
    if time.time() - _last_run.get(inv.id, 0) < 3:
        raise HTTPException(429, "Please wait a few seconds between runs.")
    _last_run[inv.id] = time.time()
    code = str(payload.get("code") or "")[:50_000]
    cases = [c for c in (q.test_cases or []) if not c.get("hidden")]
    res = coderun.run_cases(code, cases)
    return {"results": [{"input": c.get("input", ""), "expected": c.get("expected", ""), "passed": r["passed"], "got": r["got"],
                         "error": r["error"], "timeout": r["timeout"]} for c, r in zip(cases, res)]}


@public.post("/submit")
def submit(inv: ExamInvite = Depends(current_invite), db: Session = Depends(get_db)):
    if inv.status == "in_progress":
        ex.finalize(db, inv, "submitted")
    return ex.candidate_view(db, inv)


# =================================================================== HR side
def _exam_dict(db: Session, e: Exam, full: bool = False) -> dict:
    qs = ex.questions_of(db, e.id)
    invites = db.query(ExamInvite).filter(ExamInvite.exam_id == e.id).all()
    done = [i for i in invites if i.status in ex.TERMINAL and not i.voided]
    d = {"id": e.id, "title": e.title, "instructions": e.instructions, "duration_minutes": e.duration_minutes,
         "negative_marking": e.negative_marking, "max_violations": e.max_violations, "show_score": e.show_score,
         "created_at": e.created_at.isoformat(), "question_count": len(qs), "mcq_count": sum(q.kind == "mcq" for q in qs),
         "coding_count": sum(q.kind == "coding" for q in qs), "total_marks": sum(q.marks for q in qs),
         "invited": len(invites), "started": sum(i.status != "invited" for i in invites), "finished": len(done),
         "average": round(sum(i.total_score or 0 for i in done) / len(done), 2) if done else None,
         "locked": any(i.status != "invited" for i in invites), "code_runner": coderun.available(), "ai_grading": aigrade.enabled()}
    if full:
        d["questions"] = [{"id": q.id, "kind": q.kind, "text": q.text, "marks": q.marks, "options": q.options or [],
                           "correct": q.correct or [], "starter_code": q.starter_code or "", "test_cases": q.test_cases or []}
                          for q in qs]
    return d


def _clean_questions(items) -> list[dict]:
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "Add at least one question.")
    out = []
    for n, it in enumerate(items, start=1):
        kind = it.get("kind")
        text = str(it.get("text") or "").strip()
        try:
            marks = float(it.get("marks") or 0)
        except (TypeError, ValueError):
            marks = 0
        if kind not in ("mcq", "coding") or not text:
            raise HTTPException(400, f"Question {n}: write the question text.")
        if not 0 < marks <= 1000:
            raise HTTPException(400, f"Question {n}: marks must be more than 0.")
        q = {"kind": kind, "text": text, "marks": marks, "options": [], "correct": [], "starter_code": "", "test_cases": [],
             "keep_id": it["id"] if isinstance(it.get("id"), int) else None}  # an existing question keeps its id, so saved answers stay attached
        if kind == "mcq":
            options = [str(o).strip() for o in (it.get("options") or [])]
            if len(options) < 2 or any(not o for o in options) or len(options) > 8:
                raise HTTPException(400, f"Question {n}: give 2 to 8 choices, none empty.")
            correct = sorted({int(i) for i in (it.get("correct") or []) if isinstance(i, int)})
            if not correct or any(not 0 <= i < len(options) for i in correct):
                raise HTTPException(400, f"Question {n}: tick the right choice.")
            q.update(options=options, correct=correct)
        else:
            cases = []
            for c in it.get("test_cases") or []:
                if str(c.get("expected", "")).strip() == "" and str(c.get("input", "")).strip() == "":
                    continue
                cases.append({"input": str(c.get("input", "")), "expected": str(c.get("expected", "")), "hidden": bool(c.get("hidden"))})
            if not cases:
                raise HTTPException(400, f"Question {n}: add at least one test case.")
            if all(c["hidden"] for c in cases):
                raise HTTPException(400, f"Question {n}: keep at least one sample test case visible so candidates can try their code.")
            q.update(starter_code=str(it.get("starter_code") or ""), test_cases=cases)
        out.append(q)
    return out


def _apply_exam(db: Session, e: Exam, payload: dict, questions: list[dict]):
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Give the test a title.")
    try:
        minutes = int(payload.get("duration_minutes") or 0)
        neg = float(payload.get("negative_marking") or 0)
        maxv = int(2 if payload.get("max_violations") in (None, "") else payload.get("max_violations"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Time limit, negative marking and warnings must be numbers.")
    if not 5 <= minutes <= 480:
        raise HTTPException(400, "The time limit must be between 5 and 480 minutes.")
    if not 0 <= neg <= 1:
        raise HTTPException(400, "Negative marking is a share of the marks between 0 and 1 (0.25 means a quarter).")
    if not 0 <= maxv <= 10:
        raise HTTPException(400, "Warnings allowed must be between 0 and 10.")
    e.title, e.instructions = title[:200], str(payload.get("instructions") or "")
    e.duration_minutes, e.negative_marking, e.max_violations = minutes, neg, maxv
    e.show_score = bool(payload.get("show_score"))
    db.flush()
    old = {q.id: q for q in ex.questions_of(db, e.id)}
    changed, kept = set(), set()  # changed: coding questions whose marks or test cases differ, or that are new
    for pos, q in enumerate(questions):
        q = dict(q)
        row = old.get(q.pop("keep_id"))
        if row is None or row.id in kept or row.kind != q["kind"]:
            row = ExamQuestion(exam_id=e.id, position=pos, **q)
            db.add(row)
            db.flush()
            if q["kind"] == "coding":
                changed.add(row.id)
        else:
            if q["kind"] == "coding" and (row.marks != q["marks"] or (row.test_cases or []) != q["test_cases"]):
                changed.add(row.id)
        kept.add(row.id)
        row.position = pos
        for k, v in q.items():
            setattr(row, k, v)
    for qid, row in old.items():
        if qid not in kept:
            db.delete(row)
    return changed


@hr.get("")
def list_exams(db: Session = Depends(get_db)):
    return [_exam_dict(db, e) for e in db.query(Exam).order_by(Exam.id.desc())]


@hr.post("")
def create_exam(payload: dict = Body(...), db: Session = Depends(get_db)):
    questions = _clean_questions(payload.get("questions"))
    e = Exam(title="x")
    db.add(e)
    db.flush()
    _apply_exam(db, e, payload, questions)
    audit(db, "exam_created", e.title)
    db.commit()
    return _exam_dict(db, e, True)


@hr.get("/{exam_id}")
def get_exam(exam_id: int, db: Session = Depends(get_db)):
    e = db.get(Exam, exam_id)
    if not e:
        raise HTTPException(404, "No such test.")
    return _exam_dict(db, e, True)


@hr.put("/{exam_id}")
def update_exam(exam_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    e = db.get(Exam, exam_id)
    if not e:
        raise HTTPException(404, "No such test.")
    questions = _clean_questions(payload.get("questions"))
    taking = db.query(ExamInvite).filter(ExamInvite.exam_id == e.id, ExamInvite.status == "in_progress").count()
    if taking:
        raise HTTPException(409, f"{taking} candidate{' is' if taking == 1 else 's are'} taking this test right now, so it cannot be changed until "
                                 "they finish (their screen would not match). Try again in a few minutes.")
    changed = _apply_exam(db, e, payload, questions)
    db.flush()
    again = ex.regrade_finished(db, e, changed)  # scores of people who already finished follow the new questions
    audit(db, "exam_updated", e.title, f"{len(again['all'])} finished attempt(s) recalculated" if again["all"] else "")
    db.commit()
    for iid in again["run"]:
        threading.Thread(target=ex.grade_coding_in_background, args=(iid,), daemon=True).start()
    out = _exam_dict(db, e, True)
    out["recalculated"] = len(again["all"])
    return out


@hr.delete("/{exam_id}")
def delete_exam(exam_id: int, db: Session = Depends(get_db)):
    e = db.get(Exam, exam_id)
    if not e:
        raise HTTPException(404, "No such test.")
    invites = db.query(ExamInvite).filter(ExamInvite.exam_id == e.id)  # HR may delete at any stage: results go with it
    total, started = invites.count(), invites.filter(ExamInvite.status != "invited").count()
    invites.delete()
    db.query(ExamQuestion).filter(ExamQuestion.exam_id == e.id).delete()
    audit(db, "exam_deleted", e.title, f"{total} invite(s), {started} started")
    db.delete(e)
    db.commit()
    return {"deleted": True}


def _exam_url() -> str:
    return (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/") + "/exam"


@hr.post("/{exam_id}/invite")
def invite(exam_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    """Create a login for each candidate. The passwords are returned ONCE here (only a scrambled copy is stored)."""
    e = db.get(Exam, exam_id)
    if not e:
        raise HTTPException(404, "No such test.")
    ids = [int(i) for i in payload.get("candidate_ids") or [] if str(i).isdigit()]
    if not ids:
        raise HTTPException(400, "Choose at least one candidate.")
    created, skipped, existing = [], [], []
    for cid in dict.fromkeys(ids):
        c = db.get(Candidate, cid)
        if not c:
            continue
        if c.do_not_contact:
            skipped.append({"id": cid, "name": c.name, "reason": "Marked do not contact"})
        elif not c.email:
            skipped.append({"id": cid, "name": c.name, "reason": "No email address"})
        elif db.query(ExamInvite).filter(ExamInvite.exam_id == e.id, ExamInvite.candidate_id == cid).first():
            existing.append({"id": cid, "name": c.name, "reason": "Already invited (use Reset password if they lost it)"})
        else:
            inv = ExamInvite(exam_id=e.id, candidate_id=cid, email=c.email.strip().lower())
            pw = ex.set_password(inv)
            db.add(inv)
            db.flush()
            created.append({"invite_id": inv.id, "candidate_id": cid, "candidate_ref": c.candidate_ref, "name": c.name,
                            "email": inv.email, "password": pw, "email_sent": False, "email_error": None})
    if payload.get("send_email", True) and mailer.enabled():
        for row in created:  # a failed email never blocks the invite: HR still sees the password
            err = mailer.send_invite(row["email"], row["name"], e.title, e.duration_minutes, e.max_violations,
                                     _exam_url(), row["password"])
            row["email_sent"], row["email_error"] = err is None, err
    sent = sum(1 for r in created if r["email_sent"])
    audit(db, "exam_invited", e.title, f"{len(created)} invited, {sent} emailed, {len(skipped)} skipped")
    db.commit()
    return {"created": created, "skipped": skipped, "existing": existing, "exam_url": _exam_url(), "exam_title": e.title,
            "email_enabled": mailer.enabled()}


@hr.post("/invites/{invite_id}/reset-password")
def reset_password(invite_id: int, db: Session = Depends(get_db)):
    inv = db.get(ExamInvite, invite_id)
    if not inv:
        raise HTTPException(404, "No such invite.")
    if inv.status in ex.TERMINAL:
        raise HTTPException(409, "This candidate has already finished the test.")
    pw = ex.set_password(inv)  # also ends any open session of this candidate
    audit(db, "exam_password_reset", inv.email)
    db.commit()
    err = None
    if mailer.enabled():
        e, c = db.get(Exam, inv.exam_id), db.get(Candidate, inv.candidate_id)
        err = mailer.send_invite(inv.email, c.name if c else "", e.title, e.duration_minutes, e.max_violations, _exam_url(), pw)
    return {"email": inv.email, "password": pw, "exam_url": _exam_url(),
            "email_enabled": mailer.enabled(), "email_sent": mailer.enabled() and err is None, "email_error": err}


def _result_rows(db: Session, exam_id: int) -> list[dict]:
    rows = []
    for inv in db.query(ExamInvite).filter(ExamInvite.exam_id == exam_id):
        ex.expire_if_overdue(db, inv)
        c = db.get(Candidate, inv.candidate_id)
        pct = round(100 * (inv.total_score or 0) / inv.max_score, 1) if inv.max_score else None
        rows.append({"invite_id": inv.id, "candidate_id": inv.candidate_id, "candidate_ref": c.candidate_ref if c else None,
                     "name": c.name if c else "?", "email": inv.email, "phone": c.phone if c else None,
                     "status": inv.status, "status_label": ex.STATUS_LABEL.get(inv.status, inv.status),
                     "started_at": ex.iso(inv.started_at), "submitted_at": ex.iso(inv.submitted_at),
                     "mcq_score": inv.mcq_score, "coding_score": inv.coding_score, "total_score": inv.total_score,
                     "max_score": inv.max_score, "percent": pct, "violations": inv.violation_count,
                     "grading": inv.grading, "voided": inv.voided})
    finished = sorted([r for r in rows if r["status"] in ex.TERMINAL and not r["voided"]], key=lambda r: -(r["total_score"] or 0))
    for i, r in enumerate(finished, start=1):
        r["rank"] = i
    db.commit()
    order = {"in_progress": 0, "invited": 2}
    return sorted(rows, key=lambda r: (order.get(r["status"], 1), r.get("rank", 9999), r["name"].lower()))


@hr.get("/{exam_id}/results")
def results(exam_id: int, db: Session = Depends(get_db)):
    e = db.get(Exam, exam_id)
    if not e:
        raise HTTPException(404, "No such test.")
    return {"exam": _exam_dict(db, e), "rows": _result_rows(db, exam_id)}


@hr.get("/{exam_id}/export")
def export(exam_id: int, format: str = "xlsx", db: Session = Depends(get_db)):
    e = db.get(Exam, exam_id)
    if not e:
        raise HTTPException(404, "No such test.")
    head = ["Candidate ID", "Name", "Email", "Phone", "Rank", "Status", "Started (IST)", "Submitted (IST)", "MCQ score",
            "Coding score", "Total", "Out of", "Percent", "Violations", "Violation details", "Coding marking", "Voided"]
    def ist(s):
        return datetime.fromisoformat(s.rstrip("Z")) + timedelta(hours=5, minutes=30) if s else None

    def fmt(s):
        d = ist(s)
        return d.strftime("%d %b %Y %H:%M") if d else ""
    rows = []
    for r in _result_rows(db, exam_id):
        inv = db.get(ExamInvite, r["invite_id"])
        detail = "; ".join(f"{fmt(v['at'])[-5:]} {v['kind']}" for v in inv.violations or [])
        rows.append([r["candidate_ref"], r["name"], r["email"], r["phone"], r.get("rank", ""), r["status_label"], fmt(r["started_at"]),
                     fmt(r["submitted_at"]), r["mcq_score"], "pending" if r["coding_score"] is None and r["status"] in ex.TERMINAL else r["coding_score"],
                     r["total_score"], r["max_score"], r["percent"], r["violations"], detail, r["grading"], "Yes" if r["voided"] else ""])
    audit(db, "exam_export", e.title, f"{len(rows)} row(s)")
    db.commit()
    name = f"test-results-{re.sub('[^A-Za-z0-9]+', '-', e.title)[:40]}-{datetime.now():%Y%m%d-%H%M}"
    buf = io.BytesIO()
    if format == "csv":
        text = io.StringIO()
        w = csv.writer(text)
        w.writerow(head)
        w.writerows(rows)
        buf.write(text.getvalue().encode("utf-8-sig"))
        media, ext = "text/csv; charset=utf-8", "csv"
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "Results"
        ws.append(head)
        for r in rows:
            ws.append(r)
            for cell in ws[ws.max_row]:
                if isinstance(cell.value, str) and cell.value[:1] in "=+-@":
                    cell.data_type = "s"
        ws.freeze_panes = "A2"
        wb.save(buf)
        media, ext = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
    buf.seek(0)
    return StreamingResponse(buf, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}.{ext}"'})


@hr.get("/invites/{invite_id}")
def invite_detail(invite_id: int, db: Session = Depends(get_db)):
    inv = db.get(ExamInvite, invite_id)
    if not inv:
        raise HTTPException(404, "No such invite.")
    ex.expire_if_overdue(db, inv)
    e = db.get(Exam, inv.exam_id)
    c = db.get(Candidate, inv.candidate_id)
    qs = []
    for q in ex.questions_of(db, inv.exam_id):
        a = (inv.answers or {}).get(str(q.id)) or {}
        item = {"id": q.id, "kind": q.kind, "text": q.text, "marks": q.marks}
        if q.kind == "mcq":
            item.update(options=q.options, correct=q.correct, choice=a.get("choice", []), awarded=round(ex.mcq_marks(q, inv, e), 2) if inv.status in ex.TERMINAL else None)
        else:
            det = (inv.coding_detail or {}).get(str(q.id))
            manual = (inv.manual_coding or {}).get(str(q.id))
            item.update(code=a.get("code", ""), detail=det, manual=manual,
                        awarded=manual if manual is not None else (det["marks"] if det else None))
        qs.append(item)
    return {"invite_id": inv.id, "exam": {"id": e.id, "title": e.title}, "candidate": {"id": c.id, "name": c.name, "ref": c.candidate_ref},
            "email": inv.email, "status": inv.status, "status_label": ex.STATUS_LABEL.get(inv.status), "voided": inv.voided,
            "grading": inv.grading, "mcq_score": inv.mcq_score, "coding_score": inv.coding_score, "total_score": inv.total_score,
            "max_score": inv.max_score, "started_at": ex.iso(inv.started_at), "submitted_at": ex.iso(inv.submitted_at),
            "violations": inv.violations or [], "questions": qs, "code_runner": coderun.available()}


@hr.patch("/invites/{invite_id}")
def mark_invite(invite_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    """HR marks coding answers by hand, or voids / restores an attempt (for example after a false policy violation)."""
    inv = db.get(ExamInvite, invite_id)
    if not inv:
        raise HTTPException(404, "No such invite.")
    e = db.get(Exam, inv.exam_id)
    qs = ex.questions_of(db, inv.exam_id)
    by_id = {str(q.id): q for q in qs}
    if "manual_coding" in payload:
        manual = dict(inv.manual_coding or {})
        for qid, marks in (payload["manual_coding"] or {}).items():
            q = by_id.get(str(qid))
            if not q or q.kind != "coding":
                raise HTTPException(400, "Unknown coding question.")
            if marks in (None, ""):
                manual.pop(str(qid), None)
                continue
            try:
                m = float(marks)
            except (TypeError, ValueError):
                raise HTTPException(400, "Marks must be a number.")
            if not 0 <= m <= q.marks:
                raise HTTPException(400, f"Marks for this question must be between 0 and {q.marks:g}.")
            manual[str(qid)] = m
        inv.manual_coding = manual
    if "voided" in payload:
        inv.voided = bool(payload["voided"])
    if inv.status in ex.TERMINAL:
        ex.recompute(inv, e, qs)
    audit(db, "exam_marked", inv.email, "voided" if inv.voided else "marks updated")
    db.commit()
    return invite_detail(invite_id, db)
