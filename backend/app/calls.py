"""Call queue, retries, recalls and the background scheduler.

Job states: queued -> calling -> done | retry_wait | recall_wait | gave_up | cancelled.
`attempts` counts unanswered attempts only; a candidate who asks for a callback does not use up a retry.
"""
import threading
import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import callengine as eng
from . import db as dbmod
from . import importer
from .models import AuditLog, CallJob, CallRecord, Candidate, Question, QuestionSet

ACTIVE = ("queued", "calling", "retry_wait", "recall_wait")
WAITING = ("queued", "retry_wait", "recall_wait")
OPEN_RECORD = ("ringing", "in_progress")

DEFAULT_QUESTIONS = [
    ("What is your current notice period?", "text"),
    ("What is your expected monthly salary, in rupees?", "number"),
    ("Are you ready to relocate if the job needs it?", "yesno"),
    ("Can you join within 30 days?", "yesno"),
]


def audit(db: Session, action: str, record: str = "", detail: str = ""):
    db.add(AuditLog(action=action, record=record, detail=detail, actor="calls"))


def seed_defaults(db: Session):
    if db.query(QuestionSet).count():
        return
    s = QuestionSet(name="Standard screening", greeting=eng.DEFAULT_GREETING, closing=eng.DEFAULT_CLOSING, is_default=True)
    db.add(s)
    db.flush()
    for i, (text, kind) in enumerate(DEFAULT_QUESTIONS):
        db.add(Question(set_id=s.id, position=i, text=text, kind=kind))
    db.commit()


def questions_of(db: Session, set_id: int) -> list[Question]:
    return db.query(Question).filter(Question.set_id == set_id).order_by(Question.position, Question.id).all()


# ------------------------------------------------------------------ queueing
def queue_calls(db: Session, candidate_ids: list[int], set_id: int, max_attempts: int | None = None,
                include_screened: bool = False):
    """Create call jobs. Returns (queued, skipped); skipped explains why a person was left out.
    include_screened=True lets HR call again someone who already answered the questions."""
    qset = db.get(QuestionSet, set_id)
    if not qset:
        raise ValueError("Question set not found.")
    now = datetime.utcnow()
    start = eng.clamp_to_hours(now)
    queued, skipped = [], []
    for cid in dict.fromkeys(candidate_ids):
        c = db.get(Candidate, cid)
        if not c:
            skipped.append({"id": cid, "name": f"#{cid}", "reason": "Candidate not found"})
        elif c.do_not_contact:
            skipped.append({"id": cid, "name": c.name, "reason": "Marked do not contact"})
        elif not c.phone:
            skipped.append({"id": cid, "name": c.name, "reason": "No valid phone number"})
        elif not importer.is_dialable(c.phone):
            skipped.append({"id": cid, "name": c.name, "reason": f"International number ({c.phone}), not called automatically"})
        elif c.status == "Rejected":
            skipped.append({"id": cid, "name": c.name, "reason": "Marked Rejected"})
        elif not include_screened and \
                db.query(CallRecord).filter(CallRecord.candidate_id == cid, CallRecord.status == "completed").first():
            skipped.append({"id": cid, "name": c.name, "reason": "Already answered the screening questions"})
        elif db.query(CallJob).filter(CallJob.candidate_id == cid, CallJob.state.in_(ACTIVE)).first():
            skipped.append({"id": cid, "name": c.name, "reason": "Already in the call queue"})
        else:
            job = CallJob(candidate_id=cid, question_set_id=set_id, state="queued", next_run_at=start,
                          max_attempts=max_attempts or eng.cfg()["max_attempts"])
            db.add(job)
            queued.append({"id": cid, "name": c.name})
    audit(db, "calls_queued", f"{len(queued)} candidate(s)", f"set {set_id}")
    db.commit()
    return queued, skipped, start


# ------------------------------------------------------------------ providers
class MockProvider:
    """Stand-in phone line. It rings, then the simulator on the Calls page plays the candidate."""
    name = "mock"

    def place_call(self, db: Session, job: CallJob, cand: Candidate) -> CallRecord:
        n = db.query(CallRecord).filter(CallRecord.job_id == job.id).count()
        rec = CallRecord(job_id=job.id, candidate_id=cand.id, attempt=n + 1, provider=self.name,
                         provider_call_id=f"mock-{uuid.uuid4().hex[:10]}", status="ringing",
                         started_at=datetime.utcnow())
        db.add(rec)
        return rec


def get_provider():
    name = eng.cfg()["provider"]
    if name == "mock":
        return MockProvider()
    if name == "plivo":
        from .plivo_calls import PlivoProvider  # imported here because that module imports this one
        return PlivoProvider()
    raise RuntimeError(f"Unknown CALL_PROVIDER '{name}'. Use 'mock' or 'plivo'.")


# ------------------------------------------------------------------ outcomes
def finish_record(db: Session, rec: CallRecord, outcome: str, now: datetime | None = None):
    """Close a call attempt and decide what happens to the job: done, retry, recall, or give up."""
    now = now or datetime.utcnow()
    rec.status = outcome
    rec.ended_at = now
    job = db.get(CallJob, rec.job_id)
    cand = db.get(Candidate, rec.candidate_id)
    c = eng.cfg()
    was_cancelled = job.state == "cancelled"
    if outcome == "completed":
        job.state, job.last_outcome, job.next_run_at = "done", "Questions answered", None
        _mark_called(cand)
    elif outcome == "callback_requested":
        job.state, job.next_run_at = "recall_wait", eng.clamp_to_hours(rec.recall_at or now)
        job.last_outcome = f"Asked to be called on {eng.fmt_when(job.next_run_at)}"
        _mark_called(cand)
    elif outcome == "opted_out":
        job.state, job.last_outcome, job.next_run_at = "cancelled", "Asked not to be called again", None
        cand.do_not_contact, cand.status = True, "Do not contact"
        audit(db, "do_not_contact", f"candidate {cand.id}", "asked on a call")
    elif outcome == "not_interested":
        job.state, job.last_outcome, job.next_run_at = "done", "Not interested", None
        _mark_called(cand)
    else:  # no_answer, busy, failed, incomplete: use up an attempt
        job.attempts += 1
        label = {"no_answer": "No answer", "busy": "Line busy", "failed": "Call failed", "incomplete": "Call ended early"}[outcome]
        if job.attempts >= job.max_attempts:
            job.state, job.next_run_at = "gave_up", None
            job.last_outcome = f"{label}. Gave up after {job.attempts} attempts"
            if cand.status in ("In database", "Called"):
                cand.status = "Not reachable"
        else:
            job.state = "retry_wait"
            job.next_run_at = eng.clamp_to_hours(now + timedelta(minutes=c["retry_minutes"]))
            job.last_outcome = f"{label}. Trying again ({job.attempts} of {job.max_attempts})"
    if was_cancelled and outcome != "opted_out":  # HR stopped this one while the call was still in progress
        job.state, job.next_run_at, job.last_outcome = "cancelled", None, "Cancelled by HR"
    audit(db, "call_" + outcome, f"candidate {cand.id}", f"job {job.id}, attempt {rec.attempt}")


def cancel_job(db: Session, job: CallJob):
    """Stop a job for good. A call already ringing is closed here; the provider's next webhook then hangs up."""
    for rec in db.query(CallRecord).filter(CallRecord.job_id == job.id, CallRecord.status.in_(OPEN_RECORD)):
        rec.status, rec.ended_at = "failed", datetime.utcnow()
    job.state, job.next_run_at, job.last_outcome = "cancelled", None, "Cancelled by HR"


def cancel_all(db: Session) -> int:
    jobs = db.query(CallJob).filter(CallJob.state.in_(ACTIVE)).all()
    for j in jobs:
        cancel_job(db, j)
    audit(db, "calls_stopped_all", f"{len(jobs)} job(s)")
    return len(jobs)


def _mark_called(cand: Candidate):
    if cand.status == "In database":
        cand.status = "Called"


# ------------------------------------------------------------------ scheduler
def expire_stale(db: Session, now: datetime):
    """A call nobody picked up or finished must not block its job forever."""
    cutoff = now - timedelta(minutes=eng.cfg()["stale_minutes"])
    for rec in db.query(CallRecord).filter(CallRecord.status.in_(OPEN_RECORD), CallRecord.started_at < cutoff):
        finish_record(db, rec, "no_answer" if rec.status == "ringing" else "completed" if eng.reached_final_stage(rec) else "incomplete", now)


def dispatch_due(db: Session, now: datetime | None = None) -> int:
    """Place every call that is due. Called every few seconds by the scheduler thread."""
    now = now or datetime.utcnow()
    expire_stale(db, now)
    db.flush()  # sessions here do not autoflush; the query below must see the jobs just closed
    c = eng.cfg()
    slots = c["max_concurrent"] - db.query(CallJob).filter(CallJob.state == "calling").count()
    if c["max_per_day"]:  # real calls cost money: stop at the daily limit
        day_start = eng.from_ist(eng.to_ist(now).replace(hour=0, minute=0, second=0, microsecond=0))
        used = db.query(CallRecord).filter(CallRecord.provider != "mock", CallRecord.started_at >= day_start).count()
        slots = min(slots, c["max_per_day"] - used)
    if slots <= 0:
        db.commit()
        return 0
    due = (db.query(CallJob).filter(CallJob.state.in_(WAITING), CallJob.next_run_at <= now)
           .order_by(CallJob.next_run_at).limit(100).all())
    placed = 0
    for job in due:
        if placed >= slots:
            break
        cand = db.get(Candidate, job.candidate_id)
        if not cand or cand.do_not_contact or not importer.is_dialable(cand.phone):
            job.state, job.last_outcome, job.next_run_at = "cancelled", "Skipped: cannot be called", None
            continue
        if not db.get(QuestionSet, job.question_set_id or 0):
            job.state, job.last_outcome, job.next_run_at = "cancelled", "Question set was deleted", None
            continue
        if not eng.within_hours(now):
            job.next_run_at = eng.clamp_to_hours(now)
            continue
        rec = get_provider().place_call(db, job, cand)
        if rec.status in OPEN_RECORD:  # a provider that could not place the call has already closed the record
            job.state = "calling"
        placed += 1
    db.commit()
    return placed


_started = False


def start_scheduler(interval: float = 10.0):
    global _started
    if _started:
        return
    _started = True

    def loop():
        while True:
            try:
                s = dbmod.SessionLocal()
                try:
                    dispatch_due(s)
                    from . import exam as _exam
                    _exam.sweep(s)  # auto-submit tests whose time ran out
                finally:
                    s.close()
            except Exception as e:  # keep the loop alive whatever happens
                print("call scheduler error:", type(e).__name__, e)
            time.sleep(interval)

    threading.Thread(target=loop, name="call-scheduler", daemon=True).start()
