"""Tests the candidate test feature end to end on a throwaway database. No Docker, OpenAI or network needed."""
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ.update({"OPENAI_API_KEY": "", "ELEVENLABS_API_KEY": "", "PUBLIC_BASE_URL": "https://demo.ngrok-free.app", "EXAM_SECRET": "test-secret"})

import app.db as dbmod  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

tmp = Path(tempfile.mkdtemp()) / "t.db"
dbmod.engine = create_engine(f"sqlite:///{tmp}", connect_args={"check_same_thread": False})
dbmod.SessionLocal = sessionmaker(bind=dbmod.engine, autoflush=False, expire_on_commit=False)

from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
import io  # noqa: E402
from app import coderun, exam as ex  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Candidate, ExamInvite  # noqa: E402

Base.metadata.create_all(dbmod.engine)
REAL_RUNNER = coderun.available()  # is Docker really there? (checked before the tests replace it)


def override():
    s = dbmod.SessionLocal()
    try:
        yield s
    finally:
        s.close()


app.dependency_overrides[get_db] = override
hr = TestClient(app)  # the HR browser: reaches the app on this computer
pub = TestClient(app, base_url="https://demo.ngrok-free.app")  # a candidate on the public address

s = dbmod.SessionLocal()
people = {}
for key, name, email, dnc, ref in [("a", "Asha Rao", "asha@example.com", False, "C-1"), ("b", "Bilal Khan", "Bilal@Example.com", False, "C-2"),
                                   ("c", "Chitra Das", "chitra@example.com", False, "C-3"), ("d", "Dev Patel", "dev@example.com", False, "C-4"),
                                   ("noemail", "No Email", None, False, None), ("dnc", "Do Not", "dnc@example.com", True, None)]:
    c = Candidate(name=name, email=email, do_not_contact=dnc, candidate_ref=ref)
    s.add(c)
    s.flush()
    people[key] = c.id
s.commit()
s.close()


def auth(token):
    return {"Authorization": "Bearer " + token}


def login(email, pw):
    return pub.post("/api/exam/login", json={"email": email, "password": pw})


def attempt_row(email):
    return next(r for r in hr.get(f"/api/exams/{EX}/results").json()["rows"] if r["email"] == email)


def wait_graded(invite_id):
    for _ in range(80):
        if hr.get(f"/api/exams/invites/{invite_id}").json()["grading"] == "done":
            return
        time.sleep(0.1)
    raise AssertionError("coding was not marked in time")


# ---------------------------------------------------------------- HR builds a test
MCQ1 = {"kind": "mcq", "text": "2 + 2 = ?", "marks": 2, "options": ["3", "4", "5"], "correct": [1]}
MCQ2 = {"kind": "mcq", "text": "Pick the primes", "marks": 4, "options": ["2", "3", "4", "9"], "correct": [0, 1]}
CODE = {"kind": "coding", "text": "Print the sum of two numbers", "marks": 10, "starter_code": "# go",
        "test_cases": [{"input": "1 2", "expected": "3", "hidden": False}, {"input": "5 5", "expected": "10", "hidden": True}]}
body = {"title": "AI Intern Hackathon", "instructions": "Be honest.", "duration_minutes": 30, "negative_marking": 0.5, "max_violations": 2,
        "show_score": True, "questions": [MCQ1, MCQ2, CODE]}

assert hr.post("/api/exams", json=dict(body, questions=[{**MCQ1, "correct": []}])).status_code == 400
bad = dict(body, questions=[{**CODE, "test_cases": [{"input": "1", "expected": "1", "hidden": True}]}])
assert "sample" in hr.post("/api/exams", json=bad).json()["detail"]
assert hr.post("/api/exams", json=dict(body, duration_minutes=1)).status_code == 400
made = hr.post("/api/exams", json=body)
assert made.status_code == 200, made.text
EX = made.json()["id"]
assert made.json()["question_count"] == 3 and made.json()["total_marks"] == 16
print("OK: HR builds a test; wrong setups are refused with a clear message")

# ---------------------------------------------------------------- invites
r = hr.post(f"/api/exams/{EX}/invite", json={"candidate_ids": [people[k] for k in ("a", "b", "c", "d", "noemail", "dnc")]}).json()
assert {x["name"] for x in r["created"]} == {"Asha Rao", "Bilal Khan", "Chitra Das", "Dev Patel"}, r
assert {x["name"]: x["reason"] for x in r["skipped"]} == {"No Email": "No email address", "Do Not": "Marked do not contact"}
assert r["exam_url"] == "https://demo.ngrok-free.app/exam"
PW = {x["candidate_id"]: x["password"] for x in r["created"]}
INV = {x["candidate_id"]: x["invite_id"] for x in r["created"]}
assert all(len(p) == 11 and "-" in p for p in PW.values()) and len(set(PW.values())) == 4  # a different password for everyone
assert next(x for x in r["created"] if x["candidate_id"] == people["b"])["email"] == "bilal@example.com"
assert next(x for x in r["created"] if x["candidate_id"] == people["a"])["candidate_ref"] == "C-1"
again = hr.post(f"/api/exams/{EX}/invite", json={"candidate_ids": [people["a"]]}).json()
assert again["created"] == [] and again["existing"], again  # no second password for the same person
s = dbmod.SessionLocal()
inv = s.get(ExamInvite, INV[people["a"]])
assert PW[people["a"]] not in (inv.pw_hash, inv.salt) and len(inv.pw_hash) == 64  # only a scrambled copy is stored
s.close()
print("OK: a login per candidate; no email / do-not-contact are skipped; passwords are stored scrambled")

# ---------------------------------------------------------------- invitation emails
from app import mailer  # noqa: E402
assert r["email_enabled"] is False and not any(x["email_sent"] for x in r["created"])  # no SMTP set: passwords only on screen
os.environ.update({"SMTP_HOST": "smtp.example.com", "SMTP_USER": "hr@example.com", "SMTP_PASSWORD": "x", "SMTP_FROM_NAME": "Kripa HR"})
sent_mail, real_send = [], mailer._send
mailer._send = lambda m: sent_mail.append(m)
s = dbmod.SessionLocal()
for nm, em in (("Mail One", "mail1@example.com"), ("Mail Two", "mail2@example.com")):
    c = Candidate(name=nm, email=em)
    s.add(c)
    s.flush()
    people[nm] = c.id
s.commit()
s.close()
m = hr.post(f"/api/exams/{EX}/invite", json={"candidate_ids": [people["Mail One"]]}).json()
row = m["created"][0]
assert m["email_enabled"] and row["email_sent"] and len(sent_mail) == 1, m
mail_text = sent_mail[0].get_content()
assert sent_mail[0]["To"] == "mail1@example.com" and "https://demo.ngrok-free.app/exam" in mail_text
assert row["password"] in mail_text and "mail1@example.com" in mail_text and "Mail" in mail_text
print("OK: the invitation email carries the link, the login email and the password")


def boom(_m):
    raise ConnectionRefusedError("smtp down")


mailer._send = boom
m2 = hr.post(f"/api/exams/{EX}/invite", json={"candidate_ids": [people["Mail Two"]]}).json()
assert len(m2["created"]) == 1 and not m2["created"][0]["email_sent"] and "ConnectionRefusedError" in m2["created"][0]["email_error"]
assert m2["created"][0]["password"]  # HR can still hand it over
mailer._send = lambda m: sent_mail.append(m)
rp = hr.post(f"/api/exams/invites/{m2['created'][0]['invite_id']}/reset-password").json()
assert rp["email_sent"] and rp["password"] in sent_mail[-1].get_content() and rp["password"] != m2["created"][0]["password"]
mailer._send = real_send
for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM_NAME"):
    os.environ.pop(k)
print("OK: a failed email never blocks the invite; reset password emails the new one")

# ---------------------------------------------------------------- what the public address can and cannot reach
page = pub.get("/exam")
assert page.status_code == 200 and "Online test" in page.text and page.headers["cache-control"] == "no-store"
for path in ("/api/exams", f"/api/exams/{EX}", f"/api/exams/{EX}/results", "/api/candidates/1", "/api/search", "/api/calls", "/api/facets"):
    assert pub.get(path).status_code == 403, path
assert pub.post(f"/api/exams/{EX}/invite", json={"candidate_ids": [1]}).status_code == 403
assert pub.get("/api/exam/me").status_code == 401  # no login, nothing
print("OK: the public address serves only the test page and the candidate endpoints; every HR screen is refused")

# ---------------------------------------------------------------- login rules
ex._fails.clear()
assert login("asha@example.com", "wrong").status_code == 401
assert login("nobody@example.com", "x").status_code == 401
assert login("asha@example.com", PW[people["b"]]).status_code == 401  # someone else's password
for _ in range(4):
    login("asha@example.com", "wrong")
locked = login("asha@example.com", PW[people["a"]])
assert locked.status_code == 429 and "minute" in locked.json()["detail"]  # locked even for the right password
ex._fails.clear()
ok = login("ASHA@example.com", PW[people["a"]] + " ")  # email capitals and stray spaces do not matter
assert ok.status_code == 200, ok.text
TA = ok.json()["token"]
A = auth(TA)
assert ok.json()["exam"]["status"] == "invited" and ok.json()["exam"]["first_name"] == "Asha"
assert "correct" not in str(ok.json()) and "questions" not in ok.json()["exam"]
assert pub.get("/api/exam/me", headers=auth(TA[:-3] + "abc")).status_code == 401  # forged session
print("OK: wrong passwords refused, five misses lock the account, forged sessions refused")

# ---------------------------------------------------------------- taking the test
assert pub.put("/api/exam/answer", headers=A, json={"question_id": 1, "choice": [0]}).status_code == 409  # not started yet
st = pub.post("/api/exam/start", headers=A).json()
qs = st["questions"]
assert [q["kind"] for q in qs] == ["mcq", "mcq", "coding"] and st["exam"]["status"] == "in_progress"
dump = str(st)
assert "correct" not in dump and "hidden" not in dump and "5 5" not in dump, "answers or hidden tests leaked to the candidate"
assert qs[1]["multiple"] is True and qs[0]["multiple"] is False and len(qs[2]["samples"]) == 1
ends = datetime.fromisoformat(st["exam"]["ends_at"].rstrip("Z"))
assert 29 * 60 < (ends - datetime.utcnow()).total_seconds() <= 30 * 60  # the clock is the server's
q1, q2, qc = (q["id"] for q in qs)
assert pub.put("/api/exam/answer", headers=A, json={"question_id": q1, "choice": [1]}).status_code == 200
assert pub.put("/api/exam/answer", headers=A, json={"question_id": q2, "choice": [0, 1]}).status_code == 200
assert pub.put("/api/exam/answer", headers=A, json={"question_id": qc, "code": "a,b=map(int,input().split());print(a+b)"}).status_code == 200
assert pub.put("/api/exam/answer", headers=A, json={"question_id": 99999, "choice": [0]}).status_code == 404
reopen = pub.post("/api/exam/start", headers=A).json()  # reopening continues; the clock is never restarted
assert reopen["answers"][str(q1)] == {"choice": [1]} and reopen["exam"]["ends_at"] == st["exam"]["ends_at"]
print("OK: starts on the server clock, hides answers and hidden tests, saves answers, resumes without a new clock")

# ---------------------------------------------------------------- the rules: warnings, then the test ends
v1 = pub.post("/api/exam/violation", headers=A, json={"kind": "tab_hidden"}).json()
assert v1["count"] == 1 and v1["max"] == 2 and v1["status"] == "in_progress"
dup = pub.post("/api/exam/violation", headers=A, json={"kind": "window_blur"}).json()
assert dup["ignored"] is True and dup["count"] == 1  # one action raises several browser signals: counted once
ex.VIOLATION_GAP = 0
v2 = pub.post("/api/exam/violation", headers=A, json={"kind": "fullscreen_exit"}).json()
assert v2["count"] == 2 and v2["status"] == "in_progress"  # the final warning; still running
v3 = pub.post("/api/exam/violation", headers=A, json={"kind": "tab_hidden"}).json()
assert v3["count"] == 3 and v3["status"] == "terminated"  # more than 2: the test ends
me = pub.get("/api/exam/me", headers=A).json()
assert me["status"] == "terminated" and me["violation_count"] == 3
assert pub.put("/api/exam/answer", headers=A, json={"question_id": q1, "choice": [0]}).status_code == 409  # locked
print("OK: two warnings, the third violation ends the test; one action is not double-counted")

s = dbmod.SessionLocal()
inv = s.get(ExamInvite, INV[people["a"]])
assert inv.status == "terminated" and inv.submitted_at and [v["kind"] for v in inv.violations] == ["tab_hidden", "fullscreen_exit", "tab_hidden"]
assert inv.mcq_score == 6.0 and inv.grading == "manual"  # answers so far graded; no code runner is running here
assert s.get(Candidate, people["a"]).status == "Test submitted"
s.close()
print("OK: an ended test is auto-submitted and graded, and the candidate moves to 'Test submitted'")

# HR marks the coding answer by hand when there is no runner
d = hr.patch(f"/api/exams/invites/{INV[people['a']]}", json={"manual_coding": {str(qc): 7.5}}).json()
assert d["coding_score"] == 7.5 and d["total_score"] == 13.5 and d["grading"] == "done", d
assert hr.patch(f"/api/exams/invites/{INV[people['a']]}", json={"manual_coding": {str(qc): 11}}).status_code == 400
print("OK: HR can mark coding answers by hand (limited to the question's marks)")

# ---------------------------------------------------------------- submitting, negative marking, coding by the runner
ex.VIOLATION_GAP = 3.0
TB = login("bilal@example.com", PW[people["b"]]).json()["token"]
B = auth(TB)
b1, b2, bc = (q["id"] for q in pub.post("/api/exam/start", headers=B).json()["questions"])
pub.put("/api/exam/answer", headers=B, json={"question_id": b1, "choice": [2]})      # wrong: loses half of 2 marks
pub.put("/api/exam/answer", headers=B, json={"question_id": b2, "choice": [0, 1]})   # right: 4 marks
pub.put("/api/exam/answer", headers=B, json={"question_id": bc, "code": "print(3)"})
coderun.available = lambda: True
ran = []


def fake_cases(code, cases):
    ran.append([c["input"] for c in cases])
    return [{"passed": i == 0, "got": "3", "error": "", "timeout": False} for i, _ in enumerate(cases)]  # passes the sample, fails the hidden one


coderun.run_cases = fake_cases
done = pub.post("/api/exam/submit", headers=B).json()
assert done["status"] == "submitted"
wait_graded(INV[people["b"]])
det = hr.get(f"/api/exams/invites/{INV[people['b']]}").json()
assert det["mcq_score"] == 3.0 and det["coding_score"] == 5.0 and det["total_score"] == 8.0 and det["max_score"] == 16.0, det
assert ran == [["1 2", "5 5"]], ran  # the hidden case is used for marking
assert pub.get("/api/exam/me", headers=B).json()["score"] == {"total": 8.0, "max": 16.0}  # this test shows scores
print("OK: submit grades MCQs with negative marking and coding on all test cases, hidden ones included")

# ---------------------------------------------------------------- password reset, then the clock runs out
TC = login("chitra@example.com", PW[people["c"]]).json()["token"]
new = hr.post(f"/api/exams/invites/{INV[people['c']]}/reset-password").json()
assert new["password"] != PW[people["c"]]
assert pub.get("/api/exam/me", headers=auth(TC)).status_code == 401  # the old session ended
assert login("chitra@example.com", PW[people["c"]]).status_code == 401  # the old password stopped working
TC = login("chitra@example.com", new["password"]).json()["token"]
pub.post("/api/exam/start", headers=auth(TC))
s = dbmod.SessionLocal()
inv = s.get(ExamInvite, INV[people["c"]])
inv.ends_at = datetime.utcnow() - timedelta(seconds=5)  # time ran out
s.commit()
s.close()
late = pub.get("/api/exam/me", headers=auth(TC)).json()
assert late["status"] == "timed_out", late
wait_graded(INV[people["c"]])
print("OK: a password reset ends old sessions; when time runs out the test is submitted automatically")

# the background sweep submits tests even when the candidate closed the browser
TD = login("dev@example.com", PW[people["d"]]).json()["token"]
pub.post("/api/exam/start", headers=auth(TD))
s = dbmod.SessionLocal()
inv = s.get(ExamInvite, INV[people["d"]])
inv.ends_at = datetime.utcnow() - timedelta(seconds=1)
s.commit()
assert ex.sweep(s) == 1 and s.get(ExamInvite, INV[people["d"]]).status == "timed_out"
s.close()
wait_graded(INV[people["d"]])
print("OK: the scheduler's sweep auto-submits an overdue test even if the browser was closed")

# ---------------------------------------------------------------- results, export, edits
res = hr.get(f"/api/exams/{EX}/results").json()
ranked = [(r["name"], r["rank"], r["total_score"]) for r in res["rows"] if r.get("rank")]
assert ranked[0] == ("Asha Rao", 1, 13.5) and ranked[1] == ("Bilal Khan", 2, 8.0), ranked
assert res["exam"]["invited"] == 6 and res["exam"]["finished"] == 4
hr.patch(f"/api/exams/invites/{INV[people['a']]}", json={"voided": True})  # e.g. a notification caused the warnings
assert attempt_row("asha@example.com")["voided"] is True and "rank" not in attempt_row("asha@example.com")
assert attempt_row("bilal@example.com")["rank"] == 1
xl = hr.get(f"/api/exams/{EX}/export")
sheet = load_workbook(io.BytesIO(xl.content)).active
head = [c.value for c in sheet[1]]
assert head[0] == "Candidate ID" and "Violations" in head and "Total" in head and {"C-1", "C-2"} <= {str(c.value) for c in sheet["A"]}
assert hr.get(f"/api/exams/{EX}/export?format=csv").content.startswith(b"\xef\xbb\xbfCandidate ID")
assert pub.get("/api/exam/me", headers=B).json().get("score") == {"total": 8.0, "max": 16.0}
print("OK: ranked results, voiding, Excel/CSV export with Candidate ID first")

# ---------------------------------------------------------------- editing a test people already took
full = hr.get(f"/api/exams/{EX}").json()
ids_before = [q["id"] for q in full["questions"]]
edited = dict(full, title="Renamed test")
edited["questions"] = [dict(q) for q in full["questions"]]
edited["questions"][1]["marks"] = 6  # the primes question is now worth 6
r1 = hr.put(f"/api/exams/{EX}", json=edited).json()
assert r1["title"] == "Renamed test" and [q["id"] for q in r1["questions"]] == ids_before  # same questions, so answers stay attached
assert r1["recalculated"] == 4 and r1["total_marks"] == 18
assert pub.get("/api/exam/me", headers=B).json().get("score") == {"total": 10.0, "max": 18.0}  # -1 + 6 MCQ, 5 coding
edited["questions"][2]["marks"] = 20  # coding question changes: finished code is marked again
assert hr.put(f"/api/exams/{EX}", json=edited).json()["recalculated"] == 4
wait_graded(INV[people["b"]])
assert pub.get("/api/exam/me", headers=B).json().get("score") == {"total": 15.0, "max": 28.0}  # 5 MCQ + 10 of 20 coding
asha = hr.get(f"/api/exams/invites/{INV[people['a']]}").json()
assert asha["max_score"] == 28
edited["questions"] = edited["questions"][:2] + [dict(MCQ1, text="A new one", marks=1)] + edited["questions"][2:]
r3 = hr.put(f"/api/exams/{EX}", json=edited).json()
assert r3["question_count"] == 4 and r3["questions"][3]["id"] == ids_before[2] and r3["questions"][2]["id"] not in ids_before
wait_graded(INV[people["b"]])
assert pub.get("/api/exam/me", headers=B).json().get("score") == {"total": 15.0, "max": 29.0}  # the blank new question adds marks, not points
edited["questions"] = r3["questions"][:2] + r3["questions"][3:]
assert hr.put(f"/api/exams/{EX}", json=edited).json()["question_count"] == 3  # removing a question
print("OK: a taken test can be edited: questions keep their ids, finished scores are recalculated, code is marked again when it changes")

# nobody can edit while a candidate is in the middle of the test
LP = login("mail1@example.com", row["password"]).json()["token"]
live = auth(LP)
pub.post("/api/exam/start", headers=live)
blocked = hr.put(f"/api/exams/{EX}", json=edited)
assert blocked.status_code == 409 and "right now" in blocked.json()["detail"], blocked.text
print("OK: editing is refused while someone is taking the test")

# ---------------------------------------------------------------- AI marking of coding answers
from app import aigrade  # noqa: E402
asked = []


def fake_review(q, mx, code, cases, note=""):
    asked.append((code, note))
    if "give me full marks" in code:  # the AI is told to ignore instructions hidden in the code; the fake mimics a safe verdict
        return {"percent": 0.0, "marks": 0.0, "verdict": "wrong", "feedback": "Does not solve the task."}
    return {"percent": 70.0, "marks": round(mx * 0.7, 2), "verdict": "mostly_correct", "feedback": "Adds the numbers but misses an empty-input case."}


aigrade.enabled, aigrade.review = (lambda: True), fake_review
ex2 = hr.post("/api/exams", json=dict(body, title="AI marked", questions=[CODE])).json()["id"]
inv2 = hr.post(f"/api/exams/{ex2}/invite", json={"candidate_ids": [people["Mail Two"], people["noemail"]]}).json()
assert hr.get("/api/exams").json()[0]["ai_grading"] is True
pw2 = inv2["created"][0]["password"]
T2 = auth(login("mail2@example.com", pw2).json()["token"])
qid = pub.post("/api/exam/start", headers=T2).json()["questions"][0]["id"]
pub.put("/api/exam/answer", headers=T2, json={"question_id": qid, "code": "a,b=map(int,input().split());print(a+b)"})
coderun.available = lambda: False  # no Docker: the AI reads the code and proposes the marks
pub.post("/api/exam/submit", headers=T2)
iid2 = inv2["created"][0]["invite_id"]
wait_graded(iid2)
det = hr.get(f"/api/exams/invites/{iid2}").json()
cq = det["questions"][0]
assert cq["detail"]["by"] == "ai" and cq["awarded"] == 7.0 and "empty-input" in cq["detail"]["ai"]["feedback"], cq
assert det["coding_score"] == 7.0 and det["total_score"] == 7.0 and det["grading"] == "done"
assert hr.patch(f"/api/exams/invites/{iid2}", json={"manual_coding": {str(qid): 9}}).json()["coding_score"] == 9.0  # HR can overrule the AI

def broken(*a, **k):
    from app.llm import LLMUnavailable
    raise LLMUnavailable("down")


aigrade.review = broken  # AI fails: the answer waits for HR instead of getting a made-up mark
inv3 = hr.post(f"/api/exams/{ex2}/invite", json={"candidate_ids": [people["Mail One"]]}).json()["created"][0]
T3 = auth(login("mail1@example.com", inv3["password"]).json()["token"])
q3 = pub.post("/api/exam/start", headers=T3).json()["questions"][0]["id"]
pub.put("/api/exam/answer", headers=T3, json={"question_id": q3, "code": "print(1)"})
pub.post("/api/exam/submit", headers=T3)
for _ in range(80):
    if hr.get(f"/api/exams/invites/{inv3['invite_id']}").json()["grading"] == "manual":
        break
    time.sleep(0.1)
d3 = hr.get(f"/api/exams/invites/{inv3['invite_id']}").json()
assert d3["grading"] == "manual" and d3["coding_score"] is None and d3["questions"][0]["detail"] is None
coderun.available = lambda: True  # Docker on: the tests decide the marks, the AI only adds feedback
aigrade.review = fake_review
print("OK: coding answers are marked by AI when Docker is off, HR can overrule, and an AI failure leaves it for manual marking")

# ---------------------------------------------------------------- deleting a test at any stage
live_gone = hr.delete(f"/api/exams/{EX}")  # a test with finished attempts and one candidate mid-test
assert live_gone.status_code == 200
assert all(e["id"] != EX for e in hr.get("/api/exams").json()) and hr.get(f"/api/exams/{EX}").status_code == 404
assert pub.get("/api/exam/me", headers=live).status_code == 401  # the candidate who was mid-test is logged out
assert login("bilal@example.com", PW[people["b"]]).status_code == 401  # logins of the deleted test no longer work
assert hr.delete(f"/api/exams/{ex2}").status_code == 200
print("OK: a test can be deleted at any stage, with its attempts and logins")

# ---------------------------------------------------------------- the real code runner (only when Docker is running)
if REAL_RUNNER:
    from importlib import reload
    reload(coderun)
    ok = coderun.run_python("a,b=map(int,input().split())\nprint(a+b)", "1 2")
    assert ok["ok"] and ok["stdout"].strip() == "3", ok
    assert coderun.run_python("while True: pass")["timeout"] is True
    net = coderun.run_python("import urllib.request\nurllib.request.urlopen('http://example.com', timeout=3)")
    assert net["ok"] is False  # no network inside the box
    print("OK: real Docker runner: runs code, stops infinite loops, has no network")
else:
    print("SKIPPED: real Docker runner (Docker Desktop is not running)")

print("\nALL EXAM CHECKS PASSED")
