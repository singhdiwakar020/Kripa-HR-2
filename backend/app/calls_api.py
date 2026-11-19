"""HTTP endpoints for question sets, the call queue, call history and the phone simulator."""
import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy.orm import Session

from . import callengine as eng
from . import calls
from .db import get_db
from .models import CallJob, CallRecord, Candidate, Question, QuestionSet

router = APIRouter(prefix="/api")
KINDS = ("yesno", "number", "text")


def iso(dt):
    return dt.isoformat() + "Z" if dt else None  # stored as naive UTC; the Z lets the browser convert


# ------------------------------------------------------------------ question sets
def set_view(db: Session, s: QuestionSet) -> dict:
    return {"id": s.id, "name": s.name, "greeting": s.greeting, "closing": s.closing, "language": s.language, "about": s.about or "",
            "is_default": s.is_default,
            "questions": [{"id": q.id, "text": q.text, "kind": q.kind} for q in calls.questions_of(db, s.id)]}


def _write_questions(db: Session, s: QuestionSet, items):
    db.query(Question).filter(Question.set_id == s.id).delete()
    pos = 0
    for it in items or []:
        text = (it.get("text") or "").strip()
        if text:
            db.add(Question(set_id=s.id, position=pos, text=text, kind=it.get("kind") if it.get("kind") in KINDS else "text"))
            pos += 1


@router.get("/question-sets")
def sets_list(db: Session = Depends(get_db)):
    return [set_view(db, s) for s in db.query(QuestionSet).order_by(QuestionSet.id)]


@router.post("/question-sets")
def sets_create(payload: dict = Body(...), db: Session = Depends(get_db)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Give the question set a name.")
    s = QuestionSet(name=name, greeting=payload.get("greeting") or eng.DEFAULT_GREETING,
                    closing=payload.get("closing") or eng.DEFAULT_CLOSING, language=payload.get("language") or "en-IN",
                    about=payload.get("about") or "")
    db.add(s)
    db.flush()
    _write_questions(db, s, payload.get("questions"))
    db.commit()
    return set_view(db, s)


@router.put("/question-sets/{sid}")
def sets_update(sid: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    s = db.get(QuestionSet, sid)
    if not s:
        raise HTTPException(404, "Question set not found.")
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Give the question set a name.")
    s.name, s.greeting, s.closing = name, payload.get("greeting") or "", payload.get("closing") or ""
    s.language = payload.get("language") or s.language
    s.about = payload.get("about") or ""
    _write_questions(db, s, payload.get("questions"))
    if payload.get("is_default"):
        db.query(QuestionSet).update({QuestionSet.is_default: False})
        s.is_default = True
    db.commit()
    return set_view(db, s)


@router.delete("/question-sets/{sid}")
def sets_delete(sid: int, db: Session = Depends(get_db)):
    s = db.get(QuestionSet, sid)
    if not s:
        return {"ok": True}
    if s.is_default:
        raise HTTPException(400, "This is the default set. Make another set the default first.")
    if db.query(CallJob).filter(CallJob.question_set_id == sid, CallJob.state.in_(calls.ACTIVE)).first():
        raise HTTPException(400, "Calls using this set are still waiting. Cancel them first.")
    db.query(Question).filter(Question.set_id == sid).delete()
    db.delete(s)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ queue
@router.post("/calls/start")
def calls_start(payload: dict = Body(...), db: Session = Depends(get_db)):
    ids = [int(i) for i in payload.get("candidate_ids") or [] if str(i).isdigit()]
    if not ids:
        raise HTTPException(400, "Tick at least one candidate.")
    set_id = payload.get("question_set_id")
    if not set_id:
        d = db.query(QuestionSet).filter(QuestionSet.is_default.is_(True)).first() or db.query(QuestionSet).first()
        set_id = d.id if d else None
    if not set_id:
        raise HTTPException(400, "Create a question set first.")
    try:
        queued, skipped, starts = calls.queue_calls(db, ids, int(set_id), payload.get("max_attempts"),
                                                    include_screened=bool(payload.get("include_screened")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    delayed = starts > datetime.utcnow()
    return {"queued": queued, "skipped": skipped, "starts_at": iso(starts) if delayed else None, "provider": eng.cfg()["provider"]}


def job_view(db: Session, j: CallJob) -> dict:
    c = db.get(Candidate, j.candidate_id)
    s = db.get(QuestionSet, j.question_set_id) if j.question_set_id else None
    last = db.query(CallRecord).filter(CallRecord.job_id == j.id).order_by(CallRecord.id.desc()).first()
    return {"id": j.id, "candidate_id": j.candidate_id, "name": c.name if c else "?", "role": c.best_role if c else None,
            "question_set": s.name if s else None, "state": j.state, "attempts": j.attempts, "max_attempts": j.max_attempts,
            "next_run_at": iso(j.next_run_at), "last_outcome": j.last_outcome, "updated_at": iso(j.updated_at),
            "record_id": last.id if last else None, "record_status": last.status if last else None,
            "answered": len(last.answers or []) if last else 0}


@router.get("/calls")
def calls_list(db: Session = Depends(get_db)):
    jobs = db.query(CallJob).order_by(CallJob.id.desc()).limit(300).all()
    order = {"calling": 0, "queued": 1, "retry_wait": 2, "recall_wait": 3}
    jobs.sort(key=lambda j: order.get(j.state, 9))  # active first, newest first inside each group (stable sort)
    counts: dict[str, int] = {}
    for j in jobs:
        counts[j.state] = counts.get(j.state, 0) + 1
    c = eng.cfg()
    return {"jobs": [job_view(db, j) for j in jobs], "counts": counts, "provider": c["provider"],
            "settings": {"hours_start": c["hours_start"], "hours_end": c["hours_end"], "ignore_hours": c["ignore_hours"],
                         "max_attempts": c["max_attempts"], "retry_minutes": c["retry_minutes"],
                         "max_concurrent": c["max_concurrent"], "max_per_day": c["max_per_day"]}}


def _job(db: Session, jid: int) -> CallJob:
    j = db.get(CallJob, jid)
    if not j:
        raise HTTPException(404, "Call not found.")
    return j


@router.post("/calls/jobs/{jid}/cancel")
def job_cancel(jid: int, db: Session = Depends(get_db)):
    j = _job(db, jid)
    if j.state not in calls.ACTIVE:
        raise HTTPException(400, "This call is already finished.")
    calls.cancel_job(db, j)
    calls.audit(db, "call_cancelled", f"job {jid}")
    db.commit()
    return job_view(db, j)


@router.post("/calls/cancel-all")
def jobs_cancel_all(db: Session = Depends(get_db)):
    """Emergency stop: nothing waiting will be called, retries and recalls are cancelled, and live calls are hung up."""
    n = calls.cancel_all(db)
    db.commit()
    return {"cancelled": n}


@router.post("/calls/jobs/{jid}/call-now")
def job_call_now(jid: int, db: Session = Depends(get_db)):
    j = _job(db, jid)
    if j.state not in calls.WAITING:
        raise HTTPException(400, "This call is not waiting.")
    j.next_run_at = datetime.utcnow()
    db.commit()
    calls.dispatch_due(db)
    db.refresh(j)
    return job_view(db, j)


# ------------------------------------------------------------------ records
def record_view(db: Session, r: CallRecord) -> dict:
    c = db.get(Candidate, r.candidate_id)
    return {"id": r.id, "job_id": r.job_id, "candidate": {"id": r.candidate_id, "name": c.name if c else "?"},
            "attempt": r.attempt, "provider": r.provider, "status": r.status, "transcript": r.transcript or [],
            "answers": r.answers or [], "recall_at": iso(r.recall_at), "started_at": iso(r.started_at),
            "ended_at": iso(r.ended_at)}


def _rec(db: Session, rid: int) -> CallRecord:
    r = db.get(CallRecord, rid)
    if not r:
        raise HTTPException(404, "Call record not found.")
    return r


@router.get("/calls/records/{rid}")
def record_get(rid: int, db: Session = Depends(get_db)):
    return record_view(db, _rec(db, rid))


@router.get("/candidates/{cid}/calls")
def candidate_calls(cid: int, db: Session = Depends(get_db)):
    rows = db.query(CallRecord).filter(CallRecord.candidate_id == cid).order_by(CallRecord.id.desc()).all()
    return [record_view(db, r) for r in rows]


# ------------------------------------------------------------------ phone simulator (CALL_PROVIDER=mock)
def _ctx(db: Session, r: CallRecord):
    if r.provider != "mock":
        raise HTTPException(400, "The simulator only works with simulated calls.")
    job = db.get(CallJob, r.job_id)
    qset = db.get(QuestionSet, job.question_set_id or 0)
    if not qset:
        raise HTTPException(400, "The question set for this call was deleted.")
    return db.get(Candidate, r.candidate_id), qset, calls.questions_of(db, qset.id)


@router.post("/calls/records/{rid}/sim/answer")
def sim_answer(rid: int, db: Session = Depends(get_db)):
    r = _rec(db, rid)
    if r.status != "ringing":
        raise HTTPException(400, "This call is not ringing.")
    cand, qset, qs = _ctx(db, r)
    eng.start(r, cand, qset, qs)
    db.commit()
    return record_view(db, r)


@router.post("/calls/records/{rid}/sim/outcome")
def sim_outcome(rid: int, outcome: str = Body(..., embed=True), db: Session = Depends(get_db)):
    r = _rec(db, rid)
    if r.status != "ringing":
        raise HTTPException(400, "This call is not ringing.")
    if outcome not in ("no_answer", "busy", "failed"):
        raise HTTPException(400, "Unknown outcome.")
    _ctx(db, r)
    calls.finish_record(db, r, outcome)
    db.commit()
    return record_view(db, r)


@router.post("/calls/records/{rid}/sim/say")
def sim_say(rid: int, text: str = Body("", embed=True), db: Session = Depends(get_db)):
    r = _rec(db, rid)
    if r.status != "in_progress":
        raise HTTPException(400, "This call is not in progress.")
    cand, qset, qs = _ctx(db, r)
    _, outcome = eng.reply(r, cand, qset, qs, text)
    if outcome:
        calls.finish_record(db, r, outcome)
    db.commit()
    return record_view(db, r)


@router.post("/calls/records/{rid}/sim/hangup")
def sim_hangup(rid: int, db: Session = Depends(get_db)):
    r = _rec(db, rid)
    if r.status != "in_progress":
        raise HTTPException(400, "This call is not in progress.")
    _ctx(db, r)
    calls.finish_record(db, r, "incomplete")
    db.commit()
    return record_view(db, r)


# ------------------------------------------------------------------ results and downloads
OUTCOME_LABEL = {
    "completed": "Answered all questions", "callback_requested": "Asked for a callback", "no_answer": "No answer",
    "busy": "Line busy", "failed": "Call failed", "incomplete": "Ended early", "opted_out": "Asked not to be called",
    "not_interested": "Not interested", "ringing": "Ringing", "in_progress": "On the call",
}


def _answer_text(a: dict):
    v, kind = a.get("value"), a.get("kind")
    if kind == "yesno":
        return "Yes" if v is True else "No" if v is False else (a.get("raw") or "")
    if kind == "number" and isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v) if float(v).is_integer() else v
    return str(v if v not in (None, "") else (a.get("raw") or ""))


def build_results(db: Session, only: str = "completed", ids: Optional[list[int]] = None, merge: bool = False):
    """One row per candidate, built from their latest call. Returns (columns, rows).
    only='completed' keeps people who answered all the questions; 'all' keeps anyone who was called.
    merge=True puts the candidate's full sheet record in front of the call details."""
    from .main import EXPORT_COLUMNS  # imported here because main imports this module

    q = db.query(CallRecord).filter(CallRecord.status.notin_(("ringing", "in_progress")))
    if only != "all":
        q = q.filter(CallRecord.status == "completed")
    if ids:
        q = q.filter(CallRecord.candidate_id.in_(ids))
    latest: dict[int, CallRecord] = {}
    for r in q.order_by(CallRecord.id):
        latest[r.candidate_id] = r
    total_calls = {cid: n for cid, n in db.query(CallRecord.candidate_id, func_count()).group_by(CallRecord.candidate_id)}

    # question columns: script order first, then anything that only appears in old answers
    order: list[str] = []
    for qs in db.query(QuestionSet).order_by(QuestionSet.id):
        for qu in calls.questions_of(db, qs.id):
            if qu.text not in order:
                order.append(qu.text)
    used = {a["question"] for r in latest.values() for a in (r.answers or []) if a.get("kind") != "note"}
    extra = [t for r in latest.values() for a in (r.answers or []) if a.get("kind") != "note" for t in [a["question"]] if t not in order]
    qcols = [t for t in order + list(dict.fromkeys(extra)) if t in used]

    call_cols = ["Call outcome", "Call date (IST)", "Calls made", "Callback time (IST)"]
    head = ([h for h, _ in EXPORT_COLUMNS] if merge else ["Candidate ID", "Name", "Phone", "Email"]) + call_cols + qcols + ["Questions asked by candidate"]
    rows = []
    for cid, r in sorted(latest.items(), key=lambda kv: kv[1].ended_at or kv[1].started_at, reverse=True):
        c = db.get(Candidate, cid)
        if not c:
            continue
        if merge:
            base = [("Yes" if v else "No") if isinstance(v := getattr(c, f), bool) else v for _, f in EXPORT_COLUMNS]
        else:
            base = [c.candidate_ref, c.name, c.phone, c.email]
        when = lambda dt: eng.to_ist(dt).strftime("%d %b %Y %H:%M") if dt else ""  # noqa: E731
        ans: dict[str, object] = {}
        for a in r.answers or []:
            if a.get("kind") != "note":
                ans[a["question"]] = _answer_text(a)
        notes = "; ".join(a.get("raw", "") for a in (r.answers or []) if a.get("kind") == "note")
        rows.append(base + [OUTCOME_LABEL.get(r.status, r.status), when(r.ended_at or r.started_at), total_calls.get(cid, 1),
                            when(r.recall_at)] + [ans.get(t, "") for t in qcols] + [notes])
    return head, rows


def func_count():
    from sqlalchemy import func
    return func.count()


def _ids(ids: Optional[str]):
    return [int(i) for i in ids.split(",") if i.strip().isdigit()] if ids else None


@router.get("/calls/results")
def calls_results(only: str = "completed", ids: Optional[str] = None, db: Session = Depends(get_db)):
    head, rows = build_results(db, only, _ids(ids))
    return {"columns": head, "rows": rows, "count": len(rows)}


@router.get("/calls/export")
def calls_export(only: str = "completed", merge: int = 0, format: str = "xlsx", ids: Optional[str] = None,
                 db: Session = Depends(get_db)):
    """Download call results as Excel or CSV. merge=1 adds each candidate's full record in front of the call details."""
    head, rows = build_results(db, only, _ids(ids), bool(merge))
    calls.audit(db, "calls_export", f"{len(rows)} row(s)", f"merge={int(bool(merge))} only={only} format={format}")
    db.commit()
    name = f"call-results-{'merged-' if merge else ''}{datetime.now():%Y%m%d-%H%M}"
    buf = io.BytesIO()
    if format == "csv":
        text = io.StringIO()
        w = csv.writer(text)
        w.writerow(head)
        w.writerows(rows)
        buf.write(text.getvalue().encode("utf-8-sig"))  # the BOM makes Excel show Hindi and symbols correctly
        media, ext = "text/csv; charset=utf-8", "csv"
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "Call results"
        ws.append(head)
        for r in rows:
            ws.append(r)
            for cell in ws[ws.max_row]:
                if isinstance(cell.value, str) and cell.value[:1] in "=+-@":
                    cell.data_type = "s"  # keep as plain text so typed answers can never run as formulas
        ws.freeze_panes = "A2"
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = min(48, max(12, max(len(str(c.value or "")) for c in col[:60]) + 2))
        wb.save(buf)
        media, ext = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
    buf.seek(0)
    return StreamingResponse(buf, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}.{ext}"'})
