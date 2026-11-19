"""Sheet upload: read .xlsx/.csv, map columns, clean rows, merge duplicates, build a preview."""
import csv
import io
import re
from datetime import date, datetime

from openpyxl import load_workbook
from sqlalchemy.orm import Session

from .models import SHEET_FIELDS, BlockedContact, Candidate

DATE_MARK = "__date__:"

# field -> accepted header names (compared after lowercasing and stripping non-alphanumerics)
ALIASES = {
    "candidate_ref": ["candidateid", "candidateref", "candidatereference", "candidatecode", "candidatenumber", "candidateno", "applicantid"],
    "name": ["name", "candidatename", "fullname", "applicantname"],
    "email": ["email", "emailid", "emailaddress", "mail"],
    "phone": ["phone", "phonenumber", "mobile", "mobilenumber", "contact", "contactnumber", "contactdetails",
              "contactdetail", "contactno", "mobileno", "phoneno", "whatsapp", "whatsappnumber"],
    "resume_link": ["resumelink", "resume", "cv", "cvlink", "resumeurl", "driveurl", "resumedrivelink"],
    "experience_text": ["experience", "totalexperience", "experienceyears", "experienceinresume"],
    "company": ["company", "currentcompany", "organisation", "organization", "currentrecentcompany", "recentcompany"],
    "location": ["location", "city", "address"],
    "score": ["score", "aiscore", "screeningscore", "aiscreeningscore", "resumescore", "matchscore"],
    "best_role": ["bestmatchrole", "bestrole", "role", "jobrole", "appliedfor", "position", "bestmatch", "bestmatchjd",
                  "bestmatchjob", "bestmatchposition"],
    "reasoning": ["reasoning", "screeningreasoning", "aireasoning", "reason", "aireason", "scorereason"],
    "experience_hr": ["experiencehr", "hrexperience", "experience2"],
    "communication": ["communication", "communicationrating"],
    "salary_in_hand": ["salaryinhand", "currentsalary", "currentctc", "salary", "salaryinhandpermonth"],
    "expectation": ["expectation", "expectedsalary", "expectedctc", "salaryexpectation"],
    "negotiable": ["negotiable"],
    "current_location": ["currentlocation"],
    "ready_to_relocate": ["readytorelocate", "relocate", "willingtorelocate", "relocation"],
    "notice_period_text": ["noticeperiod", "notice"],
    "hr_notes": ["hrnotes", "notes", "remarks"],
    "applied_at": ["appliedat", "appliedon", "date", "timestamp", "applicationdate"],
    "status": ["status"],
}
FIELD_LABELS = {**{f: f.replace("_", " ").title() for f in ALIASES}, "experience_text": "Experience (from resume)",
                "experience_hr": "Experience (entered by HR)", "best_role": "Best-match role",
                "score": "AI screening score"}
SHEET_TO_STATUS = {
    "new": "In database", "contacted": "Called", "not responded": "Not reachable",
    "assignment submitted": "Test submitted", "rejected": "Rejected",
}
IGNORED_SHEETS = {"config", "ledger", "runs", "errors", "templates", "sent", "jds"}
RESUME_UNREAD = re.compile(r"(no text|could not (extract|read)|unreadable|scanned|resume not read)", re.I)
MONTHS = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
DATE_LIKE = re.compile(rf"^\s*(\d{{1,2}}[-/ ]{MONTHS}[a-z]*|{MONTHS}[a-z]*[-/ ]\d{{1,2}}|\d{{1,4}}[-/]\d{{1,2}}[-/]\d{{1,4}})\s*$", re.I)


def norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


# ---------------------------------------------------------------- reading files
def _cell(v):
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return DATE_MARK + v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def read_file(name: str, content: bytes):
    """Return (headers, rows). Duplicate headers get a '#2' suffix so nothing is lost."""
    lower = name.lower()
    if lower.endswith((".xlsx", ".xlsm")):
        wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        ws = None
        for n in wb.sheetnames:
            if norm_header(n) in ("careerform", "applicants", "candidates"):
                ws = wb[n]
                break
        if ws is None:
            for n in wb.sheetnames:
                if n.strip().lower() not in IGNORED_SHEETS:
                    ws = wb[n]
                    break
        if ws is None:
            raise ValueError("No usable tab found. Expected a 'Career Form' tab.")
        matrix = [[_cell(c) for c in row] for row in ws.iter_rows(values_only=True)]
    elif lower.endswith((".csv", ".txt")):
        text = content.decode("utf-8-sig", errors="replace")
        matrix = [[c.strip() for c in row] for row in csv.reader(io.StringIO(text))]
    else:
        raise ValueError("Upload an .xlsx or .csv file.")

    matrix = [r for r in matrix if any(c for c in r)]
    if len(matrix) < 2:
        raise ValueError("The file has no data rows.")
    seen, headers = {}, []
    for i, h in enumerate(matrix[0]):
        h = h or f"Column {i + 1}"
        seen[h] = seen.get(h, 0) + 1
        headers.append(h if seen[h] == 1 else f"{h}#{seen[h]}")
    rows = []
    for r in matrix[1:]:
        r = r + [""] * (len(headers) - len(r))
        rows.append(dict(zip(headers, r[: len(headers)])))
    return headers, rows


# ---------------------------------------------------------------- column mapping
def auto_map(headers: list[str]) -> dict[str, str | None]:
    """header -> field (or None when unmatched). A second 'Experience' column is the HR one."""
    mapping, used = {}, set()
    for h in headers:
        base, _, dup = h.partition("#")
        key = norm_header(base)
        field = None
        for f, names in ALIASES.items():
            if key in names:
                field = f
                break
        if field == "experience_text" and dup:
            field = "experience_hr"
        if field in used:
            field = None
        if field:
            used.add(field)
        mapping[h] = field
    return mapping


# ---------------------------------------------------------------- value cleaning
PHONE_CHUNK = re.compile(r"(?:\+|\b00)?\s*\d[\d\s().\-]{6,}\d")


def _one_phone(chunk: str) -> str | None:
    plus = chunk.lstrip().startswith(("+", "00"))
    digits = re.sub(r"\D", "", chunk)
    if chunk.lstrip().startswith("00"):
        digits = digits[2:]
    if plus and not digits.startswith("91"):
        # another country: keep the country code, shown as +<digits>. Not dialled automatically.
        return "+" + digits if 8 <= len(digits) <= 15 else None
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    elif len(digits) > 10:
        digits = digits[-10:]
    return digits if len(digits) == 10 else None


def norm_phone(v: str | None) -> str | None:
    """Find the first usable phone number in a cell. Indian numbers become 10 digits; a number with another
    country code is kept as +<digits>. Handles '+91-98765 43210', '098765 43210', '9876543210 / 8765432109' etc."""
    text = (v or "").strip()
    for m in PHONE_CHUNK.finditer(text):
        found = _one_phone(m.group())
        if found:
            return found
    digits = re.sub(r"\D", "", text)  # last resort: digits spread by odd separators
    return _one_phone(digits) if digits else None


def is_dialable(phone: str | None) -> bool:
    """Only plain Indian 10-digit numbers are called automatically."""
    return bool(phone) and phone.isdigit() and len(phone) == 10


def norm_email(v: str | None) -> str | None:
    v = (v or "").strip().lower()
    return v if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v) else None


def parse_float(v: str):
    m = re.search(r"-?\d+(?:\.\d+)?", (v or "").replace(",", ""))
    return float(m.group()) if m else None


def parse_years(text: str | None):
    t = (text or "").lower()
    if not t:
        return None
    if "fresher" in t or t.strip() in ("0", "none", "nil"):
        return 0.0
    y = re.search(r"(\d+(?:\.\d+)?)\s*(?:\+)?\s*(?:yrs?|years?|y\b)", t)
    mo = re.search(r"(\d+)\s*(?:mos?|months?)", t)
    if y or mo:
        return round((float(y.group(1)) if y else 0) + (int(mo.group(1)) / 12 if mo else 0), 2)
    if re.fullmatch(r"\s*\d+(\.\d+)?\s*", t):
        return float(t)
    if re.search(r"\bno\s+(paid\s+|work\s+|professional\s+|prior\s+|relevant\s+)?(jobs?|work|experience|roles?|internships?)\b|\bnone listed\b", t):
        return 0.0
    return None


def parse_notice(text: str | None):
    t = (text or "").lower()
    if not t:
        return None
    if "immediate" in t:
        return 0
    n = parse_float(t)
    if n is None:
        return None
    if "month" in t:
        return int(n * 30)
    if "week" in t:
        return int(n * 7)
    return int(n)


def parse_bool(v: str):
    t = (v or "").strip().lower()
    if t in ("yes", "y", "true", "1", "ready", "open"):
        return True
    if t in ("no", "n", "false", "0"):
        return False
    return None


def parse_rating(v: str):
    """Returns (rating, was_date_damaged)."""
    if v.startswith(DATE_MARK) or DATE_LIKE.match(v or ""):
        return None, True
    n = parse_float(v)
    return n, False


# ---------------------------------------------------------------- row -> record
def clean_ref(v) -> str | None:
    """The sheet's Candidate ID as text. Excel often turns 1042 into 1042.0, which must still match 1042."""
    s = re.sub(r"\s+", " ", str(v if v is not None else "")).strip()
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    return s[:100] or None


def build_record(row: dict, mapping: dict) -> dict:
    raw = {}
    for header, field in mapping.items():
        if field and field in ALIASES:
            raw[field] = row.get(header, "")
    issues = []
    rec = {k: None for k in SHEET_FIELDS}
    rec["candidate_ref"] = clean_ref(raw.get("candidate_ref"))
    rec["name"] = (raw.get("name") or "").strip()
    rec["email"] = norm_email(raw.get("email"))
    rec["phone"] = norm_phone(raw.get("phone"))
    if (raw.get("email") or "").strip() and not rec["email"]:
        issues.append("Invalid email")
    if (raw.get("phone") or "").strip() and not rec["phone"]:
        issues.append("Invalid phone")
    if rec["phone"] and rec["phone"].startswith("+"):
        issues.append("International number: not called automatically")
    if not rec["email"] and not rec["phone"]:
        issues.append("Missing email and phone: calling blocked")
    rec["resume_link"] = (raw.get("resume_link") or "").strip() or None
    rec["experience_text"] = (raw.get("experience_text") or "").strip() or None
    rec["experience_years"] = parse_years(rec["experience_text"])
    rec["experience_hr"] = (raw.get("experience_hr") or "").strip() or None
    rec["company"] = (raw.get("company") or "").strip() or None
    rec["location"] = (raw.get("location") or "").strip() or None
    rec["score"] = parse_float(raw.get("score") or "")
    if rec["score"] is not None and not 0 <= rec["score"] <= 10:
        issues.append("Score outside 0 to 10")
        rec["score"] = None
    rec["best_role"] = (raw.get("best_role") or "").strip() or None
    rec["reasoning"] = (raw.get("reasoning") or "").strip() or None
    comm, damaged = parse_rating(raw.get("communication") or "")
    rec["communication"] = comm
    if damaged:
        issues.append("Communication rating turned into a date: please correct")
    rec["salary_in_hand"] = parse_float(raw.get("salary_in_hand") or "")
    rec["expectation"] = parse_float(raw.get("expectation") or "")
    rec["negotiable"] = (raw.get("negotiable") or "").strip() or None
    rec["current_location"] = (raw.get("current_location") or "").strip() or None
    rec["ready_to_relocate"] = parse_bool(raw.get("ready_to_relocate") or "")
    rec["notice_period_text"] = (raw.get("notice_period_text") or "").strip() or None
    rec["notice_period_days"] = parse_notice(rec["notice_period_text"])
    rec["hr_notes"] = (raw.get("hr_notes") or "").strip() or None
    applied = (raw.get("applied_at") or "").replace(DATE_MARK, "")
    rec["applied_at"] = applied or None
    if not rec["resume_link"] or RESUME_UNREAD.search(rec["reasoning"] or ""):
        issues.append("Resume not read")
    if rec["best_role"] is None:
        issues.append("No best-match role")
    status_raw = (raw.get("status") or "").strip().lower()
    rec["_status"] = SHEET_TO_STATUS.get(status_raw)
    rec["_issues"] = issues
    return rec


def _merge(base: dict, other: dict):
    for k, v in other.items():
        if k == "_issues":
            base[k] = list(dict.fromkeys(base[k] + v))
        elif base.get(k) in (None, "") and v not in (None, ""):
            base[k] = v
        elif k == "score" and v is not None and base.get(k) is not None and v > base[k]:
            base[k] = v


def analyse(rows: list[dict], mapping: dict, db: Session) -> dict:
    """Clean every row, merge repeats, compare with the database. Returns records plus the preview."""
    records, skipped = [], []
    for i, row in enumerate(rows, start=2):
        rec = build_record(row, mapping)
        rec["_row"] = i
        if not rec["name"] and not rec["email"] and not rec["phone"]:
            skipped.append({"row": i, "reason": "Empty row"})
            continue
        records.append(rec)

    # merge repeats inside the file: the same Candidate ID first, then email or phone. Two different IDs are two different
    # people even if they share a phone or email.
    merged, by_ref, by_email, by_phone, merges = [], {}, {}, {}, []
    for rec in records:
        ref = rec["candidate_ref"]
        target = by_ref.get(ref) if ref else None
        if target is None:
            target = (by_email.get(rec["email"]) if rec["email"] else None) or                      (by_phone.get(rec["phone"]) if rec["phone"] else None)
            if target and ref and target.get("candidate_ref") and target["candidate_ref"] != ref:
                target = None
        if target:
            before = target["_row"]
            _merge(target, rec)
            target["_merged_rows"] = target.get("_merged_rows", [before]) + [rec["_row"]]
            merges.append({"name": target["name"] or rec["name"], "rows": target["_merged_rows"],
                           "key": ref or rec["email"] or rec["phone"]})
            rec = target
        else:
            merged.append(rec)
        if rec["candidate_ref"]:
            by_ref[rec["candidate_ref"]] = rec
        if rec["email"]:
            by_email[rec["email"]] = rec
        if rec["phone"]:
            by_phone[rec["phone"]] = rec

    # compare with database
    existing_ref = {c.candidate_ref: c for c in db.query(Candidate).filter(Candidate.candidate_ref.isnot(None))}
    existing_email = {c.email: c for c in db.query(Candidate).filter(Candidate.email.isnot(None))}
    existing_phone = {c.phone: c for c in db.query(Candidate).filter(Candidate.phone.isnot(None))}
    existing_nokey = {(c.name.lower(), c.resume_link): c for c in
                      db.query(Candidate).filter(Candidate.email.is_(None), Candidate.phone.is_(None))}
    counts = {"new": 0, "updated": 0, "unchanged": 0, "need_fix": 0}
    for rec in merged:
        match = existing_ref.get(rec["candidate_ref"]) if rec["candidate_ref"] else None  # the Candidate ID decides first
        if match is None:
            match = existing_email.get(rec["email"]) or existing_phone.get(rec["phone"])
            if match and rec["candidate_ref"] and match.candidate_ref and match.candidate_ref != rec["candidate_ref"]:
                match = None  # same email or phone but another Candidate ID: a different person, kept separate
                rec["_issues"].append("Same email or phone as another candidate with a different Candidate ID")
        if match is None and not rec["email"] and not rec["phone"]:
            match = existing_nokey.get((rec["name"].lower(), rec["resume_link"]))
        rec["_match_id"] = match.id if match else None
        if match is None:
            rec["_action"] = "new"
        else:
            changed = [f for f in SHEET_FIELDS if rec.get(f) not in (None, "") and getattr(match, f) != rec[f]]
            rec["_action"] = "updated" if changed else "unchanged"
        counts[rec["_action"]] += 1
        if rec["_issues"]:
            counts["need_fix"] += 1

    return {"records": merged, "counts": counts, "merges": merges, "skipped": skipped, "total_rows": len(rows)}


def preview_payload(batch, result: dict) -> dict:
    mapping = batch.mapping
    unmatched = [h for h, f in mapping.items() if not f]
    issue_rows = [
        {"row": r["_row"], "name": r["name"] or "(no name)", "issues": r["_issues"]}
        for r in result["records"] if r["_issues"]
    ]
    issue_summary: dict[str, int] = {}
    for r in issue_rows:
        for i in r["issues"]:
            issue_summary[i] = issue_summary.get(i, 0) + 1
    roles: dict[str, int] = {}
    for r in result["records"]:
        roles[r["best_role"] or "(none)"] = roles.get(r["best_role"] or "(none)", 0) + 1
    return {
        "batch_id": batch.id, "file_name": batch.file_name, "status": batch.status,
        "headers": batch.headers, "mapping": mapping, "unmatched_columns": unmatched,
        "field_options": [{"field": f, "label": FIELD_LABELS.get(f, f)} for f in ALIASES],
        "total_rows": result["total_rows"],
        "counts": {**result["counts"], "merged": len(result["merges"]), "skipped": len(result["skipped"])},
        "merges": result["merges"][:200], "skipped": result["skipped"][:50],
        "issue_summary": issue_summary, "issue_rows": issue_rows[:300],
        "roles": roles,
        "sample": [{k: v for k, v in r.items() if not k.startswith("_")} for r in result["records"][:5]],
        "missing_core": [f for f in ("name", "email", "phone", "score", "best_role") if f not in mapping.values()],
    }


def apply(batch, result: dict, db: Session) -> dict:
    """Write records. Status, do-not-contact and anything entered in the system are never overwritten."""
    added = updated = 0
    blocked = db.query(BlockedContact).all()
    b_email = {b.email for b in blocked if b.email}
    b_phone = {b.phone for b in blocked if b.phone}
    for rec in result["records"]:
        data = {f: rec[f] for f in SHEET_FIELDS}
        if rec["_action"] == "new":
            status = rec["_status"] or "In database"
            dnc = bool((rec["email"] and rec["email"] in b_email) or (rec["phone"] and rec["phone"] in b_phone))
            if dnc:
                status = "Do not contact"
            db.add(Candidate(**data, status=status, issues=rec["_issues"], do_not_contact=dnc,
                             merged_from=len(rec.get("_merged_rows", [])), import_batch_id=batch.id))
            added += 1
        elif rec["_action"] == "updated":
            c = db.get(Candidate, rec["_match_id"])
            for f, v in data.items():
                if v not in (None, ""):
                    setattr(c, f, v)
            c.issues = rec["_issues"]
            updated += 1
    batch.status = "confirmed"
    batch.rows_added, batch.rows_updated = added, updated
    batch.rows_skipped = len(result["skipped"])
    batch.rows_need_fix = sum(1 for r in result["records"] if r["_issues"])
    batch.rows_merged = len(result["merges"])
    return {"added": added, "updated": updated, "skipped": batch.rows_skipped,
            "need_fix": batch.rows_need_fix, "merged": batch.rows_merged}
