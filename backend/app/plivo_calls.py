"""Plivo phone line: places the calls and answers Plivo's webhooks.

Flow: the scheduler (calls.dispatch_due) asks PlivoProvider to place a call. When the candidate picks up, Plivo
fetches /api/plivo/answer, we reply with Plivo XML (speak + listen), Plivo posts what the candidate said to
/api/plivo/input, and so on until callengine.reply() reports an outcome. /api/plivo/hangup closes calls that
ended without an outcome (busy, no answer, hung up early). The conversation logic lives in callengine.py.

Needs in backend/.env: PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN, PLIVO_PHONE_NUMBER, PUBLIC_BASE_URL (the public
https address Plivo can reach, for example the ngrok one).
"""
import json
import os
import threading
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from plivo import plivoxml as px
from sqlalchemy.orm import Session

from . import callengine as eng
from . import calls
from . import db as dbmod
from . import llm
from . import tts
from .db import get_db
from .models import CallJob, CallRecord, Candidate, QuestionSet

router = APIRouter(prefix="/api/plivo")


# ------------------------------------------------------------------ settings
def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def base_url() -> str:
    return _env("PUBLIC_BASE_URL").rstrip("/")


def voice() -> str:
    return _env("PLIVO_VOICE", "Polly.Aditi")  # a female Indian-English voice


HINTS = ("yes,no,yeah,okay,haan,nahi,busy,later,call me back,tomorrow,today,morning,afternoon,evening,monday,tuesday,wednesday,"
         "thursday,friday,saturday,sunday,notice period,one month,two months,fifteen days,thirty days,immediately,thousand,lakh,"
         "relocate,stop calling")


def machine_detection_on() -> bool:
    return _env("PLIVO_MACHINE_DETECTION", "0") == "1"


def _digits(number: str) -> str:
    return "".join(ch for ch in number if ch.isdigit())


def _client():
    from plivo import RestClient
    auth_id, token = _env("PLIVO_AUTH_ID"), _env("PLIVO_AUTH_TOKEN")
    if not auth_id or not token:
        raise RuntimeError("PLIVO_AUTH_ID and PLIVO_AUTH_TOKEN are not set in backend/.env")
    return RestClient(auth_id, token, timeout=20)


# ------------------------------------------------------------------ placing calls
class PlivoProvider:
    name = "plivo"

    def place_call(self, db: Session, job: CallJob, cand: Candidate) -> CallRecord:
        n = db.query(CallRecord).filter(CallRecord.job_id == job.id).count()
        rec = CallRecord(job_id=job.id, candidate_id=cand.id, attempt=n + 1, provider=self.name,
                         status="ringing", started_at=datetime.utcnow())
        db.add(rec)
        job.state = "calling"
        db.commit()  # Plivo may call our webhooks within seconds, so the record must already exist
        try:
            qset = db.get(QuestionSet, job.question_set_id or 0)
            if qset and tts.enabled():  # make the audio now, while the phone rings, so the first words play with no wait
                tts.prewarm(eng.fixed_lines(cand, qset, calls.questions_of(db, qset.id)))
            base, from_ = base_url(), _digits(_env("PLIVO_PHONE_NUMBER"))
            if not base.startswith("https://"):
                raise RuntimeError("PUBLIC_BASE_URL must be the public https address (for example the ngrok one)")
            if not from_:
                raise RuntimeError("PLIVO_PHONE_NUMBER is not set in backend/.env")
            q = f"?rid={rec.id}"
            resp = _client().calls.create(
                from_=from_, to_="91" + cand.phone,  # phones are stored as 10 digits
                answer_url=f"{base}/api/plivo/answer{q}", answer_method="POST",
                hangup_url=f"{base}/api/plivo/hangup{q}", hangup_method="POST",
                ring_timeout=30,
                **({"machine_detection": "true", "machine_detection_url": f"{base}/api/plivo/machine{q}",
                    "machine_detection_method": "POST"} if machine_detection_on() else {}),
            )
            rec.provider_call_id = str(getattr(resp, "request_uuid", "") or "")[:100]
        except Exception as e:  # bad settings, network, Plivo refused the call
            print("plivo place_call failed:", type(e).__name__, e)
            calls.finish_record(db, rec, "failed")
        db.commit()
        return rec


# ------------------------------------------------------------------ webhook security
async def verified(request: Request) -> dict:
    """Reads Plivo's form post and checks the signature, so only Plivo can drive a call."""
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    if _env("PLIVO_VERIFY_SIGNATURE", "1") == "0":
        return params
    token = _env("PLIVO_AUTH_TOKEN")
    if not token or not base_url():
        raise HTTPException(503, "Plivo is not set up on this server.")
    from plivo.utils.signature_v3 import validate_v3_signature
    uri = base_url() + request.url.path + (f"?{request.url.query}" if request.url.query else "")
    ok = False
    try:
        ok = validate_v3_signature(request.method, uri, request.headers.get("X-Plivo-Signature-V3-Nonce", ""),
                                   token, request.headers.get("X-Plivo-Signature-V3", ""), params)
    except Exception:
        pass
    if not ok:
        raise HTTPException(403, "Bad signature.")
    return params


# ------------------------------------------------------------------ XML replies
def _xml(resp: px.ResponseElement) -> Response:
    return Response(resp.to_string(), media_type="text/xml")


def _speak(lang: str, text: str) -> px.SpeakElement:
    return px.SpeakElement(text, voice=voice(), language=lang)


def _add_lines(r: px.ResponseElement, lines, lang: str):
    """Say each line with the ElevenLabs recording when there is one, otherwise with the Plivo voice."""
    lines = list(lines)
    for text, url in zip(lines, tts.urls_for(lines)):
        r.add(px.PlayElement(url) if url else _speak(lang, text))


def hangup_xml(lines=(), lang="en-IN") -> Response:
    r = px.ResponseElement()
    _add_lines(r, lines, lang)
    r.add(px.HangupElement())
    return _xml(r)


@router.get("/audio/{name}")
def audio(name: str):
    """Serves a recorded line to Plivo. Names are unguessable and hold only the caller's script wording."""
    import re
    f = tts.path_for(name)
    if not re.fullmatch(r"[0-9a-f]{40}\.mp3", name) or not f.is_file():
        raise HTTPException(404, "No such audio.")
    return Response(f.read_bytes(), media_type="audio/mpeg", headers={"Cache-Control": "public, max-age=86400"})


def listen_mode() -> str:
    """record = record the answer and transcribe it with OpenAI (reliable for Indian accents); speech = Plivo's own recogniser."""
    return "speech" if _env("PLIVO_LISTEN", "record").lower() == "speech" else "record"


def listen_xml(rid: int, lines, lang="en-IN") -> Response:
    """Say the lines, then listen for the candidate's answer."""
    r = px.ResponseElement()
    if listen_mode() == "record":
        _add_lines(r, lines, lang)
        # ends after ~2 seconds of silence (or 20 seconds in all); Plivo then posts the recording address to /record
        r.add(px.RecordElement(action=f"{base_url()}/api/plivo/record?rid={rid}", method="POST", file_format="mp3",
                               max_length=20, timeout=int(_env("PLIVO_RECORD_SILENCE", "2")), play_beep=False))
        return _xml(r)
    url = f"{base_url()}/api/plivo/input?rid={rid}"
    # phone_call is the recogniser tuned for telephone audio; hints nudge it towards the words we expect
    g = px.GetInputElement(action=url, method="POST", input_type="speech", execution_timeout=15,
                           speech_end_timeout=2, language=lang, speech_model=_env("PLIVO_SPEECH_MODEL", "phone_call"),
                           hints=_env("PLIVO_SPEECH_HINTS", HINTS))
    g.add_speak(" ".join(lines), voice=voice(), language=lang)
    r.add(g)
    # Plivo continues here when nothing was heard: treat it as silence so the engine can repeat or give up
    r.add(px.RedirectElement(url + "&silent=1", method="POST"))
    return _xml(r)


# ------------------------------------------------------------------ webhooks
def _context(db: Session, rid: int):
    rec = db.get(CallRecord, rid)
    if not rec or rec.provider != "plivo":
        return None
    job = db.get(CallJob, rec.job_id)
    qset = db.get(QuestionSet, (job.question_set_id if job else 0) or 0)
    cand = db.get(Candidate, rec.candidate_id)
    if not (job and qset and cand):
        return None
    return rec, cand, qset, calls.questions_of(db, qset.id)


@router.post("/answer")
def answer(rid: int, params: dict = Depends(verified), db: Session = Depends(get_db)):
    ctx = _context(db, rid)
    if not ctx or ctx[0].status != "ringing":
        return hangup_xml()
    rec, cand, qset, qs = ctx
    lines = eng.start(rec, cand, qset, qs)
    if params.get("CallUUID"):
        rec.provider_call_id = params["CallUUID"][:100]
    db.commit()
    return listen_xml(rid, lines, qset.language or "en-IN")


def _heard(params: dict) -> str:
    text = (params.get("Speech") or "").strip()
    if text.startswith("{"):  # some speech results arrive as JSON
        try:
            data = json.loads(text)
            text = str(data.get("transcript") or data.get("text") or "").strip()
        except ValueError:
            pass
    return text or (params.get("Digits") or "").strip()


def advance(db: Session, rid: int, text: str) -> Response:
    """Give the candidate's words to the conversation and return what Plivo should do next."""
    ctx = _context(db, rid)
    if not ctx or ctx[0].status != "in_progress":
        return hangup_xml()
    rec, cand, qset, qs = ctx
    lang = qset.language or "en-IN"
    lines, outcome = eng.reply(rec, cand, qset, qs, text)
    if outcome:
        calls.finish_record(db, rec, outcome)
        db.commit()
        return hangup_xml(lines, lang)
    db.commit()
    return listen_xml(rid, lines, lang)


@router.post("/input")
def got_input(rid: int, params: dict = Depends(verified), db: Session = Depends(get_db)):
    heard = _heard(params)
    print(f"plivo speech input rid={rid}: fields={sorted(params)} heard={heard!r}")  # to diagnose recognition problems
    return advance(db, rid, heard)


# ---- record mode: the answer is recorded, transcribed in the background, then the conversation continues
STT_PROMPT = ("A phone call between a recruiter's assistant and a job candidate in India. Replies are short and may mix Hindi and English. "
              "Write everything in English letters (Roman script), never Devanagari, and write numbers as digits. "
              "Typical replies: yes, no, haan, nahi, 30 days, 15 din, tees din, ek mahina, do mahine, teen mahine, immediate, "
              "notice period, salary 50000 rupees, 40 thousand, 5 lakh, tomorrow, evening, relocate, busy, call me later.")
_pending: dict[int, dict] = {}  # call record id -> {"xml": str | None, "waits": int}


def transcribe(url: str, duration_ms: int | None) -> str:
    """Download the recording and turn it into text. Returns '' for silence or when nothing usable came back."""
    import httpx
    if not url.startswith("https://") or (duration_ms is not None and duration_ms < 600):
        return ""
    t0 = time.time()
    data = b""
    for _ in range(8):  # the file can take a moment to appear
        try:
            r = httpx.get(url, timeout=10, follow_redirects=True)
            if r.status_code == 200 and len(r.content) > 500:
                data = r.content
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    t_dl = time.time() - t0
    if not data:
        return ""
    client = llm._client(timeout=15.0, retries=0)
    model = _env("CALL_STT_MODEL", "gpt-4o-mini-transcribe")
    try:
        res = client.audio.transcriptions.create(model=model, file=("answer.mp3", data, "audio/mpeg"), prompt=STT_PROMPT)
    except Exception as e:
        print("transcription failed:", type(e).__name__, e)
        return ""
    text = (res.text or "").strip()
    print(f"timing: download {t_dl:.1f}s, transcribe {time.time() - t0 - t_dl:.1f}s")
    # speech models sometimes invent a stock phrase for a silent clip
    if text.lower().strip(" .!,") in {"thank you", "thanks", "you", "bye", "thanks for watching"} and (duration_ms or 0) < 2500:
        return ""
    return text


def _process(rid: int, url: str, duration_ms: int | None):
    text = ""
    try:
        text = transcribe(url, duration_ms)
    except Exception as e:
        print("recording step failed:", type(e).__name__, e)
    t_heard = time.time()
    print(f"plivo recording rid={rid}: {duration_ms} ms, heard={text!r}")
    s = dbmod.SessionLocal()
    try:
        xml = advance(s, rid, text).body.decode()
    except Exception as e:
        print("conversation step failed:", type(e).__name__, e)
        xml = hangup_xml().body.decode()
    finally:
        s.close()
    print(f"timing: understanding + reply {time.time() - t_heard:.1f}s")
    if rid in _pending:
        _pending[rid]["xml"] = xml


def _wait_xml(rid: int) -> Response:
    return _xml(_redirect(rid))


def _redirect(rid: int) -> px.ResponseElement:
    r = px.ResponseElement()
    r.add(px.RedirectElement(f"{base_url()}/api/plivo/next?rid={rid}", method="POST"))
    return r


@router.post("/record")
def recorded(rid: int, params: dict = Depends(verified), db: Session = Depends(get_db)):
    """Plivo finished recording the candidate. Start transcribing and send Plivo to /next to collect the reply."""
    rec = db.get(CallRecord, rid)
    if not rec or rec.provider != "plivo" or rec.status != "in_progress":
        return hangup_xml()
    dur = params.get("RecordingDurationMs") or params.get("RecordingDuration")
    try:
        dur_ms = int(float(dur)) if params.get("RecordingDurationMs") else int(float(dur) * 1000) if dur else None
    except ValueError:
        dur_ms = None
    _pending[rid] = {"xml": None, "waits": 0}
    threading.Thread(target=_process, args=(rid, params.get("RecordUrl") or "", dur_ms), daemon=True).start()
    return _wait_xml(rid)


@router.post("/next")
def next_step(rid: int, params: dict = Depends(verified)):
    """Plivo asks for the next instruction. Waits briefly for the transcription; if not ready, asks Plivo to come back."""
    end = time.time() + 3.5
    while time.time() < end:
        job = _pending.get(rid)
        if job and job["xml"]:
            _pending.pop(rid, None)
            return Response(job["xml"], media_type="text/xml")
        if job is None:
            break
        time.sleep(0.25)
    job = _pending.get(rid)
    if job is None:  # server restarted or already handled: end this call cleanly
        return hangup_xml()
    job["waits"] += 1
    if job["waits"] > 8:
        _pending.pop(rid, None)
        return hangup_xml()
    return _wait_xml(rid)


@router.post("/machine")
def machine(rid: int, params: dict = Depends(verified), db: Session = Depends(get_db)):
    """Answering-machine detection result. A voicemail counts as an unanswered attempt."""
    if not machine_detection_on():  # off by default: it mistakes real people for voicemail and hangs up on them
        return Response("", status_code=204)
    rec = db.get(CallRecord, rid)
    if rec and rec.provider == "plivo" and params.get("Machine", "").lower() == "true" \
            and rec.status in calls.OPEN_RECORD:
        calls.finish_record(db, rec, "no_answer")
        db.commit()
        uuid = params.get("CallUUID") or rec.provider_call_id
        try:
            _client().calls.delete(uuid)  # hang up on the voicemail
        except Exception as e:
            print("plivo hangup failed:", type(e).__name__, e)
    return Response("", status_code=204)


HANGUP_MAP = {"busy": "busy", "no-answer": "no_answer", "timeout": "no_answer", "failed": "failed",
              "cancel": "failed", "rejected": "busy"}


@router.post("/hangup")
def hangup(rid: int, params: dict = Depends(verified), db: Session = Depends(get_db)):
    """Plivo says the call is over. Closes it only if no outcome was reached during the conversation."""
    rec = db.get(CallRecord, rid)
    if rec and rec.provider == "plivo" and rec.status in calls.OPEN_RECORD:
        if rec.status == "in_progress":
            outcome = "completed" if eng.reached_final_stage(rec) else "incomplete"  # hung up during the optional Q&A: all answers are in
        else:
            outcome = HANGUP_MAP.get((params.get("CallStatus") or "").lower(), "no_answer")
        calls.finish_record(db, rec, outcome)
        db.commit()
    return Response("", status_code=204)
