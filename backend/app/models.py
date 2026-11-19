from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

# Fields that come from the sheet. A later upload may update these.
SHEET_FIELDS = [
    "candidate_ref", "name", "email", "phone", "resume_link", "experience_text", "experience_years",
    "company", "location", "score", "best_role", "reasoning",
    "experience_hr", "communication", "salary_in_hand", "expectation", "negotiable",
    "current_location", "ready_to_relocate", "notice_period_days", "notice_period_text",
    "hr_notes", "applied_at",
]


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    file_name: Mapped[str] = mapped_column(String(300))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(20), default="preview")  # preview | confirmed
    headers: Mapped[list] = mapped_column(JSON, default=list)
    raw_rows: Mapped[list] = mapped_column(JSON, default=list)
    mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    rows_added: Mapped[int] = mapped_column(Integer, default=0)
    rows_updated: Mapped[int] = mapped_column(Integer, default=0)
    rows_skipped: Mapped[int] = mapped_column(Integer, default=0)
    rows_need_fix: Mapped[int] = mapped_column(Integer, default=0)
    rows_merged: Mapped[int] = mapped_column(Integer, default=0)


class Candidate(Base):
    __tablename__ = "candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"), nullable=True)

    candidate_ref: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)  # the sheet's "Candidate ID"
    name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)  # normalised
    resume_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    experience_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # from resume, as written
    experience_years: Mapped[float | None] = mapped_column(Float, nullable=True)
    experience_hr: Mapped[str | None] = mapped_column(Text, nullable=True)  # entered by HR after a call
    company: Mapped[str | None] = mapped_column(String(300), nullable=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    best_role: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    communication: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_in_hand: Mapped[float | None] = mapped_column(Float, nullable=True)
    expectation: Mapped[float | None] = mapped_column(Float, nullable=True)
    negotiable: Mapped[str | None] = mapped_column(String(50), nullable=True)
    current_location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ready_to_relocate: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    notice_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notice_period_text: Mapped[str | None] = mapped_column(String(100), nullable=True)
    hr_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Owned by the system. The sheet never overwrites these.
    status: Mapped[str] = mapped_column(String(40), default="In database", index=True)
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    issues: Mapped[list] = mapped_column(JSON, default=list)  # e.g. ["Resume not read"]
    merged_from: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SavedSearch(Base):
    __tablename__ = "saved_searches"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    query: Mapped[str] = mapped_column(Text, default="")
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class QuestionSet(Base):
    """A script HR manages: greeting, ordered questions, closing line."""
    __tablename__ = "question_sets"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    greeting: Mapped[str] = mapped_column(Text, default="")
    closing: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(20), default="en-IN")
    about: Mapped[str] = mapped_column(Text, default="")  # facts the caller may share when a candidate asks (company, role, pay...)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    set_id: Mapped[int] = mapped_column(ForeignKey("question_sets.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20), default="text")  # yesno | number | text


class CallJob(Base):
    """One candidate waiting to be called, being called, or finished. Drives retries and recalls."""
    __tablename__ = "call_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    question_set_id: Mapped[int | None] = mapped_column(ForeignKey("question_sets.id"), nullable=True)
    # queued | calling | retry_wait | recall_wait | done | gave_up | cancelled
    state: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)  # unanswered attempts so far
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)  # UTC
    last_outcome: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CallRecord(Base):
    """One actual call attempt, with the transcript and the answers collected."""
    __tablename__ = "call_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("call_jobs.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    provider: Mapped[str] = mapped_column(String(20), default="mock")
    provider_call_id: Mapped[str] = mapped_column(String(100), default="")
    # ringing | in_progress | completed | callback_requested | no_answer | busy | failed | incomplete | opted_out | not_interested
    status: Mapped[str] = mapped_column(String(30), default="ringing", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    transcript: Mapped[list] = mapped_column(JSON, default=list)
    answers: Mapped[list] = mapped_column(JSON, default=list)
    conv_state: Mapped[dict] = mapped_column(JSON, default=dict)
    recall_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # UTC


class Exam(Base):
    """A test HR builds: instructions, a time limit, MCQs and coding questions."""
    __tablename__ = "exams"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    instructions: Mapped[str] = mapped_column(Text, default="")
    duration_minutes: Mapped[int] = mapped_column(Integer, default=60)
    negative_marking: Mapped[float] = mapped_column(Float, default=0.0)  # share of an MCQ's marks lost for a wrong answer
    max_violations: Mapped[int] = mapped_column(Integer, default=2)  # warnings allowed; the next one ends the test
    show_score: Mapped[bool] = mapped_column(Boolean, default=False)  # whether candidates see their score after submitting
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ExamQuestion(Base):
    __tablename__ = "exam_questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(10), default="mcq")  # mcq | coding
    text: Mapped[str] = mapped_column(Text)
    options: Mapped[list] = mapped_column(JSON, default=list)  # mcq: the choices
    correct: Mapped[list] = mapped_column(JSON, default=list)  # mcq: indexes of the right choices
    marks: Mapped[float] = mapped_column(Float, default=1.0)
    starter_code: Mapped[str] = mapped_column(Text, default="")  # coding
    test_cases: Mapped[list] = mapped_column(JSON, default=list)  # coding: [{"input", "expected", "hidden"}]


class ExamInvite(Base):
    """One candidate's login and attempt for one exam."""
    __tablename__ = "exam_invites"
    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    email: Mapped[str] = mapped_column(String(200), index=True)
    salt: Mapped[str] = mapped_column(String(64), default="")
    pw_hash: Mapped[str] = mapped_column(String(200), default="")
    # invited | in_progress | submitted | timed_out | terminated
    status: Mapped[str] = mapped_column(String(20), default="invited", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)  # question id -> {"choice": [..]} or {"code": "..."}
    violations: Mapped[list] = mapped_column(JSON, default=list)  # [{"at": iso, "kind": str}]
    violation_count: Mapped[int] = mapped_column(Integer, default=0)
    mcq_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    coding_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # None while coding still needs grading
    coding_detail: Mapped[dict] = mapped_column(JSON, default=dict)  # question id -> {"passed", "total", "marks", "cases"}
    manual_coding: Mapped[dict] = mapped_column(JSON, default=dict)  # question id -> marks given by HR
    total_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    grading: Mapped[str] = mapped_column(String(10), default="none")  # none | running | done | manual
    voided: Mapped[bool] = mapped_column(Boolean, default=False)


class BlockedContact(Base):
    """Email/phone of people marked do-not-contact. Survives 'clear all data' so they are never called after a re-upload."""
    __tablename__ = "blocked_contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    actor: Mapped[str] = mapped_column(String(100), default="hr")
    action: Mapped[str] = mapped_column(String(100))
    record: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
