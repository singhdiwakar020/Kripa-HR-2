"""The phone conversation, independent of any phone provider.

Time helpers, a call-back time parser, and a small state machine that turns what the candidate says
into the next thing the bot says. A provider (the built-in simulator or Plivo) only has to
pass the candidate's words in and speak the returned lines out.
"""
import os
import re
from datetime import datetime, timedelta, timezone

from . import importer, llm

IST = timezone(timedelta(hours=5, minutes=30))  # India has no daylight saving


# ------------------------------------------------------------------ settings and time
def cfg() -> dict:
    provider = os.getenv("CALL_PROVIDER", "mock").strip().lower() or "mock"
    ignore = os.getenv("CALL_IGNORE_HOURS")
    return {
        "provider": provider,
        "max_attempts": int(os.getenv("CALL_MAX_ATTEMPTS", "3")),
        "retry_minutes": int(os.getenv("CALL_RETRY_MINUTES", "120")),
        "hours_start": int(os.getenv("CALL_HOURS_START", "10")),
        "hours_end": int(os.getenv("CALL_HOURS_END", "19")),
        # testing with the simulator should work at any time of day; real calls keep the hours
        "ignore_hours": (ignore == "1") if ignore is not None else provider == "mock",
        "stale_minutes": int(os.getenv("CALL_STALE_MINUTES", "10")),
        # real bulk use: never ring everyone at once, and stop at a daily limit so credit cannot run away
        "max_concurrent": int(os.getenv("CALL_MAX_CONCURRENT", "50" if provider == "mock" else "3")),
        "max_per_day": 0 if provider == "mock" else int(os.getenv("CALL_MAX_PER_DAY", "150")),
    }


def to_ist(dt: datetime) -> datetime:
    """Database times are naive UTC."""
    return dt.replace(tzinfo=timezone.utc).astimezone(IST)


def from_ist(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def within_hours(dt_utc: datetime) -> bool:
    c = cfg()
    return c["ignore_hours"] or c["hours_start"] <= to_ist(dt_utc).hour < c["hours_end"]


def clamp_to_hours(dt_utc: datetime) -> datetime:
    """Move a time forward to the next moment calls are allowed."""
    c = cfg()
    if c["ignore_hours"]:
        return dt_utc
    t = to_ist(dt_utc)
    start = t.replace(hour=c["hours_start"], minute=0, second=0, microsecond=0)
    if t < start:
        t = start
    elif t.hour >= c["hours_end"]:
        t = start + timedelta(days=1)
    return from_ist(t)


def fmt_when(dt_utc: datetime) -> str:
    t = to_ist(dt_utc)
    return f"{t:%A}, {t.day} {t:%B} at {t.hour % 12 or 12}:{t:%M} {'AM' if t.hour < 12 else 'PM'}"


# ------------------------------------------------------------------ call-back time parser
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
NUMWORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
DAYPARTS = [(r"\b(morning|subah)\b", 10), (r"\b(noon|dopahar|lunch)\b", 12), (r"\bafternoon\b", 14),
            (r"\b(evening|shaam)\b", 18), (r"\b(night|raat)\b", 20)]


def _apply_meridian(h: int, mer: str) -> int:
    mer = mer.replace(".", "")
    if mer == "pm" and h < 12:
        return h + 12
    if mer == "am" and h == 12:
        return 0
    return h


def parse_when_rules(text: str, now: datetime) -> datetime | None:
    """Understands things like 'tomorrow at 4 pm', 'Monday morning', 'in 2 hours', '18 Nov 3:30pm'. now is IST-aware."""
    t = text.lower()

    m = re.search(r"\bin\s+(\d+|an?|one|two|three|four|five|six)\s*(hours?|hrs?|minutes?|mins?|days?)\b", t)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else NUMWORDS[m.group(1)]
        unit = m.group(2)
        return now + (timedelta(days=n) if unit.startswith("d") else timedelta(hours=n) if unit.startswith("h")
                      else timedelta(minutes=n))

    day = None
    if re.search(r"day after tomorrow|\bparso\b", t):
        day = now.date() + timedelta(days=2)
    elif re.search(r"\b(tomorrow|tmrw|tomorow|kal)\b", t):
        day = now.date() + timedelta(days=1)
    elif re.search(r"\b(today|aaj|tonight)\b", t):
        day = now.date()
    else:
        for i, wd in enumerate(WEEKDAYS):
            if re.search(rf"\b{wd}[a-z]*\b", t):
                day = now.date() + timedelta(days=(i - now.weekday()) % 7 or 7)
                break
        if day is None:
            mon = "|".join(MONTHS)
            m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?({mon})[a-z]*\b", t) or \
                re.search(rf"\b({mon})[a-z]*\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", t)
            if m:
                a, b = m.groups()
                d, mo = (int(a), MONTHS[b]) if a.isdigit() else (int(b), MONTHS[a])
                try:
                    cand = now.date().replace(month=mo, day=d)
                    day = cand if cand >= now.date() else cand.replace(year=cand.year + 1)
                except ValueError:
                    return None

    hh = mm = None
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)", t)
    if m:
        hh, mm = _apply_meridian(int(m.group(1)), m.group(3)[0] + "m"), int(m.group(2) or 0)
    else:
        m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t)
        if m:
            hh, mm = int(m.group(1)), int(m.group(2))
        else:
            m = re.search(r"(?:\bat|\baround|\bby|@)\s*(\d{1,2})\b|\b(\d{1,2})\s*baje\b", t)
            if m:
                h = int(m.group(1) or m.group(2))
                if 0 < h <= 23:
                    hh, mm = (h + 12 if 1 <= h <= 7 else h), 0  # "at 4" means 4 pm; "at 10" means 10 am
            else:
                for pat, h in DAYPARTS:
                    if re.search(pat, t):
                        hh, mm = h, 0
                        break
    if hh is None and day is None:
        return None
    if hh is None:
        hh, mm = 11, 0
    if hh > 23:
        return None
    if day is None:
        out = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return out if out > now + timedelta(minutes=5) else out + timedelta(days=1)
    out = datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)
    return out if out > now else None


def parse_when(text: str, now_utc: datetime | None = None) -> datetime | None:
    """Return the callback time as naive UTC, or None if no usable future time was said."""
    now = to_ist(now_utc or datetime.utcnow())
    found = parse_when_rules(text, now)
    if found is None and os.getenv("OPENAI_API_KEY", "").strip():
        try:
            iso = llm.parse_datetime(text, now.strftime("%Y-%m-%dT%H:%M:%S"))
            if iso:
                found = datetime.fromisoformat(iso).replace(tzinfo=IST)
                if found <= now:
                    found = None
        except Exception:
            found = None
    return from_ist(found) if found else None


# ------------------------------------------------------------------ understanding replies
OPT_OUT = re.compile(r"(stop calling|don'?t call|do not call|dont call|never call|remove my (number|name)|take me off|"
                     r"unsubscribe|call mat karo)", re.I)
NOT_INTERESTED = re.compile(r"(not interested|no interest|already (got|have) (a )?job|nahi chahiye)", re.I)
BUSY = re.compile(r"\b(busy|later|not now|bad time|call (me )?(back|again)|in a meeting|driving|can'?t talk|cannot talk|"
                  r"another time|some other time|abhi nahi|baad mein|no)\b", re.I)
YES = re.compile(r"\b(yes|yeah|yep|yup|sure|ok|okay|fine|go ahead|haan|han|ji|definitely|of course|correct|right|"
                 r"absolutely|please|go on|proceed)\b", re.I)
NO = re.compile(r"\b(no|nope|nah|nahi|nahin|not|never|won'?t|don'?t)\b", re.I)


def yes_no(text: str):
    if NO.search(text):
        return False
    if YES.search(text):
        return True
    return None


def render(template: str, cand) -> str:
    first = (cand.name or "").strip().split(" ")[0] or "there"
    return (template or "").replace("{name}", first).replace("{role}", cand.best_role or "recent")


DEFAULT_GREETING = "Hello {name}, this is the hiring team calling about your {role} application."
DEFAULT_CLOSING = "Thank you for your time. Our team will be in touch. Goodbye."


# ------------------------------------------------------------------ the conversation
def _say(rec, lines):
    rec.transcript = (rec.transcript or []) + [{"who": "bot", "text": t} for t in lines]


def _hear(rec, text):
    rec.transcript = (rec.transcript or []) + [{"who": "candidate", "text": text or "(silence)"}]


def start(rec, cand, qset, questions) -> list[str]:
    lines = [render(qset.greeting or DEFAULT_GREETING, cand), "Is this a good time for a short call?"]
    rec.status = "in_progress"
    rec.conv_state = {"step": "availability", "retries": 0, "q": 0, "side": 0}
    _say(rec, lines)
    return lines


def _ask_next(rec, cand, qset, questions, st) -> tuple[list[str], str | None]:
    if st["q"] < len(questions):
        st.update(step="question", retries=0)
        return [questions[st["q"]].text], None
    if (getattr(qset, "about", "") or "").strip() and not st.get("qa_done"):
        # the script is finished: give the candidate a chance to ask about the company or job, then close smoothly
        st.update(step="final_qa", retries=0, qa_done=True)
        return ["Thank you, that was everything from my side. Do you have any questions about the company or the job?"], None
    return [render(qset.closing or DEFAULT_CLOSING, cand)], "completed"


def fixed_lines(cand, qset, questions) -> list[str]:
    """Every line this call will say that does not depend on what the candidate says, so its audio can be made before dialling."""
    qs = [q.text for q in questions]
    out = [render(qset.greeting or DEFAULT_GREETING, cand), "Is this a good time for a short call?", *qs,
           *["Sorry, I did not catch that. " + t for t in qs], render(qset.closing or DEFAULT_CLOSING, cand),
           "No problem. When would be a good time to call you back? Please say a day and time.",
           "Sorry, I did not catch that. Is now a good time? Please say yes or no.",
           "Please say yes if that time is correct, or no to change it.",
           "I am sorry, we will try you again later. Goodbye.", "Understood. We will not call you again. Goodbye.",
           "Thank you for letting us know. Have a good day. Goodbye."]
    if (getattr(qset, "about", "") or "").strip():
        out += ["Thank you, that was everything from my side. Do you have any questions about the company or the job?",
                "Is there anything else you would like to know?", "Sure, please go ahead.",
                "Sorry, I did not catch that. Do you have any other questions? Please say yes or no."]
    return out


def reached_final_stage(rec) -> bool:
    """True when every screening question was already answered (the candidate then only had the optional Q&A left)."""
    return (rec.conv_state or {}).get("step") == "final_qa"


def llm_enabled() -> bool:
    return os.getenv("CALL_USE_LLM", "1").strip() != "0" and bool(os.getenv("OPENAI_API_KEY", "").strip())


def _prompt_for(st, questions) -> str:
    """The thing the bot is currently waiting for an answer to (used to repeat it after a side question)."""
    step = st["step"]
    if step == "availability":
        return "Is this a good time for a short call?"
    if step == "ask_when":
        return "When would be a good time to call you back? Please say a day and time."
    if step == "final_qa":
        return "Is there anything else you would like to know?"
    if step == "confirm_when":
        return f"I will call you on {fmt_when(datetime.fromisoformat(st['pending']))}. Is that correct?"
    return questions[st["q"]].text


HINDI_NUMBERS = {
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "panch": 5, "paanch": 5, "chhe": 6, "chhah": 6, "che": 6, "saat": 7,
    "aath": 8, "nau": 9, "das": 10, "gyarah": 11, "barah": 12, "pandrah": 15, "pandra": 15, "bees": 20, "pachis": 25,
    "pachchis": 25, "tees": 30, "chalis": 40, "chaalis": 40, "paintalis": 45, "pachas": 50, "pachaas": 50, "saath": 60,
    "sattar": 70, "assi": 80, "nabbe": 90, "sau": 100,
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "छह": 6, "छः": 6, "सात": 7, "आठ": 8, "नौ": 9, "दस": 10,
    "पंद्रह": 15, "पन्द्रह": 15, "बीस": 20, "पच्चीस": 25, "तीस": 30, "चालीस": 40, "पैंतालीस": 45, "पचास": 50, "साठ": 60,
    "सत्तर": 70, "अस्सी": 80, "नब्बे": 90, "सौ": 100,
}


def hindi_digits(text: str) -> str:
    """'tees din' / 'तीस दिन' -> '30 din'. Only used where a number is expected, so words like 'do' are safe to read as numbers."""
    def swap(m):
        n = HINDI_NUMBERS.get(m.group(0).lower())
        return str(n) if n is not None else m.group(0)
    return re.sub(r"[^\s,.!?;:]+", swap, text or "")  # split on spaces so Devanagari vowel marks stay inside the word


def _read_answer(kind: str, text: str):
    """Returns (value, understood)."""
    if not text:
        return None, False
    if kind == "yesno":
        v = yes_no(text)
        return v, v is not None
    if kind == "number":
        v = importer.parse_float(text)
        if v is None:
            v = importer.parse_float(hindi_digits(text))
        return v, v is not None
    return text, True


def _understand_rules(step, text, questions, st) -> dict:
    """The original keyword logic, used when the LLM is off or unreachable."""
    if OPT_OUT.search(text):
        return {"intent": "opt_out"}
    if NOT_INTERESTED.search(text) and step in ("availability", "ask_when"):
        return {"intent": "not_interested"}
    if step == "availability":
        t = re.sub(r"\b(no|not a) problem\b", "yes", text, flags=re.I)
        if BUSY.search(t):
            return {"intent": "busy"}
        return {"intent": "agree" if YES.search(t) else "unclear"}
    if step == "ask_when":
        when = parse_when(text)
        return {"intent": "time", "when": when} if when else {"intent": "unclear"}
    if step in ("confirm_when", "final_qa"):
        ans = yes_no(text)
        return {"intent": "unclear" if ans is None else "agree" if ans else "disagree"}
    value, ok = _read_answer(questions[st["q"]].kind, text)
    return {"intent": "answer", "value": value, "raw_ok": ok}


def _understand_llm(step, text, rec, cand, qset, questions, st) -> dict:
    q = questions[st["q"]] if step == "question" and st["q"] < len(questions) else None
    now = to_ist(datetime.utcnow())
    history = [f'{m["who"]}: {m["text"]}' for m in (rec.transcript or [])[-6:-1]]
    u = llm.understand(step, text, {
        "prompt": _prompt_for(st, questions),
        "role": cand.best_role, "facts": getattr(qset, "about", "") or "", "history": history,
        "kind": q.kind if q else ("yes/no: does the candidate have more questions" if step == "final_qa" else "yes/no or time"),
        "now": now.strftime("%A %Y-%m-%dT%H:%M:%S")})
    out = {"intent": u["intent"], "say": u.get("say")}
    if u["intent"] == "busy" and u.get("callback_iso"):
        out["intent"] = u["intent"] = "time"  # "I'm in a meeting, call me tomorrow evening" already says when
    if q and q.kind == "yesno" and u["intent"] in ("agree", "disagree"):
        u["value"] = "yes" if u["intent"] == "agree" else "no"  # a yes/no question in the script: that is the answer
        out["intent"] = u["intent"] = "answer"
    if u["intent"] == "time":
        when = None
        if u.get("callback_iso"):
            try:
                dt = datetime.fromisoformat(u["callback_iso"]).replace(tzinfo=IST)
                when = from_ist(dt) if dt > now else None
            except ValueError:
                when = None
        out["when"] = when or parse_when(text)
        if not out["when"]:
            out["intent"] = "unclear"
    elif u["intent"] == "answer" and q:
        val = u.get("value")
        if q.kind == "yesno":
            low = (val or "").lower()
            v = True if low in ("yes", "true") else False if low in ("no", "false") else None
            out.update(value=v, raw_ok=v is not None)
        elif q.kind == "number":
            v = importer.parse_float(val or "")
            v = v if v is not None else _read_answer("number", text)[0]
            out.update(value=v, raw_ok=v is not None)
        else:
            # a value still in Devanagari means the model could not make sense of it: ask again instead of saving gibberish
            out.update(value=val or text, raw_ok=bool(val) and not re.search("[ऀ-ॿ]", val))
    elif u["intent"] == "answer":
        out["intent"] = "unclear"  # an "answer" when no question is pending
    return out


YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "haan", "han", "ji", "haan ji", "ji haan", "yes please",
             "yes sure", "yes yes", "go ahead", "sure go ahead", "yes go ahead", "yes i can", "yes i am", "yes ready", "of course"}
NO_WORDS = {"no", "nope", "nahi", "nahin", "no no", "no thanks", "not now", "no i am not", "no i cannot", "nothing", "nothing else",
            "no nothing", "that is all", "thats all", "that's all", "no that is all", "no thats all", "no thank you", "nahi ji",
            "nothing more", "no more questions", "no questions"}


def _fast(step, text, questions, st) -> dict | None:
    """Plain replies ('yes', 'no', '50000') need no language model, which saves 1 to 3 seconds on the line."""
    if os.getenv("CALL_FAST_PATH", "1").strip() == "0":
        return None
    t = re.sub(r"[^a-z0-9 ,.]", "", text.lower()).strip(" .,")
    t = re.sub(r"\s+", " ", t)
    if not t:
        return None
    yes, no = t in YES_WORDS, t in NO_WORDS
    if step in ("availability", "confirm_when", "final_qa"):
        if yes:
            return {"intent": "agree"}
        if no:
            return {"intent": "disagree"}
        return None
    if step == "question":
        q = questions[st["q"]]
        if q.kind == "yesno" and (yes or no):
            return {"intent": "answer", "value": yes, "raw_ok": True}
        if q.kind == "number" and re.fullmatch(r"[0-9][0-9,]*(\.[0-9]+)?( rupees| rs| days)?", t):
            return {"intent": "answer", "value": importer.parse_float(t), "raw_ok": True}
    return None


def understand(step, text, rec, cand, qset, questions, st) -> dict:
    fast = _fast(step, text or "", questions, st) if text else None
    if fast:
        return fast
    if text and llm_enabled():
        try:
            return _understand_llm(step, text, rec, cand, qset, questions, st)
        except Exception as e:  # network, timeout, bad reply: keep the call alive on the keyword rules
            print("call LLM unavailable, using keyword rules:", type(e).__name__, e)
    return _understand_rules(step, text, questions, st)


def reply(rec, cand, qset, questions, text: str) -> tuple[list[str], str | None]:
    """Feed what the candidate said. Returns (what the bot says next, final outcome or None while the call continues)."""
    text = (text or "").strip()
    st = dict(rec.conv_state or {"step": "availability", "retries": 0, "q": 0})
    st.setdefault("side", 0)
    _hear(rec, text)
    lines: list[str] = []
    outcome = None
    step = st["step"]
    u = understand(step, text, rec, cand, qset, questions, st)
    intent = u["intent"]

    def retry(msg):
        """First unclear reply: ask again. Second: end the call so it is tried again later."""
        if st["retries"] < 1:
            st["retries"] += 1
            return [msg], None
        return ["I am sorry, we will try you again later. Goodbye."], "incomplete"

    if intent == "opt_out":
        lines, outcome = ["Understood. We will not call you again. Goodbye."], "opted_out"
    elif intent == "not_interested" and step != "final_qa":
        lines, outcome = ["Thank you for letting us know. Have a good day. Goodbye."], "not_interested"
    elif intent == "repeat":
        lines = [_prompt_for(st, questions)]
    elif intent == "question":
        st["side"] += 1
        rec.answers = (rec.answers or []) + [{"question_id": None, "question": "Candidate asked", "kind": "note",
                                              "raw": text, "value": text}]
        said = u.get("say") if st["side"] <= 4 else None
        lines = [said or "I do not have that detail, but I will note it and our hiring team will get back to you.",
                 _prompt_for(st, questions)]
        if step == "final_qa" and st["side"] >= 6:  # enough questions for one call: wrap up politely
            lines, outcome = [lines[0], "Our hiring team will share anything else. " + render(qset.closing or DEFAULT_CLOSING, cand)], "completed"

    elif step == "final_qa":
        if intent in ("agree",):
            lines = ["Sure, please go ahead."]
        elif intent in ("unclear",) and st["retries"] < 1 and text:
            st["retries"] += 1
            lines = ["Sorry, I did not catch that. Do you have any other questions? Please say yes or no."]
        else:  # no / nothing else / thank you / silence: finish the call
            lines, outcome = [render(qset.closing or DEFAULT_CLOSING, cand)], "completed"

    elif step == "availability":
        if intent == "time" and u.get("when"):
            when = clamp_to_hours(u["when"])
            st.update(step="confirm_when", retries=0, pending=when.isoformat())
            lines = [f"No problem. I will call you on {fmt_when(when)}. Is that correct?"]
        elif intent in ("busy", "disagree"):
            st.update(step="ask_when", retries=0)
            lines = ["No problem. When would be a good time to call you back? Please say a day and time."]
        elif intent in ("agree", "answer"):
            lines, outcome = _ask_next(rec, cand, qset, questions, st)
        elif st["retries"] < 1:
            st["retries"] += 1
            lines = ["Sorry, I did not catch that. Is now a good time? Please say yes or no."]
        else:
            st.update(step="ask_when", retries=0)
            lines = ["That is fine. When should we call you back? Please say a day and time."]

    elif step == "ask_when":
        if intent == "time" and u.get("when"):
            when = clamp_to_hours(u["when"])
            st.update(step="confirm_when", retries=0, pending=when.isoformat())
            lines = [f"I will call you on {fmt_when(when)}. Is that correct?"]
        else:
            lines, outcome = retry("Sorry, I could not understand the time. Please say it like: tomorrow at 4 pm.")

    elif step == "confirm_when":
        if intent == "agree":
            rec.recall_at = datetime.fromisoformat(st["pending"])
            lines, outcome = [f"Thank you. We will call you on {fmt_when(rec.recall_at)}. Goodbye."], "callback_requested"
        elif intent == "time" and u.get("when"):
            when = clamp_to_hours(u["when"])
            st.update(pending=when.isoformat(), retries=0)
            lines = [f"Okay. I will call you on {fmt_when(when)}. Is that correct?"]
        elif intent == "disagree":
            st.update(step="ask_when", retries=0)
            lines = ["Okay. Please say the day and time again."]
        else:
            lines, outcome = retry("Please say yes if that time is correct, or no to change it.")

    elif step == "question":
        q = questions[st["q"]]
        if intent == "busy":  # has to go mid-script: keep what was answered and arrange a call back
            st.update(step="ask_when", retries=0)
            lines = ["No problem. When would be a good time to call you back? Please say a day and time."]
        else:
            ok = intent == "answer" and u.get("raw_ok", True)
            value = u.get("value") if intent == "answer" else None
            if not ok and st["retries"] < 1:
                st["retries"] += 1
                lines = ["Sorry, I did not catch that. " + q.text]
            else:
                rec.answers = (rec.answers or []) + [{"question_id": q.id, "question": q.text, "kind": q.kind,
                                                      "raw": text, "value": value}]
                st["q"] += 1
                lines, outcome = _ask_next(rec, cand, qset, questions, st)

    rec.conv_state = st
    if lines:
        _say(rec, lines)
    return lines, outcome
