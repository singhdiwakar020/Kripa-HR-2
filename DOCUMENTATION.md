# Kripa HR: Documentation

This guide has two parts. Sections 1 to 4 are for the people who **use** the app (HR). Sections 5 to 9 are for the people who **run or change** it.

**Contents**

1. [Overview](#1-overview)
2. [Setup](#2-setup)
3. [Using the app](#3-using-the-app)
4. [Online tests in detail](#4-online-tests-in-detail)
5. [How it works inside](#5-how-it-works-inside)
6. [API reference](#6-api-reference)
7. [Configuration reference](#7-configuration-reference)
8. [Security and privacy](#8-security-and-privacy)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Overview

Kripa HR takes a candidate spreadsheet (the "Career Form") through four steps:

```
 Upload sheet  ->  Search in plain language  ->  Screening call  ->  Online test
 (merge, clean)    (AI filters + skill fit)      (AI caller)         (MCQ + coding)
```

Everything is stored in one SQLite file (`backend/kripa.db`). There is no cloud database and no login for HR, because the HR screens only answer on the computer that runs the app.

**Parts**

| Part | Technology | Folder |
|---|---|---|
| API and business logic | FastAPI, SQLAlchemy, SQLite | `backend/app/` |
| HR screens | React 18, Vite | `frontend/src/` |
| Candidate test page | One self-contained HTML page | `backend/app/exam_page.html` |
| Phone calls | Plivo (calls), OpenAI (understanding, speech to text), ElevenLabs (voice) | `plivo_calls.py`, `callengine.py`, `tts.py` |
| Coding marks | Docker (runs code) and OpenAI (reads code) | `coderun.py`, `aigrade.py` |

---

## 2. Setup

### Requirements

- Python 3.11 or newer
- Node.js 18 or newer
- Optional, by feature: an OpenAI key, a Plivo account, an ElevenLabs account, a mail account (Gmail works), Docker Desktop, ngrok

### Install and run

```powershell
# Backend (terminal 1)
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python -m uvicorn app.main:app --reload      # http://127.0.0.1:8000

# Frontend (terminal 2)
cd frontend
npm install
npm run dev                                                 # http://localhost:5173
```

The frontend proxies `/api` to port 8000. Restart the API after any change to `backend/.env`. `npm run build` in `frontend/` checks that the screens compile; it writes `frontend/dist/`, but the API does not serve it, so keep `npm run dev` running to use the HR screens.

### First run checklist

1. Put `OPENAI_API_KEY` in `backend/.env`.
2. Open http://localhost:5173 and go to **Upload**.
3. Upload `backend/sample_career_form.xlsx`, check the preview, and confirm.
4. Go to **Search** and try: `candidates in Bhopal with experience above 2 years`.

### Turning on the optional features

| Feature | What to do |
|---|---|
| **Real phone calls** | Create a Plivo account and a phone number. Set `CALL_PROVIDER=plivo`, `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN`, `PLIVO_PHONE_NUMBER`. Run `ngrok http 8000` and set `PUBLIC_BASE_URL` to the https address it prints. Restart the API. |
| **Natural voice** | Set `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID`. A free ElevenLabs plan cannot use library voices through the API, so use a voice you created or a paid plan. |
| **Emailing test logins** | Set `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`. For Gmail: host `smtp.gmail.com`, port `587`, and a Google **App Password** (needs 2-Step Verification). |
| **Exact coding marks** | Install Docker Desktop, start it, and run `docker pull python:3.12-slim` once. |

---

## 3. Using the app

### 3.1 Upload

1. Open **Upload** and choose an `.xlsx` or `.csv` file.
2. The app guesses which column is which (name, email, phone, Candidate ID, location and so on). Fix any wrong guess with the dropdowns and the preview updates.
3. The preview sorts every row into **new**, **updated**, **unchanged** or **needs a look**. Nothing is saved yet.
4. Press **Confirm**. The candidates are saved.

**How duplicates are handled**

Rows are matched, in this order, by: **Candidate ID**, then **email**, then **phone**, then name plus resume link (only for rows with neither email nor phone).

- Two rows in the same file with the same match are merged into one.
- A row that matches someone already stored updates that person.
- Two different Candidate IDs are **never** merged, even if they share an email or phone. The stored person is kept and the row is flagged.
- Problems (a bad phone number, a missing email) never reject a row. They appear as notes on the candidate.

**What a re-upload can change.** Only the sheet's own fields (name, contact, location, score, notice period and similar). Fields the system or HR owns are never overwritten: status, do-not-contact, call results, test results, notes.

**Clear all data…** on the Upload page (you must type a confirmation word) wipes everything except the do-not-contact list, so people who opted out are still never called after a re-upload.

### 3.2 Search

Type what you need in your own words.

> *Python developers in Indore, 2 to 4 years, salary under 6 lakh, can join within 30 days*

How it works: the AI turns the sentence into filters (role, location, experience, salary, notice period, status, skills). Only real values from your data are used. Filters are applied, and when you ask for skills, the AI reads each remaining profile and keeps the ones that fit, with a reason.

- Use the filter chips to adjust the result.
- **Save** (next to the "Saved searches" list) names and keeps the current search for later.
- Click a candidate to see their full record, call history and notes, and to edit HR fields.
- **Do not contact** removes a person from every search, call and test, permanently.
- **Export** downloads an Excel file in the original sheet's layout, Candidate ID first.
- Tick people, then press **Call** (screening calls) or **Send test** (online test).

If a search with skills is too broad, narrow the filters first. The AI judges a limited number of profiles at a time.

### 3.3 Calls

**Question sets.** Under *Calls → Question sets*, build the script:

- **Greeting** and **closing** lines.
- **Questions**, each with an answer type: free text, a number (for example salary), or yes/no.
- **Company and job details.** Write what a candidate may ask about: the company, the role, location, salary range, timings. The caller answers questions from **only** this box. It does not guess. Fill it in properly.

**Starting calls.** Tick candidates on Search, press **Call**, choose a question set, and start. Calls are queued and placed automatically inside calling hours.

**What the caller does**

1. Greets and checks it is a good time. If not, it asks when to call back and books it.
2. Asks each question and records the answer. Hindi, English and Hinglish are understood. Spoken numbers ("tees hazaar", "thirty days") become digits.
3. If a company and job details box exists, it asks "Is there anything else you would like to know?", answers from the box, and closes politely when the candidate says no.
4. Ends with the closing line.

**Outcomes and retries**

| Outcome | What happens |
|---|---|
| Answered all questions | Job is done. Candidate status becomes **Called**. |
| Asked to be called later | A call-back is booked for the time they said. It does not use up a retry. |
| No answer, busy, failed, hung up early | Retried after the retry gap, up to the maximum attempts, then **Not reachable**. |
| Said not interested | Done, recorded. |
| Asked not to be called again | Marked **do not contact** automatically. |

**Safety limits** (all adjustable, see [section 7](#7-configuration-reference)): calling hours 10:00 to 19:00 IST, 3 attempts, 120 minutes between attempts, 3 calls at once, 150 call attempts a day. People who already answered the questions, are Rejected, or are do-not-contact are never queued. **Stop all calls** cancels everything waiting and ends calls in progress.

Only plain 10-digit Indian mobile numbers are dialled. Other numbers are stored and skipped.

**Results.** The Calls page lists every call with the transcript, answers and outcome, and exports to Excel with Candidate ID first.

**Simulator.** With `CALL_PROVIDER=mock` (the default) no real call is made. You play the candidate on screen to test your script for free.

### 3.4 Tests

See the next section.

---

## 4. Online tests in detail

### 4.1 Build a test

*Tests → + New test*

| Field | Meaning |
|---|---|
| Title | Shown to candidates and in results. |
| Time limit | 5 to 480 minutes. The clock starts when the candidate presses Start. |
| Wrong MCQ answer costs | A share of the question's marks lost for a wrong answer: none, a quarter, a third, half, or all. A blank answer costs nothing. The MCQ total never goes below 0. |
| Warnings before the test ends | 0 to 4. See the rules below. |
| Instructions | Shown on the candidate's first screen. |
| Show candidates their score | Whether a score appears after they submit. |

**Multiple-choice questions.** 2 to 8 choices. Tick one right answer, or several for a "pick all that apply" question (the candidate must pick exactly the right set).

**Coding questions (Python).** Write the problem, optional starter code, and test cases. Each test case has an input and the exact expected output. Tick **Hidden** for cases the candidate must not see. At least one case must stay visible so they can try their code.

### 4.2 Send the test

1. On **Search**, tick candidates and press **Send test**.
2. Choose the test and press **Create logins**. Each candidate needs an email address on their record.
3. Each candidate gets their own password, and an email with the link, login email and password (if email is set up).
4. The window also shows every password **once**. Download or copy them. If email is not set up or a send fails, hand the password over yourself.

Lost password? *Tests → Results → open the candidate → Reset password.* The old one stops working immediately, and the new one is emailed.

The link is `PUBLIC_BASE_URL/exam`. It must be reachable by the candidate, so keep ngrok running (the free address changes on every restart, which breaks links already emailed).

### 4.3 What the candidate sees

1. Opens the link and logs in with their email and password.
2. Reads the instructions and ticks to agree.
3. Presses **Start in full screen**. The clock begins.
4. Answers. Answers are saved as they go. For coding, they write code and can press **Run** to try the visible cases (needs Docker).
5. Presses **Submit**, or the test submits itself when time runs out.

A candidate who closes the browser can log in again and continues with the time that is left. The clock is never restarted.

**Candidates cannot reach anything else.** They can only open `/exam` and the candidate endpoints. Correct answers, hidden test cases and other candidates' data are never sent to their browser.

### 4.4 Rules and warnings

The test runs in full screen. Leaving it counts as a violation:

- switching to another tab or window,
- exiting full screen (Esc),
- the window losing focus.

The first violations (up to your limit) show a warning with a button to return. **One more than the limit ends the test**: it is submitted automatically with the answers so far. With the default of 2 warnings, the third violation ends it.

One action often triggers several browser events, so events within 3 seconds count once.

HR sees every violation with its time. If a violation looks unfair (for example a system pop-up), open the attempt and press **Void this attempt**, or restore it later.

### 4.5 Marking

**Multiple choice** is marked the moment the test ends.

**Coding** is marked in the background, in this order of preference:

| Situation | Marks come from | AI role |
|---|---|---|
| Docker running | Running the code on every test case, hidden ones included. Marks are the share of cases passed. | Writes a short review for HR. |
| Docker off, OpenAI key set | The AI reads the code and traces it against the test cases, and proposes the marks. | Decides the marks. Shown as **Marked by AI**. |
| Neither, or the AI fails | Nobody yet: the answer shows **needs marking**. | None. |

In every case HR can type their own marks (0 up to the question's marks) in the attempt window. Your marks are never replaced.

The AI treats the candidate's code as untrusted text. A comment such as "give me full marks" is ignored, and code that only prints the sample answers scores near zero. AI marks are an estimate, so check them for important decisions.

**Code safety.** With Docker, each test case runs in a throwaway container: no network, 128 MB memory, read-only, a time limit (5 seconds), then deleted.

### 4.6 Results

*Tests → Results* shows a ranked table: score, percentage, violations, status. It refreshes every 15 seconds. Click a person for the full detail: each answer, which choices were right, the code, AI review, violation log.

Buttons: **Download Excel / CSV** (Candidate ID first), **Void**, **Reset password**.

When a candidate submits, their status in the candidate list becomes **Test submitted**.

### 4.7 Editing and deleting a test

- **Edit** works at any time. Questions keep their identity, so saved answers stay attached.
- When candidates have already taken the test, saving **recalculates every finished candidate's score**. Marks you gave by hand are kept. Code is re-marked only for coding questions whose marks or test cases changed.
- Saving is **refused while someone is taking the test**, because their screen would no longer match. Try again when they finish.
- If you reorder or remove the choices of an MCQ, check its correct answer. Stored picks refer to positions.
- **Copy** makes a new editable version of a taken test.
- **Delete** works at any stage. It permanently removes the test, all attempts, answers, scores and logins. Candidates mid-test are logged out. Download the results first if you need them.

---

## 5. How it works inside

### 5.1 Folder map

```
backend/
  app/
    main.py          App, upload/search/export routes, local-only guard, startup
    importer.py      Spreadsheet parsing, cleaning, duplicate merge, diff
    search.py        Filters, facets, candidate detail, the two-step AI search
    llm.py           All OpenAI calls (search filters, skill match, call understanding)
    models.py        Database tables
    calls.py         Call queue, retries, call-backs, scheduler thread
    callengine.py    The conversation (provider independent)
    plivo_calls.py   Plivo provider and webhooks
    calls_api.py     Calls screens' API and the simulator
    tts.py           ElevenLabs voice, cache, fallback
    exam.py          Test logic: passwords, sessions, timer, marking
    exam_api.py      Candidate API and HR API for tests
    exam_page.html   The candidate's test page
    coderun.py       Runs code in Docker
    aigrade.py       AI review of coding answers
    mailer.py        Test invitation emails
  *_test.py          Plain assert scripts (no pytest)
frontend/src/        React screens: Upload, Search, Calls, Tests and modals
```

### 5.2 Data model

| Table | Holds |
|---|---|
| `import_batches` | Each upload: file name, raw rows, column mapping, counts. |
| `candidates` | One row per person. Sheet fields plus system fields (status, do-not-contact, issues). |
| `saved_searches` | Searches HR saved. |
| `question_sets`, `questions` | Call scripts. |
| `call_jobs`, `call_records` | The queue (one job per person) and each attempt with transcript and answers. |
| `exams`, `exam_questions` | A test and its questions. |
| `exam_invites` | One candidate's login and attempt: scrambled password, answers, violations, scores. |
| `blocked_contacts` | Do-not-contact emails and phones. Survives "Clear all data". |
| `audit_log` | Every significant write: who, what, when. |

**Candidate status** moves through: *In database → Called → Test submitted*, or *Not reachable*, *Rejected*, *Do not contact*.

The schema is created at startup (`create_all`). New columns on existing tables are added automatically for the few that have changed. For any other schema change on an existing database, add the column by hand or start with a fresh file.

### 5.3 The upload handshake

```
POST /api/imports              parse file, store batch (status "preview"), auto-detect mapping
POST /api/imports/{id}/preview re-run analysis with an edited column mapping
POST /api/imports/{id}/confirm analyse again, then write candidates
```

`importer.analyse` does the work: clean each row, merge duplicates inside the file, then classify each row against the database as new, updated or unchanged. Only fields in `models.SHEET_FIELDS` are ever updated from a sheet. To add a new sheet-derived column, change `models.py` (column and `SHEET_FIELDS`), `importer.ALIASES` and `build_record`, `search.candidate_detail`, and `main.EXPORT_COLUMNS`.

### 5.4 Search pipeline

1. `llm.parse_with_llm` turns the query into a strict JSON filter. It may only use real roles and locations from your data. Anything invalid is dropped. Results are cached.
2. The filters (merged with filters chosen in the UI, which win) run in SQL.
3. If skills were asked for, `llm.match_with_llm` reads the remaining profiles and returns the matches in order with reasons.

If OpenAI fails, the API returns 503 with a clear message. Do-not-contact people are excluded from every search.

### 5.5 Call engine

`callengine.reply` is a small state machine: availability, ask when, confirm when, question, final Q&A. Each reply from the candidate goes through:

1. A fast path for obvious answers (yes, no, a bare number) so the line stays quick.
2. `llm.understand`, which classifies the reply (agree, answer, question, busy, call-back time, opt out, unclear) and, for questions, answers **only** from the company details box.
3. A keyword fallback when the AI is off or fails (`CALL_USE_LLM=0` or any error).

**Audio.** Plivo records each answer, `gpt-4o-mini-transcribe` turns it into text (with a hint to write Hindi in Roman letters), and the reply is spoken back. With ElevenLabs set up, fixed lines (greeting, questions, closing) are generated while the phone rings, cached in `backend/audio_cache/` and played instantly. Replies made live wait at most `TTS_LIVE_TIMEOUT` (4 s). If ElevenLabs fails, the Plivo voice takes over for a while (a circuit breaker of 1 to 10 minutes).

**Scheduler.** One background thread (`calls.start_scheduler`) places due calls every 10 seconds, expires stale calls, and auto-submits overdue tests.

### 5.6 Test engine

- **Passwords** are random, shown once, and stored only as a scrypt hash.
- **Sessions** are signed tokens (HMAC) using `EXAM_SECRET`, or a key saved in `backend/.exam_secret` on first use.
- **The server owns the clock.** `ends_at` is set when the test starts. Every candidate request and the scheduler check it, so a closed browser still gets auto-submitted at the right time.
- **Violations** are reported by the page, debounced for 3 seconds, and counted on the server. The page cannot reduce the count.
- **Lockout:** 5 wrong passwords per email, or 30 per network address, lock login for a while (kept in memory, cleared on restart).
- **Public and private split.** `main.local_only` allows `/exam` and `/api/exam/*` (candidate side) from anywhere, while `/api/exams/*` (with an "s", the HR side) answers only on the local computer.

### 5.7 Tests (for developers)

Run from `backend/`:

```powershell
.venv\Scripts\python smoke_test.py    # upload, Candidate ID merge, search, export
.venv\Scripts\python calls_test.py    # queue, retries, Hindi numbers, final Q&A
.venv\Scripts\python plivo_test.py    # provider, webhooks, ElevenLabs flow, fallbacks
.venv\Scripts\python exam_test.py     # login, timer, violations, grading, editing, AI marking, delete
```

Each runs on a throwaway database and needs no API keys. They open `sample_career_form.xlsx` by relative path, so run them from `backend/`. There is no single-test selection. On Windows set `PYTHONIOENCODING=utf-8` if the console complains about Hindi text.

---

## 6. API reference

HR routes answer on `localhost` only. Interactive docs are at http://127.0.0.1:8000/docs.

### Upload and candidates

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/imports` | Upload a file, get a preview |
| POST | `/api/imports/{id}/preview` | Re-preview with a new column mapping |
| POST | `/api/imports/{id}/confirm` | Save the candidates |
| GET | `/api/imports` | Upload history |
| POST | `/api/reset` | Clear all data (keeps the do-not-contact list) |
| GET | `/api/facets` | Roles, locations and statuses in your data |
| GET | `/api/search` | Search. `q` for plain language, plus explicit filters |
| GET / PATCH | `/api/candidates/{id}` | Read or edit a candidate |
| POST | `/api/candidates/{id}/do-not-contact` | Block a person for good |
| GET / POST / DELETE | `/api/saved-searches` | Saved searches |
| GET | `/api/export` | Excel export in the sheet's layout |

### Calls

| Method | Path | Purpose |
|---|---|---|
| GET / POST / PUT / DELETE | `/api/question-sets` | Manage scripts |
| POST | `/api/calls/start` | Queue calls for candidates |
| GET | `/api/calls` | Queue and recent calls |
| POST | `/api/calls/jobs/{id}/cancel` | Cancel one |
| POST | `/api/calls/cancel-all` | **Kill switch** |
| POST | `/api/calls/jobs/{id}/call-now` | Call now |
| GET | `/api/calls/records/{id}` | One call's transcript and answers |
| GET | `/api/calls/results` · `/api/calls/export` | Results and Excel download |
| POST | `/api/calls/records/{id}/sim/*` | Simulator controls (mock provider) |

### Phone provider webhooks (public)

`/api/plivo/answer`, `input`, `record`, `next`, `machine`, `hangup` and `/api/plivo/audio/{name}.mp3`. Plivo signatures are checked unless `PLIVO_VERIFY_SIGNATURE=0`. Audio file names are unguessable.

### Tests, HR side (localhost only)

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/exams` | List or create |
| GET / PUT / DELETE | `/api/exams/{id}` | Read, edit (recalculates finished attempts), delete |
| POST | `/api/exams/{id}/invite` | Create logins and email them. Body: `{"candidate_ids": [..]}` |
| POST | `/api/exams/invites/{id}/reset-password` | New password |
| GET | `/api/exams/{id}/results` | Ranked results |
| GET | `/api/exams/{id}/export` | Excel or `?format=csv` |
| GET / PATCH | `/api/exams/invites/{id}` | Attempt detail. PATCH marks coding or voids |

### Tests, candidate side (public)

| Method | Path | Purpose |
|---|---|---|
| GET | `/exam` | The test page |
| POST | `/api/exam/login` | Email + password, returns a session token |
| GET | `/api/exam/me` | State of my attempt |
| POST | `/api/exam/start` | Start (or continue) |
| PUT | `/api/exam/answer` | Save an answer |
| POST | `/api/exam/violation` | Report leaving the window |
| POST | `/api/exam/run` | Run code on the visible cases |
| POST | `/api/exam/submit` | Submit |

---

## 7. Configuration reference

All settings live in `backend/.env`. Restart the API after changing them. `.env.example` is the template.

### AI

| Variable | Default | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | | Needed for search, call understanding, speech to text and AI marking |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model for search |
| `CALL_LLM_MODEL` | `OPENAI_MODEL` | Model for understanding call replies |
| `CALL_LLM_TIMEOUT` | `6` | Seconds to wait before falling back to keyword rules |
| `CALL_USE_LLM` | `1` | `0` uses only keyword rules on calls |
| `CALL_STT_MODEL` | `gpt-4o-mini-transcribe` | Speech to text |
| `EXAM_AI_MODEL` | `OPENAI_MODEL` | Model for marking coding answers |
| `EXAM_AI_GRADING` | `1` | `0` turns AI marking off |

### Calls

| Variable | Default | Meaning |
|---|---|---|
| `CALL_PROVIDER` | `mock` | `mock` (simulator) or `plivo` (real calls) |
| `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN`, `PLIVO_PHONE_NUMBER` | | Plivo account and the number calls come from |
| `PUBLIC_BASE_URL` | | Public https address (ngrok) for webhooks and test links |
| `PLIVO_VERIFY_SIGNATURE` | `1` | `0` skips webhook signature checks (testing only) |
| `PLIVO_LISTEN` | `record` | `record` (recommended) or `speech` |
| `PLIVO_VOICE` | | Fallback voice |
| `CALL_HOURS_START`, `CALL_HOURS_END` | `10`, `19` | Calling window, IST |
| `CALL_IGNORE_HOURS` | `1` for mock, else `0` | `1` ignores the window |
| `CALL_MAX_ATTEMPTS` | `3` | Tries per person |
| `CALL_RETRY_MINUTES` | `120` | Gap between tries |
| `CALL_MAX_CONCURRENT` | `3` (real) | Calls at the same time |
| `CALL_MAX_PER_DAY` | `150` (real) | Daily limit of billable attempts |
| `CALL_STALE_MINUTES` | `10` | When a stuck call is closed |

### Voice

| Variable | Default | Meaning |
|---|---|---|
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` | | Turns on the ElevenLabs voice |
| `ELEVENLABS_MODEL` | `eleven_flash_v2_5` | Fast model |
| `TTS_PROVIDER` | | `polly` turns ElevenLabs off |
| `TTS_LIVE_TIMEOUT` | `4` | Max seconds to wait for a live reply's audio |

### Tests

| Variable | Default | Meaning |
|---|---|---|
| `EXAM_SECRET` | auto | Signing key for candidate sessions. Set it to keep sessions valid across machines |
| `EXAM_PYTHON_IMAGE` | `python:3.12-slim` | Docker image for running code |
| `EXAM_RUN_TIMEOUT` | `5` | Seconds allowed per test case |
| `EXAM_RUN_PARALLEL` | `3` | Test cases run at the same time |

### Email

| Variable | Default | Meaning |
|---|---|---|
| `SMTP_HOST` | | Mail server. Email is off without it |
| `SMTP_PORT` | `587` | `465` for SSL |
| `SMTP_SECURITY` | `starttls` | `starttls`, `ssl` or `none` |
| `SMTP_USER`, `SMTP_PASSWORD` | | Login. Gmail needs an App Password |
| `SMTP_FROM` | `SMTP_USER` | Sender address |
| `SMTP_FROM_NAME` | `Kripa HR` | Sender name |

### Network

| Variable | Default | Meaning |
|---|---|---|
| `ALLOWED_HOSTS` | | Extra host names allowed to reach HR screens, if you deliberately deploy to a server |

---

## 8. Security and privacy

- **HR screens are local only.** `main.local_only` serves everything except the phone webhooks, the candidate test page and its endpoints only when the request comes from localhost. A public ngrok address never exposes candidate data.
- **There is no HR login.** The protection is that the HR side answers only on the computer running it. If you deploy it on a server for several people, you must add authentication and set `ALLOWED_HOSTS` deliberately first.
- **Secrets** (`.env`, `.exam_secret`) and data (`kripa.db`, backups, call audio) are in `.gitignore`. Never commit them.
- **Passwords** for tests are random, one per candidate, stored hashed, shown once.
- **Test integrity.** The server owns the clock and the violation count. Correct answers and hidden test cases never reach the browser. Submitted code runs only inside a network-less Docker container. The AI marker ignores instructions written inside candidate code.
- **Candidate data sent outside.** OpenAI receives candidate profiles for search, call replies for understanding and marking, and call audio for transcription. ElevenLabs receives only the script text. Plivo carries the calls. Review these against your privacy policy and what candidates agreed to.
- **Do not contact** is permanent and survives a data wipe.
- **Backups.** Copy `backend/kripa.db` to back up everything. Keep backups out of the repository.

---

## 9. Troubleshooting

| Problem | Likely cause and fix |
|---|---|
| Search says the AI is unavailable (503) | `OPENAI_API_KEY` missing, wrong, or out of credit. Fix `.env` and restart the API. |
| Search says "narrow the filters" | Skill search matched too many people at once. Add a role, location or experience filter first. |
| Real call does not ring | Check `CALL_PROVIDER=plivo`, the three Plivo values, a current `PUBLIC_BASE_URL` (ngrok changes on restart), and that it is within calling hours. |
| Call connects but the caller is silent | ngrok is not running or the URL in `.env` is old. Webhooks cannot reach the app. |
| Caller voice is the basic one | ElevenLabs keys missing, or error 402 (free plans cannot use library voices through the API). Use your own voice or a paid plan. |
| Candidates cannot open the test link | `PUBLIC_BASE_URL` is empty or ngrok stopped. Phones are blocked by design from HR screens, but `/exam` works on any device with a browser. Full screen needs a laptop or desktop. |
| "Not sent" in the Email column | Hover it for the reason. `535` or "Authentication" means a wrong Gmail App Password. "Email is not set up" means `SMTP_HOST` is empty or the API was not restarted. |
| Coding answers say "needs marking" | No Docker and no OpenAI key. Start Docker Desktop and run `docker pull python:3.12-slim`, or set `OPENAI_API_KEY`. Or type marks by hand. |
| Editing a test says "candidates are taking this test right now" | Wait until they finish, then save again. |
| A candidate lost their password | Tests → Results → open the person → **Reset password**. |
| Garbled Hindi in the Windows console | Run with `PYTHONIOENCODING=utf-8`. |
| Browser shows an old screen after an update | Hard refresh (Ctrl+F5). |
| Database error after pulling new code | A table gained a column. Add it by hand, or move `kripa.db` aside (after backing it up) to start fresh. |
