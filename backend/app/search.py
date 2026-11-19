"""Search: the OpenAI model reads the request and judges skill fit. Filters are applied in the database."""
import re
from difflib import SequenceMatcher

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import llm
from .models import Candidate

STATUSES = ["In database", "Called", "Not reachable", "Test submitted", "Rejected"]

# words that wrap a name search but are not part of anyone's name
STOP = {"search", "seach", "serch", "serach", "find", "show", "get", "give", "list", "lookup", "look", "up", "for", "the",
        "name", "named", "called", "me", "of", "a", "an", "candidate", "candidates", "person", "people", "mr", "mrs", "ms",
        "shri", "please", "pls", "with", "by", "id", "contact"}


def _tokens(s: str) -> list[str]:
    """Lowercase words of a query. Digit groups such as '98765 43210' are joined so phone numbers can be found."""
    s = re.sub(r"(?<=\d)[\s-]+(?=\d)", "", (s or "").lower())
    out = [t.strip(".,;:") for t in re.findall(r"[\w@.+]+", s)]
    return [t for t in out if t and t not in STOP]


def _fuzzy_names(db: Session, word: str, limit: int = 40) -> dict[int, float]:
    """Typo-tolerant name match: every word typed must resemble some word in the name."""
    toks = _tokens(word)
    if not toks:
        return {}
    scored = []
    for cid, name in db.query(Candidate.id, Candidate.name):
        parts = _tokens(name)
        if not parts:
            continue
        per = [max(SequenceMatcher(None, t, p).ratio() for p in parts) for t in toks]
        if min(per) >= 0.72:
            scored.append((sum(per) / len(per), cid))
    scored.sort(reverse=True)
    return {cid: s for s, cid in scored[:limit]}


def facets(db: Session) -> dict:
    def distinct(col):
        return [r[0] for r in db.query(col, func.count()).filter(col.isnot(None)).filter(col != "")
                .group_by(col).order_by(func.count().desc()).all()]
    locs: dict[str, int] = {}
    for (loc,) in db.query(Candidate.location).filter(Candidate.location.isnot(None)):
        key = loc.split(",")[0].strip()
        if key:
            locs[key] = locs.get(key, 0) + 1
    return {
        "roles": distinct(Candidate.best_role),
        "locations": [k for k, _ in sorted(locs.items(), key=lambda kv: -kv[1])],
        "statuses": STATUSES + ["Do not contact"],
        "total": db.query(func.count(Candidate.id)).scalar(),
    }


def _apply_filters(query, f: dict):
    query = query.filter(Candidate.do_not_contact.is_(False))  # never shown
    if f.get("status") == "Do not contact":
        return query.filter(Candidate.id < 0)
    if f.get("role"):
        if f.get("role_match") == "family":  # "AI Developer" also covers "AI Developer Intern"
            query = query.filter(func.lower(Candidate.best_role).contains(f["role"].lower(), autoescape=True))
        else:
            query = query.filter(Candidate.best_role == f["role"])
    if f.get("min_score") is not None:
        query = query.filter(Candidate.score >= f["min_score"])
    if f.get("max_score") is not None:
        query = query.filter(Candidate.score <= f["max_score"])
    if f.get("status"):
        query = query.filter(Candidate.status == f["status"])
    if f.get("min_experience") is not None and f["min_experience"] > 0:  # "at least 0 years" says nothing
        query = query.filter(Candidate.experience_years >= f["min_experience"])
    if f.get("max_experience") is not None:  # people whose experience is not stated are kept, and marked in the results
        query = query.filter((Candidate.experience_years <= f["max_experience"]) | Candidate.experience_years.is_(None))
    if f.get("max_notice_days") is not None:
        query = query.filter(Candidate.notice_period_days <= f["max_notice_days"])
    if f.get("max_expectation") is not None:
        query = query.filter(Candidate.expectation <= f["max_expectation"])
    if f.get("ready_to_relocate"):
        query = query.filter(Candidate.ready_to_relocate.is_(True))
    if f.get("location") and not f.get("include_relocators"):
        query = query.filter(func.lower(Candidate.location).contains(f["location"].lower()))
    elif f.get("location") and f.get("include_relocators"):
        like = func.lower(Candidate.location).contains(f["location"].lower())
        query = query.filter(like | (Candidate.ready_to_relocate.is_(True)))
    if f.get("callable_only"):
        query = query.filter((Candidate.email.isnot(None)) | (Candidate.phone.isnot(None)))
    if f.get("name"):  # every word typed must be inside the name, in any order and any part of it
        for t in _tokens(f["name"]):
            query = query.filter(func.lower(Candidate.name).contains(t, autoescape=True))
    if f.get("text"):  # every word must appear somewhere in the person's details
        hay = func.lower(func.coalesce(Candidate.name, ""))
        for col in (Candidate.email, Candidate.phone, Candidate.company, Candidate.location, Candidate.best_role,
                    Candidate.reasoning, Candidate.hr_notes):
            hay = hay + " " + func.lower(func.coalesce(col, ""))
        for t in _tokens(f["text"]):
            query = query.filter(hay.contains(t, autoescape=True))
    if f.get("_ids") is not None:  # internal: used for the typo-tolerant name fallback
        query = query.filter(Candidate.id.in_(f["_ids"]))
    return query


def _explain(c: Candidate, f: dict, reason: str) -> list[str]:
    why = []
    if reason:
        why.append(reason)
    if c.best_role:
        why.append(c.best_role)
    if c.score is not None:
        why.append(f"score {c.score:g}")
    if c.experience_text:
        why.append(c.experience_text if len(c.experience_text) < 28 else f"{c.experience_years:g} yrs"
                   if c.experience_years is not None else "experience listed")
    if c.location:
        why.append(c.location.split(",")[0])
    if f.get("max_experience") is not None and c.experience_years is None:
        why.append("experience not stated: check the resume")
    if f.get("ready_to_relocate") and c.ready_to_relocate:
        why.append("ready to relocate")
    if f.get("location") and f.get("include_relocators") and c.location and \
            f["location"].lower() not in c.location.lower():
        why.append("outside city but ready to relocate")
    return why


def _profile(c: Candidate) -> dict:
    return {
        "id": c.id, "name": c.name, "role": c.best_role, "score": c.score,
        "experience": c.experience_text, "location": c.location, "company": c.company,
        "reasoning": (c.reasoning or "")[:400], "notes": (c.hr_notes or "")[:200] or None,
    }


def run_search(db: Session, q: str, explicit: dict, limit: int, offset: int, sort: str) -> dict:
    fc = facets(db)
    notice = None
    parsed: dict = {}
    if q.strip():
        try:
            parsed = llm.parse_with_llm(q, fc["roles"], fc["locations"])
        except llm.LLMUnavailable:
            # still useful without the AI: look for the words in names, contact details and notes
            parsed = {"text": q.strip()}
            notice = "Smart understanding is unavailable right now, so I searched for your words in names, contacts and notes."
        if not parsed:  # nothing recognisable as a filter, so treat the words as a plain text search
            parsed = {"text": q.strip()}
    filters = {**parsed, **{k: v for k, v in explicit.items() if v not in (None, "", [])}}
    if parsed.get("role") and not explicit.get("role"):
        filters["role_match"] = "family"  # a role understood from words is a family: developers includes developer interns
    rows = _apply_filters(db.query(Candidate), filters).all()

    fuzzy: dict[int, float] = {}
    word = filters.get("name") or filters.get("text")
    if not rows and word:  # no exact match: try similar spellings of the name
        fuzzy = _fuzzy_names(db, word)
        if fuzzy:
            relaxed = {k: v for k, v in filters.items() if k not in ("name", "text")}
            rows = _apply_filters(db.query(Candidate), {**relaxed, "_ids": list(fuzzy)}).all()
            if rows:
                notice = "No exact match. Showing similar names."
            else:
                fuzzy = {}

    skills = [k for k in filters.get("keywords", []) if k]
    reasons = {}
    if skills:
        judged = llm.match_with_llm(q or ", ".join(skills), skills, [_profile(c) for c in rows])
        order = {m["id"]: i for i, m in enumerate(judged)}
        reasons = {m["id"]: m["reason"] for m in judged}
        rows = [c for c in rows if c.id in order]
    else:
        order = {}

    def relevance(c: Candidate):
        in_city = bool(filters.get("location") and c.location and filters["location"].lower() in c.location.lower())
        name_fit = 0.0
        if word:  # the closer the name is to what was typed, the higher it ranks
            name_fit = -fuzzy.get(c.id, SequenceMatcher(None, " ".join(_tokens(word)), " ".join(_tokens(c.name))).ratio())
        return (order.get(c.id, 10_000), name_fit, -(c.score or 0), not in_city, c.name.lower())

    if sort == "score":
        rows.sort(key=lambda c: (-(c.score or 0), c.name.lower()))
    elif sort == "name":
        rows.sort(key=lambda c: c.name.lower())
    elif sort == "experience":
        rows.sort(key=lambda c: -(c.experience_years or 0))
    else:
        rows.sort(key=relevance)

    total = len(rows)
    page = rows[offset: offset + limit]
    return {
        "q": q, "filters": filters, "total": total, "parser": "llm" if q.strip() or skills else None, "notice": notice,
        "results": [{**candidate_summary(c), "match": _explain(c, filters, reasons.get(c.id, ""))} for c in page],
        "suggestions": suggestions(db, filters, total),
    }


def suggestions(db: Session, f: dict, total: int) -> list[dict]:
    if f.get("keywords"):
        return []  # these counts ignore the AI skill judgment, so they would be wrong
    out = []
    if total == 0:  # nothing matched: say which single condition, if removed, would bring people back
        for key, label in [("role", "the role"), ("location", "the city"), ("max_experience", "the experience limit"),
                           ("min_experience", "the minimum experience"), ("min_score", "the score limit"),
                           ("status", "the status"), ("max_notice_days", "the notice period limit"), ("ready_to_relocate", "ready to relocate")]:
            if f.get(key) is None:
                continue
            n = _apply_filters(db.query(Candidate), {k: v for k, v in f.items() if k != key}).count()
            if n:
                out.append({"label": f"Remove {label} to see {n} {'person' if n == 1 else 'people'}?", "patch": {key: None}})
        if out:
            return out
    if f.get("location") and not f.get("include_relocators"):
        n = _apply_filters(db.query(Candidate), {**f, "include_relocators": True}).count()
        if n > total:
            out.append({"label": f"Also include {n - total} candidate(s) from other cities who are ready to relocate?",
                        "patch": {"include_relocators": True}})
    if f.get("min_score") is not None and f["min_score"] >= 1:
        lower = f["min_score"] - 1
        n = _apply_filters(db.query(Candidate), {**f, "min_score": lower}).count()
        if n > total:
            out.append({"label": f"Lower the score to {lower:g}+ to see {n - total} more?",
                        "patch": {"min_score": lower}})
    if f.get("max_experience") is not None and f.get("max_experience") >= 1:
        wider = f["max_experience"] + 1
        n = _apply_filters(db.query(Candidate), {**f, "max_experience": wider}).count()
        if n > total:
            out.append({"label": f"Widen experience to {wider:g} years to see {n - total} more?",
                        "patch": {"max_experience": wider}})
    return out


def candidate_summary(c: Candidate) -> dict:
    return {
        "id": c.id, "candidate_ref": c.candidate_ref, "name": c.name, "email": c.email, "phone": c.phone, "location": c.location,
        "best_role": c.best_role, "score": c.score, "experience_text": c.experience_text,
        "experience_years": c.experience_years, "status": c.status, "issues": c.issues or [],
        "ready_to_relocate": c.ready_to_relocate, "notice_period_days": c.notice_period_days,
        "expectation": c.expectation, "callable": bool(c.email or c.phone),
    }


def candidate_detail(c: Candidate) -> dict:
    d = candidate_summary(c)
    d.update({
        "resume_link": c.resume_link, "company": c.company, "reasoning": c.reasoning,
        "experience_hr": c.experience_hr, "communication": c.communication,
        "salary_in_hand": c.salary_in_hand, "negotiable": c.negotiable,
        "current_location": c.current_location, "notice_period_text": c.notice_period_text,
        "hr_notes": c.hr_notes, "applied_at": c.applied_at, "do_not_contact": c.do_not_contact,
        "merged_from": c.merged_from,
    })
    return d
