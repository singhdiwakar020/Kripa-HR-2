"""Tests the Plivo line with a fake Plivo client and fake webhook posts. No account, phone or OpenAI needed."""
import os
import tempfile
from pathlib import Path

os.environ.update({"OPENAI_API_KEY": "", "ELEVENLABS_API_KEY": "", "CALL_PROVIDER": "plivo", "CALL_IGNORE_HOURS": "1", "CALL_RETRY_MINUTES": "0",
                   "CALL_MAX_ATTEMPTS": "2", "PLIVO_AUTH_ID": "MAXXXXXXXXXXXXXXXXXX", "PLIVO_AUTH_TOKEN": "secret-token",
                   "PLIVO_PHONE_NUMBER": "+911234567890", "PUBLIC_BASE_URL": "https://demo.ngrok-free.app",
                   "PLIVO_VERIFY_SIGNATURE": "0", "CALL_MAX_CONCURRENT": "10", "PLIVO_MACHINE_DETECTION": "1", "PLIVO_LISTEN": "speech"})  # detection is off by default; this tests the opt-in  # real default is 3; calls_test.py covers the limit

import app.db as dbmod  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

tmp = Path(tempfile.mkdtemp()) / "t.db"
dbmod.engine = create_engine(f"sqlite:///{tmp}", connect_args={"check_same_thread": False})
dbmod.SessionLocal = sessionmaker(bind=dbmod.engine, autoflush=False, expire_on_commit=False)

from fastapi.testclient import TestClient  # noqa: E402
from app import calls, plivo_calls  # noqa: E402
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


class FakeCalls:
    created, deleted, fail = [], [], False

    def create(self, **kw):
        if FakeCalls.fail:
            raise RuntimeError("Plivo said no")
        FakeCalls.created.append(kw)
        return type("R", (), {"request_uuid": "req-123"})()

    def delete(self, uuid):
        FakeCalls.deleted.append(uuid)


class FakeClient:
    calls = FakeCalls()


plivo_calls._client = lambda: FakeClient()

c = TestClient(app)
with open("sample_career_form.xlsx", "rb") as fh:
    p = c.post("/api/imports", files={"file": ("s.xlsx", fh)}).json()
c.post(f"/api/imports/{p['batch_id']}/confirm")
s0 = dbmod.SessionLocal()
calls.seed_defaults(s0)
s0.close()
SET = c.get("/api/question-sets").json()[0]["id"]
people = [x for x in c.get("/api/search", params={"limit": 200}).json()["results"] if x["phone"]]
A, B, C, D = [x["id"] for x in people[:4]]
OPT = people[4]["id"]  # a person who has not been screened yet (A finishes the script, so A is never called twice)


def dispatch():
    s = dbmod.SessionLocal()
    try:
        return calls.dispatch_due(s)
    finally:
        s.close()


def rec_of(cid):
    return c.get(f"/api/candidates/{cid}/calls").json()[0]


def job_of(cid):
    return max((j for j in c.get("/api/calls").json()["jobs"] if j["candidate_id"] == cid), key=lambda j: j["id"])


def hook(path, rid, **form):
    return c.post(f"/api/plivo/{path}?rid={rid}", data=form)


# ---------------------------------------------------------------- placing a call
c.post("/api/calls/start", json={"candidate_ids": [A, B, C, D], "question_set_id": SET})
assert dispatch() == 4 and len(FakeCalls.created) == 4
k = FakeCalls.created[0]
assert k["from_"] == "911234567890" and k["to_"].startswith("91") and len(k["to_"]) == 12, k
assert k["answer_url"].startswith("https://demo.ngrok-free.app/api/plivo/answer?rid="), k
assert k["machine_detection"] == "true" and k["hangup_method"] == "POST"
ra, rb, rc, rd = [rec_of(x) for x in (A, B, C, D)]
assert ra["provider"] == "plivo" and ra["status"] == "ringing" and job_of(A)["state"] == "calling"
print("OK: call placed with the right numbers and webhook addresses")

# ---------------------------------------------------------------- a full happy call
x = hook("answer", ra["id"], CallUUID="uuid-A")
assert x.status_code == 200 and "text/xml" in x.headers["content-type"], x.text
assert "<GetInput" in x.text and 'inputType="speech"' in x.text and "good time" in x.text, x.text
assert f"/api/plivo/input?rid={ra['id']}" in x.text and "<Redirect" in x.text
assert "<Hangup" in hook("answer", ra["id"]).text  # Plivo asking twice must not restart the script
x = hook("input", ra["id"], Speech="yes sure")
assert "notice period" in x.text and "<Hangup" not in x.text, x.text
hook("input", ra["id"], Speech="one month")
hook("input", ra["id"], Speech="fifty thousand rupees 50000")
hook("input", ra["id"], Speech="yes")
x = hook("input", ra["id"], Speech="yes")
assert "<Hangup" in x.text and "Thank you" in x.text, x.text
r = rec_of(A)
assert r["status"] == "completed" and len(r["answers"]) == 4 and job_of(A)["state"] == "done", r
assert hook("hangup", ra["id"], CallStatus="completed").status_code == 204  # late hangup changes nothing
assert rec_of(A)["status"] == "completed"
print("OK: full call, 4 answers saved")

# ---------------------------------------------------------------- busy: asks for a time, schedules a recall
hook("answer", rb["id"])
hook("input", rb["id"], Speech="I am busy right now")
x = hook("input", rb["id"], Speech="tomorrow at 4 pm")
assert "Is that correct" in x.text, x.text
x = hook("input", rb["id"], Speech="yes")
assert "<Hangup" in x.text
assert rec_of(B)["status"] == "callback_requested" and job_of(B)["state"] == "recall_wait"
print("OK: busy candidate gets a recall")

# ---------------------------------------------------------------- silence, voicemail, busy tone
hook("answer", rc["id"])
x = hook("input", rc["id"], silent="1")  # nothing heard
assert "did not catch" in x.text, x.text
hook("machine", rc["id"], Machine="true", CallUUID="uuid-C")
assert rec_of(C)["status"] == "no_answer" and FakeCalls.deleted == ["uuid-C"]
assert job_of(C)["state"] == "retry_wait"
assert hook("hangup", rc["id"], CallStatus="completed").status_code == 204
assert rec_of(C)["status"] == "no_answer"
hook("hangup", rd["id"], CallStatus="busy")
assert rec_of(D)["status"] == "busy" and job_of(D)["state"] == "retry_wait"
print("OK: voicemail and busy tone count as unanswered attempts")

# ---------------------------------------------------------------- retry, then a hang-up midway
assert dispatch() == 2  # C and D go again
rc2 = rec_of(C)
assert rc2["attempt"] == 2
hook("answer", rc2["id"])
hook("hangup", rc2["id"], CallStatus="completed")
assert rec_of(C)["status"] == "incomplete" and job_of(C)["state"] == "gave_up"
print("OK: retry placed, early hang-up closes the call")

# ---------------------------------------------------------------- opt out
c.post("/api/calls/start", json={"candidate_ids": [OPT], "question_set_id": SET})
assert dispatch() >= 1
ro = rec_of(OPT)
hook("answer", ro["id"])
x = hook("input", ro["id"], Speech="please stop calling me")
assert "<Hangup" in x.text and c.get(f"/api/candidates/{OPT}").json()["do_not_contact"] is True
print("OK: opt-out sets do not contact")

# ---------------------------------------------------------------- Plivo refuses the call
FakeCalls.fail = True
c.post(f"/api/calls/jobs/{job_of(B)['id']}/cancel")  # B was waiting on a recall; start a clean job instead
c.post("/api/calls/start", json={"candidate_ids": [B], "question_set_id": SET})
dispatch()
assert rec_of(B)["status"] == "failed" and job_of(B)["state"] == "retry_wait", (rec_of(B), job_of(B))
FakeCalls.fail = False
print("OK: a refused call is recorded as failed and retried")

# ---------------------------------------------------------------- unknown record
assert "<Hangup" in hook("answer", 99999).text and "<Hangup" in hook("input", 99999, Speech="hi").text

# ---------------------------------------------------------------- signature check
os.environ["PLIVO_VERIFY_SIGNATURE"] = "1"
assert hook("answer", ra["id"]).status_code == 403
from plivo.utils.signature_v3 import get_signature_v3, construct_post_url  # noqa: E402

url = f"https://demo.ngrok-free.app/api/plivo/hangup?rid={rd['id']}"
form = {"CallStatus": "busy"}
nonce = "12345"
sig = get_signature_v3("secret-token", construct_post_url(url, form).decode(), nonce).decode()
ok = c.post(f"/api/plivo/hangup?rid={rd['id']}", data=form, headers={"X-Plivo-Signature-V3": sig, "X-Plivo-Signature-V3-Nonce": nonce})
assert ok.status_code == 204, (ok.status_code, ok.text)
bad = c.post(f"/api/plivo/hangup?rid={rd['id']}", data=form, headers={"X-Plivo-Signature-V3": "nope", "X-Plivo-Signature-V3-Nonce": nonce})
assert bad.status_code == 403
print("OK: unsigned or wrongly signed webhooks are rejected, correctly signed ones accepted")
print("\nALL PLIVO CHECKS PASSED")

# ---- by default nothing hangs up on a "voicemail" verdict (it mistakes real people for machines)
os.environ["PLIVO_MACHINE_DETECTION"] = "0"
os.environ["PLIVO_VERIFY_SIGNATURE"] = "0"
FakeCalls.created.clear()
FakeCalls.deleted.clear()
c.post("/api/calls/start", json={"candidate_ids": [people[5]["id"]], "question_set_id": SET})
assert dispatch() >= 1 and all("machine_detection" not in k for k in FakeCalls.created), FakeCalls.created
r5 = rec_of(people[5]["id"])
hook("answer", r5["id"])
hook("machine", r5["id"], Machine="true", CallUUID="uuid-X")
assert rec_of(people[5]["id"])["status"] == "in_progress" and FakeCalls.deleted == [], "must not hang up on a person"
print("OK: calls are no longer cut by wrong voicemail detection")


# ---- record mode (the default): the answer is recorded, transcribed, then the conversation continues
os.environ["PLIVO_LISTEN"] = "record"
os.environ["PLIVO_VERIFY_SIGNATURE"] = "0"
heard_queue = ["", "yes", "one month"]  # silence first, then real answers


def fake_transcribe(url, duration_ms):
    assert url.startswith("https://"), url
    return heard_queue.pop(0)


plivo_calls.transcribe = fake_transcribe
REC = people[7]["id"]
c.post("/api/calls/start", json={"candidate_ids": [REC], "question_set_id": SET})
assert dispatch() >= 1
rr = rec_of(REC)
x = hook("answer", rr["id"]).text
assert "<Record" in x and f"/api/plivo/record?rid={rr['id']}" in x and "good time" in x and "<GetInput" not in x, x
x = hook("record", rr["id"], RecordUrl="https://media.example/a.mp3", RecordingDurationMs="2300").text
assert "<Redirect" in x and f"/api/plivo/next?rid={rr['id']}" in x, x
x = hook("next", rr["id"]).text  # silence: asked again
assert "did not catch" in x and "<Record" in x, x
hook("record", rr["id"], RecordUrl="https://media.example/b.mp3", RecordingDurationMs="900")
x = hook("next", rr["id"]).text  # "yes": the first question is asked
assert "notice period" in x and "<Record" in x, x
assert rec_of(REC)["status"] == "in_progress"
assert "<Hangup" in hook("record", 999999, RecordUrl="https://x/a.mp3").text  # unknown call: hang up quietly
assert "<Hangup" in hook("next", 999999).text
print("OK: record mode: records, transcribes, continues the script, treats silence as silence")

# ---- ElevenLabs voice: lines are made ahead of the call, played from files, with the Plivo voice as the fallback
import re as _re  # noqa: E402
import time as _time  # noqa: E402
from app import tts  # noqa: E402

tts.CACHE_DIR = Path(tempfile.mkdtemp())
made = []


def fake_fetch(text):
    made.append(text)
    return b"x" * 600


tts._fetch = fake_fetch
os.environ.update({"ELEVENLABS_API_KEY": "fake-key", "ELEVENLABS_VOICE_ID": "voice123"})
heard_queue[:] = ["yes", "one month"]
EL = people[8]["id"]
c.post("/api/calls/start", json={"candidate_ids": [EL], "question_set_id": SET})
assert dispatch() >= 1
for _ in range(40):  # the audio is made in the background while the phone rings
    if any("good time" in t for t in made) and any("notice period" in t for t in made):
        break
    _time.sleep(0.1)
assert any("good time" in t for t in made) and any("notice period" in t for t in made) and "Okay." not in made, made
rr = rec_of(EL)
x = hook("answer", rr["id"]).text
assert x.count("<Play>https://demo.ngrok-free.app/api/plivo/audio/") == 2 and "<Speak" not in x and "<Record" in x, x
name = _re.search(r"/api/plivo/audio/([0-9a-f]{40}\.mp3)", x).group(1)
a = c.get(f"/api/plivo/audio/{name}")
assert a.status_code == 200 and a.headers["content-type"] == "audio/mpeg" and len(a.content) == 600
assert c.get("/api/plivo/audio/../../.env").status_code in (404, 405) and c.get("/api/plivo/audio/" + "0" * 40 + ".mp3").status_code == 404
x = hook("record", rr["id"], RecordUrl="https://media.example/a.mp3", RecordingDurationMs="2300").text
assert "<Play>" not in x and "<Redirect" in x, x  # no filler sound: just wait for the reply
before = len(made)
x = hook("next", rr["id"]).text  # question one was prepared before dialling: nothing new is made during the call
assert "<Play>" in x and "<Record" in x and len(made) == before, (x, made[before:])
# if ElevenLabs fails, the Plivo voice takes over at once and the call goes on
def broken(text):
    raise RuntimeError("503")
tts._fetch = broken
x = plivo_calls.hangup_xml(["A line nobody made yet."]).body.decode()
assert "<Speak" in x and "<Play>" not in x and "<Hangup" in x, x
assert tts.urls_for(["Another new line."]) == [None]  # circuit open: no waiting on the broken service
tts._down_until = 0.0
os.environ["TTS_PROVIDER"] = "polly"
assert "<Speak" in plivo_calls.hangup_xml(["Anything."]).body.decode()  # one setting turns ElevenLabs off
os.environ.pop("TTS_PROVIDER")
os.environ["ELEVENLABS_API_KEY"] = ""
print("OK: ElevenLabs lines are prepared before the call, served from files, with Plivo voice as the fallback")
