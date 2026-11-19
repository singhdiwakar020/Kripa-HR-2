"""Runs the Stage 1 flow end to end against a throwaway database."""
import os
import sys
import tempfile
from pathlib import Path

import app.db as dbmod
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

tmp = Path(tempfile.mkdtemp()) / "t.db"
dbmod.engine = create_engine(f"sqlite:///{tmp}", connect_args={"check_same_thread": False})
dbmod.SessionLocal = sessionmaker(bind=dbmod.engine, autoflush=False, expire_on_commit=False)

from fastapi.testclient import TestClient  # noqa: E402
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

# Stand-in for OpenAI so this check does not need a key or a network call.
from app import llm  # noqa: E402


def fake_parse(q, roles, locations):
    ql = q.lower()
    f = {}
    if "sales" in ql:
        f["role"] = "Sales Intern"
    elif "intern" in ql:
        f["role"] = "AI Developer Intern"
    elif "ai developer" in ql:
        f["role"] = "AI Developer"
    if "bhopal" in ql:
        f["location"] = "Bhopal"
    if "pune" in ql:
        f["location"] = "Pune"
    if "7 or more" in ql:
        f["min_score"] = 7.0
    if "above 6" in ql:
        f["min_score"] = 6.01
    if "still new" in ql:
        f["status"] = "In database"
    if "fresher" in ql:
        f["max_experience"] = 0.99
    if "30 days" in ql:
        f["max_notice_days"] = 30
    if "relocate" in ql:
        f["ready_to_relocate"] = True
    if "python" in ql:
        f["keywords"] = ["python", "rag"]
    return f


def fake_match(request, skills, profiles):
    out = []
    for p in profiles:
        text = f"{p.get('reasoning') or ''} {p.get('experience') or ''}".lower()
        if all(any(bit in text for bit in s.split("|")) for s in skills):
            out.append({"id": p["id"], "reason": "profile supports " + ", ".join(s.split("|")[0] for s in skills)})
    return out


llm.parse_with_llm = fake_parse
llm.match_with_llm = fake_match
c = TestClient(app)

with open("sample_career_form.xlsx", "rb") as fh:
    r = c.post("/api/imports", files={"file": ("sample_career_form.xlsx", fh)})
assert r.status_code == 200, r.text
p = r.json()
print("PREVIEW counts:", p["counts"], "\nissues:", p["issue_summary"], "\nunmatched:", p["unmatched_columns"])
print("mapping:", {k: v for k, v in p["mapping"].items()})
assert p["counts"]["merged"] == 1 and p["counts"]["new"] == 26, p["counts"]

r = c.post(f"/api/imports/{p['batch_id']}/confirm")
print("CONFIRM:", r.json())

# re-upload: nothing new, nothing overwritten
c.post("/api/candidates/1/do-not-contact", json={"value": True})
with open("sample_career_form.xlsx", "rb") as fh:
    p2 = c.post("/api/imports", files={"file": ("again.xlsx", fh)}).json()
print("RE-UPLOAD counts:", p2["counts"])
assert p2["counts"]["new"] == 0

for q in ["AI developer interns in Bhopal with a score of 7 or more who are still New",
          "python rag interns score above 6", "freshers in Pune", "Sales intern",
          "AI developer under 30 days notice ready to relocate"]:
    s = c.get("/api/search", params={"q": q}).json()
    print(f"\nQ: {q}\n  filters={s['filters']}\n  total={s['total']}", [x["name"] for x in s["results"][:3]])
    if s["results"]:
        print("  why:", s["results"][0]["match"])
    print("  suggestions:", [x["label"] for x in s["suggestions"]])

all_ids = [x["id"] for x in c.get("/api/search", params={"limit": 200}).json()["results"]]
assert 1 not in all_ids, "do-not-contact candidate leaked into search"
x = c.get("/api/export")
assert x.status_code == 200 and len(x.content) > 1000
print("\nOK: dnc hidden, export", len(x.content), "bytes")

# ---- flexible name / text search (the fake parser returns no filters for these, so the plain-text path is used)
def names(q, **kw):
    s = c.get("/api/search", params={"q": q, **kw}).json()
    return [x["name"] for x in s["results"]], s


for q, want in [("sneha", "Sneha Sharma"), ("Sneh", "Sneha Sharma"), ("sharma sneha", "Sneha Sharma"),
                ("search for the name SNEHA sharma", "Sneha Sharma")]:
    got, s = names(q)
    assert got and got[0] == want, (q, got[:3])
got, s = names("snehaa sharmaa")  # typo
assert "Sneha Sharma" in got and s["notice"] and "similar" in s["notice"], (got[:3], s["notice"])
assert "Nikhil Sharma" in names("", name="nikhil")[0]
assert names("zzzqq")[0] == []

dnc_name = c.get("/api/candidates/1").json()["name"]
assert dnc_name not in names(dnc_name.split()[0])[0] or names(dnc_name)[0].count(dnc_name) == 0, "do-not-contact leaked"

real_parse = llm.parse_with_llm


def broken(*a, **k):
    raise llm.LLMUnavailable("no key")


llm.parse_with_llm = broken  # the AI is down: name search must still work
got, s = names("sneha")
assert got[0] == "Sneha Sharma" and s["notice"], (got[:3], s["notice"])
llm.parse_with_llm = real_parse
print("OK: flexible search (partial, any order, typos, details, works without AI, hides do-not-contact)")

# ---- phone numbers in the formats found in the real "Contact Details" column
from app import importer as imp  # noqa: E402

phones = {
    "+91-9630677877": "9630677877", "+91 9885228387": "9885228387", "9827526103": "9827526103",
    "+91 74708 60193": "7470860193", "+919550518316": "9550518316", "81228 27934": "8122827934",
    "+91-7869875252": "7869875252", "098765 43210": "9876543210", "91 98765 43210": "9876543210",
    "Call 98765-43210 after 5pm": "9876543210", "9876543210 / 8765432109": "9876543210",
    "+2330242755791": "+2330242755791",  # Ghana: kept with its country code
    "": None, "abc": None, "12345": None,
}
for raw, want in phones.items():
    assert imp.norm_phone(raw) == want, (raw, imp.norm_phone(raw), want)
assert imp.is_dialable("9630677877") and not imp.is_dialable("+2330242755791") and not imp.is_dialable(None)
hdrs = ["Name", "Email ID", "Contact Details", "Best Match JD", "Current/Recent Company", "Salary in Hand (per month)"]
m = imp.auto_map(hdrs)
assert m["Contact Details"] == "phone" and m["Best Match JD"] == "best_role", m
assert m["Current/Recent Company"] == "company" and m["Salary in Hand (per month)"] == "salary_in_hand", m
print("OK: phone numbers read from 'Contact Details' in every format, foreign numbers kept but not dialled")

# ---- HR edits a phone number in the app
ids = [x["id"] for x in c.get("/api/search", params={"limit": 3}).json()["results"]]
p1, p2 = ids[0], ids[1]
r = c.patch(f"/api/candidates/{p1}", json={"phone": "+91 98765-43210"})
assert r.status_code == 200 and r.json()["phone"] == "9876543210", r.text
assert c.get(f"/api/candidates/{p1}").json()["phone"] == "9876543210"
assert c.patch(f"/api/candidates/{p1}", json={"phone": "abc"}).status_code == 400
assert c.patch(f"/api/candidates/{p1}", json={"phone": "12345"}).status_code == 400
dup = c.patch(f"/api/candidates/{p2}", json={"phone": "9876543210"})
assert dup.status_code == 400 and "already has" in dup.json()["detail"], dup.text
assert c.patch(f"/api/candidates/{p1}", json={"phone": "9876543210"}).status_code == 200  # own number again is fine
assert c.patch("/api/candidates/999999", json={"phone": "9876543210"}).status_code == 404
print("OK: phone can be corrected in the app (formats cleaned, bad and duplicate numbers refused)")

# ---- clear all data: needs DELETE, backs up, keeps do-not-contact people blocked after a re-upload
import app.main as appmain  # noqa: E402
from pathlib import Path as _P  # noqa: E402
import shutil, tempfile as _t  # noqa: E402
appmain.dbmod.DB_PATH = _P(_t.mkdtemp()) / "t.db"
shutil.copy(tmp, appmain.dbmod.DB_PATH)
_s = dbmod.SessionLocal()
from app import calls as _calls  # noqa: E402
_calls.seed_defaults(_s)  # normally done at app startup
_s.close()
assert c.post("/api/reset", json={"confirm": "nope"}).status_code == 400
dnc_before = c.get("/api/search", params={"limit": 1}).json()["total"]
dnc_id = c.get("/api/search", params={"limit": 1}).json()["results"][0]["id"]
dnc_email = c.get(f"/api/candidates/{dnc_id}").json()["email"]
c.post(f"/api/candidates/{dnc_id}/do-not-contact", json={"value": True})
r = c.post("/api/reset", json={"confirm": "DELETE"})
assert r.status_code == 200 and r.json()["removed"] > 0 and r.json()["do_not_contact_kept"] >= 1, r.text
assert (appmain.dbmod.DB_PATH.parent / "backups" / r.json()["backup"]).exists()
assert c.get("/api/facets").json()["total"] == 0 and c.get("/api/calls").json()["jobs"] == []
assert len(c.get("/api/question-sets").json()) >= 1, "question sets must survive"
with open("sample_career_form.xlsx", "rb") as fh:
    p = c.post("/api/imports", files={"file": ("s.xlsx", fh)}).json()
c.post(f"/api/imports/{p['batch_id']}/confirm")
back = [x for x in c.get("/api/search", params={"limit": 200}).json()["results"] if x["email"] == dnc_email]
assert back == [], "do-not-contact person must stay blocked after re-upload"
assert c.get("/api/facets").json()["total"] >= 1
print("OK: clear all data works, backs up, keeps question sets and the do-not-contact block")

# ---- search improvements: role families, unknown experience, helpful suggestions when nothing matches
exact = c.get("/api/search", params={"role": "AI Developer", "limit": 200}).json()
family = c.get("/api/search", params={"role": "AI Developer", "role_match": "family", "limit": 200}).json()
assert family["total"] > exact["total"] and any(x["best_role"] == "AI Developer Intern" for x in family["results"]), (exact["total"], family["total"])
assert all("AI Developer" in x["best_role"] for x in family["results"])
assert all(x["best_role"] == "AI Developer" for x in exact["results"])  # the dropdown still means exactly that role
q = c.get("/api/search", params={"q": "ai developer under 1 year", "limit": 200}).json()  # fake parser: role AI Developer (+ no years)
assert q["filters"].get("role_match") == "family", q["filters"]

# a person whose experience is not stated must still appear for "less than 1 year", and not for "3+ years"
s_ = dbmod.SessionLocal()
from app.models import Candidate as _C  # noqa: E402
unk = s_.query(_C).filter(_C.best_role == "AI Developer Intern", _C.do_not_contact.is_(False)).first()
unk.experience_years, unk.experience_text = None, "[2 internships: AI, Machine Learning]"
s_.commit()
uid, uname = unk.id, unk.name
s_.close()
lt1 = c.get("/api/search", params={"max_experience": 0.99, "role": "AI Developer Intern", "limit": 200}).json()
hit = [x for x in lt1["results"] if x["id"] == uid]
assert hit and any("experience not stated" in m for m in hit[0]["match"]), lt1["total"]
ge3 = c.get("/api/search", params={"min_experience": 3, "limit": 200}).json()
assert uid not in [x["id"] for x in ge3["results"]]

# nothing matches: the suggestions say which single condition to remove
none = c.get("/api/search", params={"role": "Sales Intern", "location": "Zzzville", "limit": 5}).json()
assert none["total"] == 0 and any(x["patch"] == {"location": None} for x in none["suggestions"]), none["suggestions"]
assert importer_ok if (importer_ok := (imp.parse_years("[No paid jobs or internships listed]") == 0.0)) else False
assert imp.parse_years("[2 internships: AI, Machine Learning]") is None and imp.parse_years("9 mo [AI intern]") == 0.75
print("OK: 'AI developer' includes interns, unstated experience is kept and flagged, empty results suggest what to remove")

# ---- the guard that removes filters the question never asked for
_roles = ["AI Developer", "AI Developer Intern", "Digital Marketing Intern", "Sales Intern"]
_g = llm.guard_filters
assert _g("fresher AI developer bhopal", {"role": "AI Developer", "status": "In database", "location": "Bhopal"}, _roles) == {"role": "AI Developer", "location": "Bhopal"}
assert _g("freshers in Pune", {"role": "AI Developer Intern", "status": "In database", "location": "Pune"}, _roles) == {"location": "Pune"}
assert _g("not yet contacted AI interns", {"role": "AI Developer Intern", "status": "In database"}, _roles)["status"] == "In database"
assert _g("people we already called", {"status": "Called"}, _roles)["status"] == "Called"
assert _g("digital marketing interns", {"role": "Digital Marketing Intern"}, _roles)["role"] == "Digital Marketing Intern"
assert "role" not in _g("people in Bhopal", {"role": "Sales Intern", "location": "Bhopal"}, _roles)
print("OK: made-up statuses and roles are dropped, real ones kept")
assert _g("fresher AI developer bhopal", {"role": "AI Developer Intern"}, _roles)["role"] == "AI Developer"
assert _g("AI intern in Bhopal", {"role": "AI Developer Intern"}, _roles)["role"] == "AI Developer Intern"
assert c.get("/api/search", params={"min_experience": 0, "limit": 200}).json()["total"] == c.get("/api/search", params={"limit": 200}).json()["total"]
print("OK: 'intern' only kept when asked for; a minimum of 0 years is ignored")

# ---- Candidate ID column: it decides who is the same person, in the file and against what is already stored
import io  # noqa: E402
from openpyxl import Workbook, load_workbook  # noqa: E402
from app.models import Candidate  # noqa: E402


def upload_rows(rows):
    wb = Workbook()
    ws = wb.active
    ws.append(["Candidate ID", "Name", "Email ID", "Contact Details", "Best Match JD", "Screening Score"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    p = c.post("/api/imports", files={"file": ("ids.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}).json()
    assert p["mapping"]["Candidate ID"] == "candidate_ref", p["mapping"]
    done = c.post(f"/api/imports/{p['batch_id']}/confirm").json()
    return p, done


p, done = upload_rows([
    (9001.0, "Idris Test", "idris@example.com", "9876500001", "AI Developer", 7),
    ("9001", "Idris Test", "idris.second@example.com", "9876500002", None, None),   # same ID: the same person, merged
    ("9002", "Another Person", "idris@example.com", "9876500003", "AI Developer", 6),  # same email, other ID: another person
])
s = dbmod.SessionLocal()
mine = {x.candidate_ref: x for x in s.query(Candidate).filter(Candidate.candidate_ref.in_(["9001", "9002"]))}
assert set(mine) == {"9001", "9002"} and done["added"] == 2 and done["merged"] == 1, (done, set(mine))
assert mine["9001"].name == "Idris Test" and mine["9002"].name == "Another Person"
assert mine["9001"].id != mine["9002"].id
s.close()

p, done = upload_rows([("9001", "Idris Renamed", "idris.new@example.com", "9876500009", "AI Developer", 8)])
s = dbmod.SessionLocal()
again = s.query(Candidate).filter(Candidate.candidate_ref == "9001").all()
assert len(again) == 1 and done["updated"] == 1 and done["added"] == 0, (done, len(again))
assert again[0].id == mine["9001"].id and again[0].name == "Idris Renamed" and again[0].email == "idris.new@example.com"
s.close()
p, done = upload_rows([("9003", "Third Person", "idris.new@example.com", "9876500077", "AI Developer", 5)])  # an email already stored under 9001
s = dbmod.SessionLocal()
third = s.query(Candidate).filter(Candidate.candidate_ref == "9003").one()
assert third.id != again[0].id and any("different Candidate ID" in i for i in third.issues), third.issues
s.close()
wb = load_workbook(io.BytesIO(c.get("/api/export").content))
assert wb.active["A1"].value == "Candidate ID" and "9001" in {str(r[0].value) for r in wb.active.iter_rows(min_row=2)}
print("OK: Candidate ID decides matches: merged in the file, found again on re-upload, kept in exports")
