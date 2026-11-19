# Kripa HR

A small hiring toolkit for HR teams. Upload the candidate spreadsheet, find people in plain language, screen them with automatic phone calls, and test them online.

| | What it does |
|---|---|
| **Upload** | Import a candidate sheet (Excel or CSV). Duplicates are merged by Candidate ID, email or phone. A preview shows what is new, updated or unchanged before anything is saved. |
| **Search** | Ask in your own words: *"Python developers in Bhopal with 2 years experience, ready to join in 30 days"*. AI turns it into filters and judges skill fit. |
| **Calls** | An AI caller rings candidates (Hindi, English, Hinglish), asks your screening questions, answers their questions about the job, and records the answers. Retries, call-backs, calling hours and daily limits are built in. |
| **Tests** | Build a hackathon-style test with multiple-choice and Python coding questions. Send candidates a link, login and password by email. They take it full screen with warnings for leaving the window. Scores, ranking and results are ready for HR. Coding answers are marked automatically. |

## Quick start

You need Python 3.11+ and Node 18+.

```powershell
# 1. Backend (terminal 1)
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env          # then open .env and add your keys
.venv\Scripts\python -m uvicorn app.main:app --reload     # http://127.0.0.1:8000

# 2. Frontend (terminal 2)
cd frontend
npm install
npm run dev                      # http://localhost:5173
```

Open http://localhost:5173, go to **Upload**, and try `backend/sample_career_form.xlsx`.

## Settings (`backend/.env`)

Only `OPENAI_API_KEY` is needed to start. Everything else switches on a feature.

| Setting | Needed for |
|---|---|
| `OPENAI_API_KEY` | Search, understanding what candidates say on calls, AI marking of coding answers |
| `CALL_PROVIDER=plivo` + `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN`, `PLIVO_PHONE_NUMBER` + `PUBLIC_BASE_URL` | Real phone calls (the default `mock` uses an on-screen simulator, no cost) |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` | A natural caller voice instead of the basic one |
| `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` | Emailing test links and passwords to candidates |

The full list, with defaults and what each does, is in [DOCUMENTATION.md](DOCUMENTATION.md#7-configuration-reference).

## Safe by default

- **Candidate data stays on your computer.** The server answers only on `localhost`. When you expose it with ngrok for calls or tests, only the phone webhooks and the candidate test page are reachable from outside. Search, uploads and results are not.
- **Real calls have guards:** calling hours (10:00 to 19:00 IST), at most 3 calls at once, at most 150 attempts a day, people who finished or were rejected are never called again, and **Stop all calls** is one click.
- **Secrets are never committed.** `.env`, the database and backups are in `.gitignore`.

## Running the checks

There is no test framework to install. Each file is a plain script, run from `backend/`:

```powershell
.venv\Scripts\python smoke_test.py   # upload, merge, search, export
.venv\Scripts\python calls_test.py   # call queue, retries, conversation
.venv\Scripts\python plivo_test.py   # phone provider, voice, webhooks
.venv\Scripts\python exam_test.py    # tests: login, timer, rules, marking
```

They use a throwaway database and need no API keys.

## Learn more

[DOCUMENTATION.md](DOCUMENTATION.md) covers how to use each screen, how the system works inside, the API, and troubleshooting.
