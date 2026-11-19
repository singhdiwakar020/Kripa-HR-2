"""Tests the calling flow end to end with the built-in phone simulator. No phone line and no OpenAI needed."""
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ["OPENAI_API_KEY"] = ""  # the time parser must not reach out to OpenAI here
os.environ["CALL_PROVIDER"] = "mock"
os.environ["CALL_RETRY_MINUTES"] = "0"
os.environ["CALL_MAX_ATTEMPTS"] = "2"

import app.db as dbmod  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

tmp = Path(tempfile.mkdtemp()) / "t.db"
dbmod.engine = create_engine(f"sqlite:///{tmp}", connect_args={"check_same_thread": False})
dbmod.SessionLocal = sessionmaker(bind=dbmod.engine, autoflush=False, expire_on_commit=False)

from fastapi.testclient import TestClient  # noqa: E402
from app import callengine as eng, calls  # noqa: E402
from app.main import app  # noqa: E402
from app.db import Base, get_db  # noqa: E402

Base.metadata.create_all(dbmod.engine)


def override():
    s = dbmod.SessionLocal()
    try:
        yield s
    finally:
        s.close()


app.dependency_overrides[get_db] = override

# ---------------------------------------------------------------- call-back time parser
now_utc = datetime(2026, 11, 16, 6, 30)  # Monday 16 Nov 2026, 12:00 in India
cases = {
    "tomorrow at 4 pm": (2026, 11, 17, 16, 0),
    "tomorrow 4:30pm": (2026, 11, 17, 16, 30),
    "Monday morning": (2026, 11, 23, 10, 0),
    "Friday at 3": (2026, 11, 20, 15, 0),
    "in 2 hours": (2026, 11, 16, 14, 0),
    "18 Nov 3:30pm": (2026, 11, 18, 15, 30),
    "kal shaam 5 baje": (2026, 11, 17, 17, 0),
    "tomorrow": (2026, 11, 17, 11, 0),
    "at 4": (2026, 11, 16, 16, 0),
    "day after tomorrow evening": (2026, 11, 18, 18, 0),
}
for text, want in cases.items():
    got = eng.parse_when(text, now_utc)
    assert got is not None, text
    t = eng.to_ist(got)
    assert (t.year, t.month, t.day, t.hour, t.minute) == want, (text, t)
assert eng.parse_when("banana", now_utc) is None
assert eng.parse_when("", now_utc) is None
print("OK: callback time parser,", len(cases), "phrases")

# ---------------------------------------------------------------- data
c = TestClient(app)
with open("sample_career_form.xlsx", "rb") as fh:
    p = c.post("/api/imports", files={"file": ("s.xlsx", fh)}).json()
c.post(f"/api/imports/{p['batch_id']}/confirm")

s0 = dbmod.SessionLocal()
calls.seed_defaults(s0)
s0.close()
sets = c.get("/api/question-sets").json()
assert len(sets) == 1 and len(sets[0]["questions"]) == 4 and sets[0]["is_default"], sets
SET = sets[0]["id"]

people = [x for x in c.get("/api/search", params={"limit": 200}).json()["results"] if x["phone"]]
A, B, C, D, E, F = [x["id"] for x in people[:6]]
c.post(f"/api/candidates/{D}/do-not-contact", json={"value": True})


def dispatch(at=None):
    s = dbmod.SessionLocal()
    try:
        return calls.dispatch_due(s, at)
    finally:
        s.close()


def jobs():
    return {j["candidate_id"]: j for j in c.get("/api/calls").json()["jobs"]}


def say(rid, text):
    return c.post(f"/api/calls/records/{rid}/sim/say", json={"text": text}).json()


# ---------------------------------------------------------------- queue rules
r = c.post("/api/calls/start", json={"candidate_ids": [A, B, C, D, E, F], "question_set_id": SET}).json()
assert len(r["queued"]) == 5 and r["skipped"][0]["reason"] == "Marked do not contact", r
r2 = c.post("/api/calls/start", json={"candidate_ids": [A], "question_set_id": SET}).json()
assert r2["queued"] == [] and r2["skipped"][0]["reason"] == "Already in the call queue", r2
assert dispatch() == 5
J = jobs()
assert all(J[x]["state"] == "calling" and J[x]["record_status"] == "ringing" for x in (A, B, C, E, F)), J
print("OK: queue skips do-not-contact and duplicates; 5 calls ringing")

# ---------------------------------------------------------------- A: full screening
rid = J[A]["record_id"]
v = c.post(f"/api/calls/records/{rid}/sim/answer").json()
assert v["status"] == "in_progress" and "good time" in v["transcript"][-1]["text"]
say(rid, "yes, go ahead")
say(rid, "about 30 days")
say(rid, "40000")
say(rid, "sure")
v = say(rid, "yes")
assert v["status"] == "completed", v
assert [a["value"] for a in v["answers"]] == ["about 30 days", 40000.0, True, True], v["answers"]
assert jobs()[A]["state"] == "done" and c.get(f"/api/candidates/{A}").json()["status"] == "Called"
print("OK: A answered every question, saved, status Called")

# ---------------------------------------------------------------- B: busy, asks for a callback time
rid = J[B]["record_id"]
c.post(f"/api/calls/records/{rid}/sim/answer")
v = say(rid, "sorry I am busy right now")
assert "back" in v["transcript"][-1]["text"], v["transcript"][-1]
v = say(rid, "tomorrow at 4 pm")
assert "correct" in v["transcript"][-1]["text"]
v = say(rid, "yes")
assert v["status"] == "callback_requested" and v["recall_at"], v
jb = jobs()[B]
assert jb["state"] == "recall_wait" and jb["attempts"] == 0, jb
due = datetime.fromisoformat(jb["next_run_at"].rstrip("Z"))
assert eng.to_ist(due).hour == 16 and due > datetime.utcnow()
assert dispatch() == 0 and jobs()[B]["state"] == "recall_wait", "must not call before the agreed time"
print("OK: B asked for a callback; recall booked for tomorrow 4 pm India time, no retry used")

# ---------------------------------------------------------------- C: no answer, retries, then gives up
rid = J[C]["record_id"]
c.post(f"/api/calls/records/{rid}/sim/outcome", json={"outcome": "no_answer"})
assert jobs()[C]["state"] == "retry_wait" and jobs()[C]["attempts"] == 1
assert dispatch() >= 1 and jobs()[C]["state"] == "calling"
rid2 = jobs()[C]["record_id"]
assert rid2 != rid
c.post(f"/api/calls/records/{rid2}/sim/outcome", json={"outcome": "busy"})
jc = jobs()[C]
assert jc["state"] == "gave_up" and "Gave up" in jc["last_outcome"], jc
assert c.get(f"/api/candidates/{C}").json()["status"] == "Not reachable"
print("OK: C retried automatically, then gave up and became Not reachable")

# ---------------------------------------------------------------- E: asks not to be called
rid = J[E]["record_id"]
c.post(f"/api/calls/records/{rid}/sim/answer")
v = say(rid, "please stop calling me")
assert v["status"] == "opted_out"
assert jobs()[E]["state"] == "cancelled"
assert c.get(f"/api/candidates/{E}").json()["do_not_contact"] is True
assert E not in [x["id"] for x in c.get("/api/search", params={"limit": 200}).json()["results"]]
print("OK: E opted out, is do-not-contact and hidden from search")

# ---------------------------------------------------------------- time passes: B is recalled, F's unanswered call is closed
f_first = J[F]["record_id"]
assert dispatch(due + timedelta(minutes=1)) >= 1
assert c.get(f"/api/calls/records/{f_first}").json()["status"] == "no_answer", "unanswered ringing call must be closed"
jf = jobs()[F]
assert jf["attempts"] == 1 and jf["record_id"] != f_first, jf  # counted, and retried
assert jobs()[B]["state"] == "calling", "B must be called at the agreed time"
recs = c.get(f"/api/candidates/{B}/calls").json()
assert len(recs) == 2 and recs[0]["attempt"] == 2 and recs[0]["status"] == "ringing", recs
assert jobs()[B]["attempts"] == 0
print("OK: B recalled at the agreed time; F's unanswered call closed, counted and retried")

# ---------------------------------------------------------------- safeguards for real bulk use
os.environ["CALL_PROVIDER"] = "plivo"  # only to read the live-mode defaults; no provider is created here
live = eng.cfg()
assert live["max_concurrent"] == 3 and live["max_per_day"] == 150 and live["ignore_hours"] is False, live
os.environ["CALL_PROVIDER"] = "mock"

from app.models import Candidate  # noqa: E402

G, H, I, K, L = [x["id"] for x in people[6:11]]
s1 = dbmod.SessionLocal()
s1.query(Candidate).filter(Candidate.id == L).update({"status": "Rejected"})
s1.commit()
s1.close()
r = c.post("/api/calls/start", json={"candidate_ids": [A, G, H, I, K, L], "question_set_id": SET}).json()
why = {x["id"]: x["reason"] for x in r["skipped"]}
assert why[A] == "Already answered the screening questions" and why[L] == "Marked Rejected", why
assert len(r["queued"]) == 4, r
in_use = sum(1 for j in jobs().values() if j["state"] == "calling")
os.environ["CALL_MAX_CONCURRENT"] = str(in_use + 2)
assert dispatch() == 2, "must only use the free lines, not ring everyone at once"
assert dispatch() == 0
del os.environ["CALL_MAX_CONCURRENT"]
print("OK: skips rejected and already-screened people; rings only as many as there are free lines")

n = c.post("/api/calls/cancel-all").json()["cancelled"]
assert n >= 4, n
assert all(j["state"] not in calls.ACTIVE for j in c.get("/api/calls").json()["jobs"])
assert c.post(f"/api/calls/records/{jobs()[B]['record_id']}/sim/say", json={"text": "hello"}).status_code == 400
print("OK: Stop all cancels everything waiting and closes calls in progress")

pub = TestClient(app, base_url="http://abc123.ngrok-free.app")  # what the internet sees
for method, path in [("get", "/api/facets"), ("get", "/api/candidates/1"), ("get", "/docs"), ("get", "/api/calls")]:
    got = getattr(pub, method)(path)
    assert got.status_code == 403 and "Not available" in got.json()["detail"], (path, got.status_code)
assert pub.post("/api/calls/start", json={"candidate_ids": [1]}).status_code == 403
assert "Not available" not in pub.post("/api/plivo/answer?rid=1").text, "Plivo webhooks must stay reachable"
print("OK: the public address serves only Plivo webhooks, never candidate data")

# ---------------------------------------------------------------- LLM-driven conversation (stand-in model)
from app import llm as _llm  # noqa: E402

seen_ctx = []


def fake_understand(step, text, ctx):
    seen_ctx.append(ctx)
    t = text.lower()
    if "salary" in t:
        return {"intent": "question", "value": None, "callback_iso": None, "say": "The stipend is shared at the interview."}
    if "robot" in t:
        return {"intent": "question", "value": None, "callback_iso": None, "say": "I am an automated assistant."}
    if "say that again" in t:
        return {"intent": "repeat", "value": None, "callback_iso": None, "say": None}
    if "tomorrow" in t:
        tom = (eng.to_ist(datetime.utcnow()) + timedelta(days=1)).strftime("%Y-%m-%dT16:00:00")
        return {"intent": "time", "value": None, "callback_iso": tom, "say": None}
    if "yah" in t or "go on" in t:
        return {"intent": "agree", "value": None, "callback_iso": None, "say": None}
    if t.strip() == "yes":
        return {"intent": "answer", "value": "yes", "callback_iso": None, "say": None}
    if "month" in t:
        return {"intent": "answer", "value": "one month", "callback_iso": None, "say": None}
    if "fifty" in t:
        return {"intent": "answer", "value": "50000", "callback_iso": None, "say": None}
    if "boom" in t:
        raise _llm.LLMUnavailable("down")
    return {"intent": "unclear", "value": None, "callback_iso": None, "say": None}


os.environ["OPENAI_API_KEY"] = "test-key"
real_understand = _llm.understand
_llm.understand = fake_understand
c.put(f"/api/question-sets/{SET}", json={**c.get("/api/question-sets").json()[0], "about": "We are Acme Tech in Bhopal."})

M1, M2 = [x["id"] for x in people[11:13]]
c.post("/api/calls/start", json={"candidate_ids": [M1, M2], "question_set_id": SET})
assert dispatch() == 2
J2 = jobs()
rid = J2[M1]["record_id"]
c.post(f"/api/calls/records/{rid}/sim/answer")
v = say(rid, "what is the salary")  # a question mid-call: answered, then the pending question is asked again
assert v["transcript"][-2]["text"] == "The stipend is shared at the interview." and "good time" in v["transcript"][-1]["text"], v["transcript"][-2:]
assert seen_ctx[-1]["facts"] == "We are Acme Tech in Bhopal."
v = say(rid, "are you a robot")
assert "automated assistant" in v["transcript"][-2]["text"]
v = say(rid, "yah sach good time talk")  # garbled "yes": understood by meaning
assert "notice period" in v["transcript"][-1]["text"], v["transcript"][-1]
v = say(rid, "say that again")
assert "notice period" in v["transcript"][-1]["text"]
say(rid, "it is one month")
v = say(rid, "fifty thousand rupees")
assert [a["value"] for a in v["answers"] if a["kind"] != "note"] == ["one month", 50000.0], v["answers"]
assert sum(1 for a in v["answers"] if a["kind"] == "note") == 2  # the two side questions are saved for HR
say(rid, "yes")
v = say(rid, "yes")
assert v["status"] == "in_progress" and "any questions" in v["transcript"][-1]["text"], v  # facts exist: closing Q&A first
v = say(rid, "no")
assert v["status"] == "completed", v
print("OK: LLM path: answers side questions from the facts, repeats, understands garbled speech, saves what was asked")

rid = J2[M2]["record_id"]
c.post(f"/api/calls/records/{rid}/sim/answer")
v = say(rid, "can you call tomorrow")  # volunteers a time right away
assert "correct" in v["transcript"][-1]["text"] and "4:00 PM" in v["transcript"][-1]["text"], v["transcript"][-1]
v = say(rid, "yah")
assert v["status"] == "callback_requested" and v["recall_at"], v
print("OK: LLM path: a candidate who offers a call-back time straight away gets it booked")

# if the model is down mid-call, the keyword rules keep the call going
c.post("/api/calls/cancel-all")
N1 = people[13]["id"]
c.post("/api/calls/start", json={"candidate_ids": [N1], "question_set_id": SET})
dispatch()
rid = jobs()[N1]["record_id"]
c.post(f"/api/calls/records/{rid}/sim/answer")
v = say(rid, "boom yes")
assert "notice period" in v["transcript"][-1]["text"], v["transcript"][-1]
print("OK: when the LLM fails the call carries on with the keyword rules")
_llm.understand = real_understand
os.environ["OPENAI_API_KEY"] = ""

# ---------------------------------------------------------------- plain replies skip the language model
from types import SimpleNamespace as NS  # noqa: E402
qs = [NS(kind="yesno"), NS(kind="number"), NS(kind="text")]
assert eng._fast("availability", "Yes.", qs, {"q": 0}) == {"intent": "agree"}
assert eng._fast("availability", "haan ji", qs, {"q": 0}) == {"intent": "agree"}
assert eng._fast("confirm_when", "No", qs, {"q": 0}) == {"intent": "disagree"}
assert eng._fast("question", "Yes!", qs, {"q": 0})["value"] is True
assert eng._fast("question", "no", qs, {"q": 0})["value"] is False
assert eng._fast("question", "50,000 rupees", qs, {"q": 1})["value"] == 50000.0
assert eng._fast("question", "30", qs, {"q": 1})["value"] == 30.0
for step, text, q in [("availability", "yes but call me tomorrow", 0), ("availability", "what is the salary", 0),
                      ("availability", "no problem", 0), ("question", "about fifty thousand", 1), ("question", "one month", 2),
                      ("question", "yes", 2), ("ask_when", "tomorrow", 0)]:
    assert eng._fast(step, text, qs, {"q": q}) is None, (step, text)
print("OK: plain yes/no/number replies skip the language model; anything with more in it still goes to it")

# ---------------------------------------------------------------- re-calling and results download
import csv as _csv, io as _io  # noqa: E402
from openpyxl import load_workbook as _lw  # noqa: E402

c.post("/api/calls/cancel-all")
r = c.post("/api/calls/start", json={"candidate_ids": [A], "question_set_id": SET}).json()
assert r["queued"] == [] and r["skipped"][0]["reason"] == "Already answered the screening questions", r
r = c.post("/api/calls/start", json={"candidate_ids": [A, L], "question_set_id": SET, "include_screened": True}).json()
assert [x["id"] for x in r["queued"]] == [A] and r["skipped"][0]["reason"] == "Marked Rejected", r  # a re-call, but Rejected stays blocked
assert dispatch() >= 1
newest = max((j for j in c.get("/api/calls").json()["jobs"] if j["candidate_id"] == A), key=lambda j: j["id"])
assert newest["state"] == "calling" and newest["record_status"] == "ringing", newest
c.post("/api/calls/cancel-all")
print("OK: someone who already finished can be called again on request; Rejected people still cannot")

res = c.get("/api/calls/results").json()
cols, rows = res["columns"], res["rows"]
assert res["count"] >= 1 and cols[:4] == ["Candidate ID", "Name", "Phone", "Email"], cols
row = next(x for x in rows if dict(zip(cols, x))["Name"] == c.get(f"/api/candidates/{A}").json()["name"])
d = dict(zip(cols, row))
assert d["Call outcome"] == "Answered all questions" and d["Calls made"] >= 2, d
assert d["What is your current notice period?"] == "about 30 days" and d["What is your expected monthly salary, in rupees?"] == 40000, d
assert d["Are you ready to relocate if the job needs it?"] == "Yes" and d["Can you join within 30 days?"] == "Yes", d
assert B not in [x for x in []] and all(dict(zip(cols, x))["Call outcome"] == "Answered all questions" for x in rows)  # default: finished calls only
every = c.get("/api/calls/results", params={"only": "all"}).json()
assert every["count"] > res["count"], (every["count"], res["count"])

csv_plain = c.get("/api/calls/export", params={"format": "csv"})
assert csv_plain.status_code == 200 and "attachment" in csv_plain.headers["content-disposition"]
hdr = next(_csv.reader(_io.StringIO(csv_plain.content.decode("utf-8-sig"))))
assert hdr[:4] == ["Candidate ID", "Name", "Phone", "Email"] and "AI Screening Score" not in hdr, hdr
x_plain = _lw(_io.BytesIO(c.get("/api/calls/export").content)).active
assert [cell.value for cell in x_plain[1]][:4] == ["Candidate ID", "Name", "Phone", "Email"] and x_plain.max_row == res["count"] + 1

merged = _lw(_io.BytesIO(c.get("/api/calls/export", params={"merge": 1}).content)).active
mh = [cell.value for cell in merged[1]]
assert mh[:3] == ["Candidate ID", "Name", "Resume Link"] and "AI Screening Score" in mh and "HR Notes" in mh, mh
assert mh.index("Call outcome") > mh.index("HR Notes") and "What is your current notice period?" in mh
mr = dict(zip(mh, [cell.value for cell in merged[2]]))
assert mr["Call outcome"] and mr["Email"], mr
one = c.get("/api/calls/results", params={"ids": str(A)}).json()
assert one["count"] == 1
print("OK: results table and downloads: Excel/CSV, calls only or merged with the full candidate record, one question per column")

# ---------------------------------------------------------------- question set editing
new = c.post("/api/question-sets", json={"name": "Sales", "questions": [{"text": "Do you have a laptop?", "kind": "yesno"}, {"text": " ", "kind": "text"}]}).json()
assert [q["text"] for q in new["questions"]] == ["Do you have a laptop?"], new
assert c.delete(f"/api/question-sets/{SET}").status_code == 400  # default set is protected
assert c.delete(f"/api/question-sets/{new['id']}").status_code == 200
print("\nALL CALL TESTS PASSED")

# ---- Hindi / Hinglish number words are read as numbers
assert eng.hindi_digits("tees din") == "30 din"
assert eng._read_answer("number", "tees")[0] == 30.0
assert eng._read_answer("number", "तीस")[0] == 30.0
print("hindi number words OK")

# ---- after the script, the candidate can ask about the company, and "no" ends the call smoothly
from types import SimpleNamespace as NS  # noqa: E402
from app import llm as _llm  # noqa: E402


def _qa_call(replies):
    qset = NS(greeting="Hi {name}", closing="Thanks. Goodbye.", about="Office in Arera Colony, Bhopal.", language="en-IN")
    qs = [NS(id=1, text="Notice period?", kind="text")]
    rec = NS(transcript=[], answers=[], conv_state={"step": "question", "retries": 0, "q": 0, "side": 0}, recall_at=None)
    cand = NS(name="Asha Rao", best_role="AI Developer")
    out, last = [], None
    for said in replies:
        lines, last = eng.reply(rec, cand, qset, qs, said)
        out.append(lines)
        if last:
            break
    return rec, out, last


_orig_llm = _llm.understand
_os_env = os.environ.get("CALL_USE_LLM")
_os_key = os.environ.get("OPENAI_API_KEY")
try:
    def _fake(step, text, ctx):
        t = text.lower()
        if step == "final_qa":
            if "where" in t:
                return {"intent": "question", "value": None, "callback_iso": None, "say": "The office is in Arera Colony, Bhopal."}
            return {"intent": "disagree", "value": None, "callback_iso": None, "say": None}
        return {"intent": "answer", "value": "30 days", "callback_iso": None, "say": None}
    _llm.understand = _fake
    os.environ["CALL_USE_LLM"] = "1"
    os.environ["OPENAI_API_KEY"] = "test"
    rec, out, last = _qa_call(["thirty days", "where is the office", "nothing else"])
    assert "any questions" in out[0][0] and last == "completed", (out, last)
    assert out[1][0].startswith("The office is in Arera") and "anything else" in out[1][1].lower() and eng.reached_final_stage(rec) is True
    assert out[2] == ["Thanks. Goodbye."], out
    rec, out, last = _qa_call(["thirty days", "no"])  # plain "no" is handled without the model
    assert last == "completed" and out[1] == ["Thanks. Goodbye."]
finally:
    _llm.understand = _orig_llm
    if _os_key is not None:
        os.environ["OPENAI_API_KEY"] = _os_key
    if _os_env is None:
        os.environ.pop("CALL_USE_LLM", None)
    else:
        os.environ["CALL_USE_LLM"] = _os_env
print("OK: closing Q&A stage answers questions, then ends the call on 'no'")
