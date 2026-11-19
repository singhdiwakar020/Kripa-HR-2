# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Kripa HR "Stage 1": upload a candidate spreadsheet (the "Career Form"), store it in SQLite, and search it in plain language. Backend is FastAPI + SQLAlchemy + SQLite in `backend/`; frontend is React 18 + Vite in `frontend/` (two pages: `UploadPage.jsx`, `SearchPage.jsx`). The directory is not a git repo.

## Commands

Backend (PowerShell, from `backend/`; a `.venv` already exists there):
```powershell
.venv\Scripts\python -m uvicorn app.main:app --reload   # API on http://127.0.0.1:8000
.venv\Scripts\python smoke_test.py                      # end-to-end flow on a throwaway DB
.venv\Scripts\python calls_test.py                      # call queue/retries/recalls with the built-in simulator
.venv\Scripts\python plivo_test.py                      # Plivo provider + webhooks with a fake Plivo client
.venv\Scripts\python exam_test.py                       # candidate tests: login, timer, violations, grading, results
.venv\Scripts\python make_sample.py                     # regenerate sample_career_form.xlsx
```
Frontend (from `frontend/`): `npm run dev` (http://localhost:5173, proxies `/api` to port 8000), `npm run build`.

There is no linter and no pytest suite. The three `*_test.py` / `smoke_test.py` files are plain scripts of asserts, each run on a throwaway DB and from `backend/` (they open `sample_career_form.xlsx` by relative path). There is no single-test selection.

Automatic screening calls: `calls.py` (queue, retries, recalls, scheduler thread) and `callengine.py` (provider-independent conversation) drive a provider chosen by `CALL_PROVIDER` (`mock` = simulator on the Calls page, default; `plivo` = `plivo_calls.py`, which places calls and serves the `/api/plivo/*` webhooks with Plivo XML). Real calls need `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN`, `PLIVO_PHONE_NUMBER` and a public https `PUBLIC_BASE_URL` (ngrok) in `backend/.env`; webhook signatures are verified unless `PLIVO_VERIFY_SIGNATURE=0`. Calling hours (10-19 IST), attempts (3) and retry gap (120 min) come from `CALL_*` env vars in `callengine.cfg()`. Live-mode safeguards (all in `cfg()`/`calls.py`): `CALL_MAX_CONCURRENT` (3 at once), `CALL_MAX_PER_DAY` (150 billable attempts), people who already completed the script or are Rejected are never queued, and `POST /api/calls/cancel-all` is the kill switch. `main.local_only` middleware serves everything except `/api/plivo/*` only on localhost, so the public ngrok address never exposes candidate data (add hosts via `ALLOWED_HOSTS` if deploying on purpose). Listening: `PLIVO_LISTEN=record` (default) records each answer, `plivo_calls.transcribe` sends it to OpenAI (`CALL_STT_MODEL`) in a background thread and `/api/plivo/next` hands Plivo the reply (Plivo's own speech recogniser, `PLIVO_LISTEN=speech`, returned mostly silence). `callengine.understand` classifies each reply with the LLM (`llm.understand`, facts from `QuestionSet.about`; `CALL_USE_LLM=0` or any LLM failure falls back to keyword rules). Only plain 10-digit Indian numbers are dialled; other country codes are stored as `+<digits>` and skipped.

Search needs `OPENAI_API_KEY` (and optional `OPENAI_MODEL`, default `gpt-4o-mini`) in `backend/.env`; restart the API after changing it. `smoke_test.py` monkeypatches `llm.parse_with_llm` / `llm.match_with_llm`, so it needs no key.

## Architecture

**Upload flow is a three-step, stateful handshake** (`main.py` + `importer.py`):
1. `POST /api/imports` parses the file and stores an `ImportBatch` (status `preview`) holding `headers`, `raw_rows`, and the auto-detected `mapping` as JSON. Nothing is written to `candidates` yet.
2. `POST /api/imports/{id}/preview` re-runs `importer.analyse` with a user-edited column mapping.
3. `POST /api/imports/{id}/confirm` runs `analyse` again and `importer.apply` writes candidates.

`analyse` is the core: it cleans each row (`build_record`), merges duplicates within the file by email/phone, then classifies each against the DB as new/updated/unchanged. Matching is by the sheet's Candidate ID (`candidate_ref`) first, then email, then phone, then (name, resume_link) for rows with neither; two different Candidate IDs are never merged even when they share an email or phone (the stored one is kept and the row gets an issue). The ID is also the first column of every export and of the call-results downloads. Cleaning problems become per-record `issues` strings (shown in UI, stored on the candidate) rather than rejections.

**Ownership rule:** `models.SHEET_FIELDS` lists fields a re-upload may update. `status`, `do_not_contact`, `issues`, etc. are system-owned and never overwritten by the sheet, except that a new candidate's initial status comes from the sheet's status column via `SHEET_TO_STATUS`. Adding a new sheet-derived column means touching `models.py` (column + `SHEET_FIELDS`), `importer.ALIASES`/`build_record`, `search.candidate_detail`, and `main.EXPORT_COLUMNS` as applicable.

**Search is a two-LLM-call pipeline** (`search.run_search` + `llm.py`):
1. `llm.parse_with_llm` turns the query into a strict-JSON-schema filter dict. It is constrained to real values (roles/locations come from `search.facets`, output is validated and anything invalid dropped). Cached with `lru_cache`.
2. Filters (merged with explicit query params, which win) are applied in SQL by `_apply_filters`.
3. If the filters include `keywords`, `llm.match_with_llm` judges the remaining candidates' profiles for skill fit, returning ordered ids plus reasons; non-matches are dropped. This is capped by `max_tokens=4096`, so broad filters with keywords can fail with an "narrow the filters" error.

LLM failures raise `LLMUnavailable`, which `main.py` maps to HTTP 503. Candidates with `do_not_contact` are excluded from all searches unconditionally. `suggestions()` skips keyword searches because its counts would ignore the LLM judgment.

Other notes: DB file is `backend/kripa.db`, created via `Base.metadata.create_all` at import of `main.py` (no migrations, so schema changes to existing tables require manual handling or deleting the DB). Every write of significance goes through `main.audit()` into `AuditLog`. `GET /api/export` writes an xlsx in the original sheet's column layout.

Voice: `tts.py` makes the caller's audio with ElevenLabs when `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID` are in `backend/.env` (model `ELEVENLABS_MODEL`, default `eleven_flash_v2_5`; `TTS_PROVIDER=polly` turns it off). Fixed lines (`callengine.fixed_lines`) are made while the phone rings (`PlivoProvider.place_call` -> `tts.prewarm`), cached in `backend/audio_cache/` and served unauthenticated at `/api/plivo/audio/<unguessable>.mp3`; replies made live wait at most `TTS_LIVE_TIMEOUT` (4 s), and any failure falls back to the Plivo voice (`PLIVO_VOICE`) with a 1 to 10 minute circuit breaker. Free ElevenLabs plans cannot use library voices through the API (error 402). After the script, `callengine` runs a `final_qa` step (only when the question set has "Company and job details") so the candidate can ask questions before the call closes.

Candidate tests ("Tests" tab): HR builds an `Exam` (MCQ + Python coding questions, time limit, negative marking, warnings allowed) in `TestsPage.jsx`, ticks candidates on Search and presses "Send test" (`SendTestModal.jsx`), which creates an `ExamInvite` per candidate with a random password (stored scrypt-hashed, shown to HR once; `exam.py`) and emails the link, login email and password through SMTP (`mailer.py`, `SMTP_*` in `backend/.env`; without SMTP, or if a send fails, HR still sees the password and hands it over; reset-password re-emails). Candidates use ONLY `/exam` (the single self-contained page `app/exam_page.html`, no HR code in it) and `/api/exam/*` (`exam_api.py`, session token signed with `EXAM_SECRET` or `backend/.exam_secret`; both are public in `main.local_only`, while `/api/exams/*` with an "s" is the HR side and stays local-only). The server owns the clock: `ends_at` is set at start, `exam.sweep` (run by the call scheduler thread) and every candidate request auto-submit overdue attempts. Leaving the tab/window/full screen is reported to `/api/exam/violation` (3 s debounce); more than `Exam.max_violations` ends the attempt as `terminated` and grades what was answered. MCQs are graded at submit; coding is graded in a background thread by `coderun.py`, which runs each test case in a throwaway Docker container (`python:3.12-slim`, no network, 128 MB, read-only) and needs Docker Desktop running. Without Docker, `grading="manual"` and HR enters marks in the attempt drawer. Candidate answers, hidden test cases and correct options are never sent to the browser. Login lockout (5 misses per email, 30 per address) is in memory. A test can be edited after candidates took it (`PUT /api/exams/{id}`): questions are updated in place by id so saved answers stay attached, `exam.regrade_finished` recomputes finished attempts (re-running code only for coding questions whose marks or cases changed), and the save is refused with 409 while anyone is `in_progress`. Coding answers: with Docker the test cases decide the marks and `aigrade.py` (OpenAI, `EXAM_AI_MODEL`, off with `EXAM_AI_GRADING=0`) adds written feedback; without Docker the AI reads the code and proposes the marks (`coding_detail[...]["by"] == "ai"`, HR can overrule by hand); with neither, or on an AI failure, the answer waits for manual marking. A test can be deleted at any stage (attempts and logins go with it).
