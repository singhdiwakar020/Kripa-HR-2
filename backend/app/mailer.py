"""Sends the test invitation (link + login + password) to a candidate by email over SMTP.

Settings in backend/.env:
  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD,
  SMTP_FROM (shown as sender; defaults to SMTP_USER), SMTP_FROM_NAME (default "Kripa HR"),
  SMTP_SECURITY = starttls (default, port 587) | ssl (port 465) | none
For Gmail use smtp.gmail.com with an App Password (not the normal password).
"""
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr


def enabled() -> bool:
    return bool(os.getenv("SMTP_HOST") and (os.getenv("SMTP_FROM") or os.getenv("SMTP_USER")))


def _send(msg: EmailMessage):
    host = os.getenv("SMTP_HOST", "")
    mode = (os.getenv("SMTP_SECURITY") or "starttls").lower()
    port = int(os.getenv("SMTP_PORT") or (465 if mode == "ssl" else 587))
    user, pw = os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASSWORD", "")
    ctx = ssl.create_default_context()
    if mode == "ssl":
        s = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx)
    else:
        s = smtplib.SMTP(host, port, timeout=20)
    with s:
        if mode == "starttls":
            s.starttls(context=ctx)
        if user:
            s.login(user, pw)
        s.send_message(msg)


def send_invite(to_email: str, name: str, exam_title: str, minutes: int, max_violations: int,
                url: str, password: str) -> str | None:
    """Returns None when sent, otherwise a short error text (never raises)."""
    if not enabled():
        return "Email is not set up (SMTP_HOST in backend/.env)."
    sender = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER")
    msg = EmailMessage()
    msg["Subject"] = f"Your online test: {exam_title}"
    msg["From"] = formataddr((os.getenv("SMTP_FROM_NAME") or "Kripa HR", sender))
    msg["To"] = to_email
    first = (name or "").strip().split(" ")[0] or "there"
    warn = f"leaving the full-screen window more than {max_violations} time{'s' if max_violations != 1 else ''} ends the test"
    msg.set_content(
        f"Hello {first},\n\n"
        f"You have been invited to take the online test \"{exam_title}\".\n\n"
        f"Test link: {url}\n"
        f"Login email: {to_email}\n"
        f"Password: {password}\n\n"
        f"Time allowed: {minutes} minutes. The clock starts when you press Start, so open the link only when you are ready.\n\n"
        f"Before you begin:\n"
        f"- Use a laptop or desktop with a current browser (Chrome or Edge).\n"
        f"- The test runs in full screen. Switching tabs or windows gives a warning, and {warn}.\n"
        f"- Your answers are submitted automatically when the time is up.\n\n"
        f"Please do not share this password. It only works for you.\n\n"
        f"Good luck,\nKripa HR\n"
    )
    try:
        _send(msg)
        return None
    except Exception as e:  # report to HR, do not break the invite
        return f"{type(e).__name__}: {str(e)[:160]}"
