"""Test (exam) logic: candidate passwords and sessions, timing, violations, grading.

Candidates log in with their email and a personal password at /exam. Everything they can reach lives under /api/exam/*
and only ever shows their own attempt. The HR screens (/api/exams/*, candidates, search, calls) stay local-only.
"""
import hashlib
import hmac
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from . import aigrade, coderun
from . import db as dbmod
from .models import Candidate, Exam, ExamInvite, ExamQuestion

TERMINAL = ("submitted", "timed_out", "terminated")
STATUS_LABEL = {"invited": "Not started", "in_progress": "In progress", "submitted": "Submitted",
                "timed_out": "Timed out (auto-submitted)", "terminated": "Ended: policy violation (auto-submitted)"}
VIOLATION_GAP = 3.0  # seconds: one action (leaving full screen, losing focus, hiding the tab) is often several browser events
PW_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"  # no 0/O, 1/l/I


# ------------------------------------------------------------------ passwords and sessions
def new_password() -> str:
    a = "".join(secrets.choice(PW_ALPHABET) for _ in range(10))
    return a[:5] + "-" + a[5:]


def hash_password(password: str, salt: str) -> str:
    return hashlib.scrypt(password.strip().encode("utf-8"), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32).hex()


def set_password(inv: ExamInvite) -> str:
    """Give the invite a fresh random password. Returns it in plain text; only the scrambled form is stored."""
    pw = new_password()
    inv.salt = secrets.token_hex(16)
    inv.pw_hash = hash_password(pw, inv.salt)
    return pw


def _secret() -> bytes:
    env = os.getenv("EXAM_SECRET", "").strip()
    if env:
        return env.encode()
    f = Path(__file__).resolve().parent.parent / ".exam_secret"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
    return f.read_text().strip().encode()


def make_token(inv: ExamInvite, ttl_minutes: int) -> str:
    exp = int(time.time()) + ttl_minutes * 60
    body = f"{inv.id}.{exp}.{inv.pw_hash[:12]}"  # the password fingerprint ends old sessions when the password is reset
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_token(token: str) -> tuple[int, str] | None:
    try:
        iid, exp, fp, sig = token.split(".")
        body = f"{iid}.{exp}.{fp}"
        if not hmac.compare_digest(sig, hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()):
            return None
        if int(exp) < time.time():
            return None
        return int(iid), fp
    except (ValueError, AttributeError):
        return None


# ------------------------------------------------------------------ login throttling (in memory)
_fails: dict[str, list] = {}
_lock = threading.Lock()
MAX_FAILS, LOCK_SECONDS = 5, 15 * 60


def locked(key: str) -> int:
    """Seconds left on a lock for this email or address, else 0."""
    with _lock:
        n, until = _fails.get(key, [0, 0.0])
        if until > time.time():
            return int(until - time.time()) + 1
        if until:
            _fails.pop(key, None)
        return 0


def note_fail(key: str, limit: int = MAX_FAILS):
    with _lock:
        n, until = _fails.get(key, [0, 0.0])
        n += 1
        _fails[key] = [n, time.time() + LOCK_SECONDS if n >= limit else 0.0]


def clear_fails(key: str):
    with _lock:
        _fails.pop(key, None)


# ------------------------------------------------------------------ views
def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def questions_of(db: Session, exam_id: int) -> list[ExamQuestion]:
    return db.query(ExamQuestion).filter(ExamQuestion.exam_id == exam_id).order_by(ExamQuestion.position, ExamQuestion.id).all()


def candidate_view(db: Session, inv: ExamInvite) -> dict:
    """What a candidate may see about their own attempt. Never includes correct answers or hidden test cases."""
    exam = db.get(Exam, inv.exam_id)
    cand = db.get(Candidate, inv.candidate_id)
    qs = questions_of(db, inv.exam_id)
    first = ((cand.name if cand else "") or "").strip().split(" ")[0]
    out = {"status": inv.status, "status_label": STATUS_LABEL.get(inv.status, inv.status), "title": exam.title,
           "instructions": exam.instructions, "duration_minutes": exam.duration_minutes, "first_name": first,
           "question_count": len(qs), "total_marks": sum(q.marks for q in qs), "max_violations": exam.max_violations,
           "violation_count": inv.violation_count, "server_now": iso(datetime.utcnow()), "ends_at": iso(inv.ends_at)}
    if inv.status in TERMINAL and exam.show_score and not inv.voided and inv.grading == "done":
        out["score"] = {"total": inv.total_score, "max": inv.max_score}
    return out


def question_view(q: ExamQuestion) -> dict:
    d = {"id": q.id, "kind": q.kind, "text": q.text, "marks": q.marks}
    if q.kind == "mcq":
        d.update(options=q.options, multiple=len(q.correct or []) > 1)
    else:
        d.update(starter_code=q.starter_code or "",
                 samples=[{"input": c.get("input", ""), "expected": c.get("expected", "")} for c in (q.test_cases or []) if not c.get("hidden")])
    return d


# ------------------------------------------------------------------ finishing and grading
def mcq_marks(q: ExamQuestion, inv: ExamInvite, exam: Exam) -> float:
    """Marks for one MCQ: full marks for exactly the right choices, a deduction for a wrong answer, 0 if left blank."""
    picked = {int(i) for i in ((inv.answers or {}).get(str(q.id)) or {}).get("choice", []) if str(i).lstrip("-").isdigit()}
    if not picked:
        return 0.0
    return q.marks if picked == set(q.correct or []) else -q.marks * (exam.negative_marking or 0)


def grade_mcq(inv: ExamInvite, exam: Exam, qs: list[ExamQuestion]) -> float:
    return max(0.0, round(sum(mcq_marks(q, inv, exam) for q in qs if q.kind == "mcq"), 2))


def recompute(inv: ExamInvite, exam: Exam, qs: list[ExamQuestion]):
    coding = [q for q in qs if q.kind == "coding"]
    got, missing = 0.0, False
    for q in coding:
        manual = (inv.manual_coding or {}).get(str(q.id))
        auto = (inv.coding_detail or {}).get(str(q.id))
        if manual is not None:
            got += min(float(manual), q.marks)
        elif auto is not None:
            got += auto["marks"]
        else:
            missing = True
    inv.coding_score = None if (coding and missing) else round(got, 2)
    inv.max_score = round(sum(q.marks for q in qs), 2)
    inv.total_score = round((inv.mcq_score or 0) + got, 2)  # coding marks still unknown count as 0 until graded
    if not coding or not missing:
        inv.grading = "done"


def can_grade_code() -> bool:
    return coderun.available() or aigrade.enabled()


def finalize(db: Session, inv: ExamInvite, status: str) -> ExamInvite:
    """End the attempt (submitted, timed_out or terminated), grade the MCQs now and the coding in the background."""
    if inv.status in TERMINAL:
        return inv
    exam = db.get(Exam, inv.exam_id)
    qs = questions_of(db, inv.exam_id)
    now = datetime.utcnow()
    inv.status = status
    inv.submitted_at = min(now, inv.ends_at) if (status == "timed_out" and inv.ends_at) else now
    inv.mcq_score = grade_mcq(inv, exam, qs)
    inv.coding_detail = inv.coding_detail or {}
    coding = [q for q in qs if q.kind == "coding"]
    if coding and can_grade_code():
        inv.grading = "running"
    elif coding:
        inv.grading = "manual"  # neither a code runner nor AI: HR marks the coding answers by hand
    recompute(inv, exam, qs)
    cand = db.get(Candidate, inv.candidate_id)
    if cand and not cand.do_not_contact and cand.status != "Rejected":
        cand.status = "Test submitted"
    db.commit()
    if inv.grading == "running":
        threading.Thread(target=grade_coding_in_background, args=(inv.id,), daemon=True).start()
    return inv


def grade_coding_in_background(invite_id: int):
    """Mark every coding answer. With Docker: run the test cases (marks) and ask the AI for written feedback.
    Without Docker: the AI reads the code and proposes the marks. Anything that fails is left for HR to mark by hand."""
    s = dbmod.SessionLocal()
    try:
        inv = s.get(ExamInvite, invite_id)
        exam = s.get(Exam, inv.exam_id)
        qs = questions_of(s, inv.exam_id)
        keep = {k: v for k, v in (inv.coding_detail or {}).items() if k in {str(q.id) for q in qs}}  # already marked and unchanged
        detail = dict(keep)
        runner = coderun.available()
        for q in qs:
            if q.kind != "coding" or str(q.id) in detail:
                continue
            code = (((inv.answers or {}).get(str(q.id)) or {}).get("code") or "")
            cases = q.test_cases or []
            if not code.strip() or not cases:
                detail[str(q.id)] = {"passed": 0, "total": len(cases), "marks": 0.0, "cases": [], "by": "tests"}
                continue
            if runner:
                res = coderun.run_cases(code, cases)
                passed = sum(1 for r in res if r["passed"])
                d = {"passed": passed, "total": len(cases), "marks": round(q.marks * passed / len(cases), 2), "by": "tests",
                     "cases": [{"passed": r["passed"], "hidden": bool(c.get("hidden")), "timeout": r["timeout"],
                                "error": r["error"][:300]} for c, r in zip(cases, res)]}
                if aigrade.enabled():  # tests decide the marks; the AI only explains
                    try:
                        d["ai"] = aigrade.review(q.text, q.marks, code, cases, f"{passed} of {len(cases)} test cases passed")
                    except Exception as e:
                        print("AI feedback failed:", type(e).__name__, str(e)[:120])
                detail[str(q.id)] = d
            elif aigrade.enabled():
                try:
                    r = aigrade.review(q.text, q.marks, code, cases)
                    detail[str(q.id)] = {"passed": None, "total": len(cases), "marks": r["marks"], "cases": [], "by": "ai", "ai": r}
                except Exception as e:  # leave this answer for HR
                    print("AI marking failed:", type(e).__name__, str(e)[:120])
        inv.coding_detail = detail
        recompute(inv, exam, qs)
        inv.grading = "done" if inv.coding_score is not None else "manual"
        s.commit()
    except Exception as e:  # leave it for HR to mark by hand rather than losing the attempt
        print("exam grading failed:", type(e).__name__, e)
        try:
            inv = s.get(ExamInvite, invite_id)
            inv.grading = "manual"
            s.commit()
        except Exception:
            pass
    finally:
        s.close()


def regrade_finished(db: Session, exam: Exam, changed_coding: set) -> dict:
    """After HR edits a test that people already took: recompute every finished attempt against the new questions.
    Returns the invite ids that were recalculated ('all') and those whose code must be run again ('run')."""
    qs = questions_of(db, exam.id)
    coding = [q for q in qs if q.kind == "coding"]
    live = {str(q.id) for q in coding}
    out = {"all": [], "run": []}
    for inv in db.query(ExamInvite).filter(ExamInvite.exam_id == exam.id, ExamInvite.status.in_(TERMINAL)):
        detail = {k: v for k, v in (inv.coding_detail or {}).items() if k in live and int(k) not in changed_coding}
        inv.coding_detail = detail
        inv.manual_coding = {k: v for k, v in (inv.manual_coding or {}).items() if k in live}
        inv.mcq_score = grade_mcq(inv, exam, qs)
        recompute(inv, exam, qs)
        need = [q for q in coding if str(q.id) not in detail and str(q.id) not in (inv.manual_coding or {})]
        if need:
            inv.grading = "running" if can_grade_code() else "manual"
            if inv.grading == "running":
                out["run"].append(inv.id)
        out["all"].append(inv.id)
    return out


def expire_if_overdue(db: Session, inv: ExamInvite) -> ExamInvite:
    if inv.status == "in_progress" and inv.ends_at and datetime.utcnow() >= inv.ends_at:
        finalize(db, inv, "timed_out")
    return inv


def sweep(db: Session) -> int:
    """Auto-submit every attempt whose time ran out, even when the candidate closed their browser."""
    n = 0
    for inv in db.query(ExamInvite).filter(ExamInvite.status == "in_progress", ExamInvite.ends_at <= datetime.utcnow()):
        finalize(db, inv, "timed_out")
        n += 1
    return n


def start(db: Session, inv: ExamInvite) -> ExamInvite:
    if inv.status == "invited":
        exam = db.get(Exam, inv.exam_id)
        inv.status = "in_progress"
        inv.started_at = datetime.utcnow()
        inv.ends_at = inv.started_at + timedelta(minutes=exam.duration_minutes)
        db.commit()
    return inv


def record_violation(db: Session, inv: ExamInvite, kind: str) -> dict:
    """Count a violation (leaving the tab, window or full screen). More than the allowed number ends the test."""
    exam = db.get(Exam, inv.exam_id)
    now = datetime.utcnow()
    ignored = False
    if inv.violations:
        try:
            last = datetime.fromisoformat(inv.violations[-1]["at"].rstrip("Z"))
            ignored = (now - last).total_seconds() < VIOLATION_GAP
        except (ValueError, KeyError):
            ignored = False
    if not ignored:
        inv.violations = (inv.violations or []) + [{"at": now.isoformat() + "Z", "kind": str(kind)[:40]}]
        inv.violation_count = len(inv.violations)
        if inv.violation_count > exam.max_violations:
            finalize(db, inv, "terminated")
        db.commit()
    return {"count": inv.violation_count, "max": exam.max_violations, "status": inv.status, "ignored": ignored}
