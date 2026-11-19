# Kripa HR: Stage 1 (upload sheet, database, smart search)

Backend: FastAPI + SQLite. Frontend: React (Vite).

## Run

```powershell
# terminal 1: API on http://127.0.0.1:8000
cd backend
.venv\Scripts\python -m uvicorn app.main:app --reload

# terminal 2: UI on http://localhost:5173
cd frontend
npm run dev
```

Plain-language search uses the OpenAI API. Put the key in `backend/.env` (see `.env.example`):

```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

Restart the API after changing it. The key is not committed.

Try it with `backend/sample_career_form.xlsx` (regenerate with `python make_sample.py`).
`python smoke_test.py` runs the upload flow against a throwaway database. Search in that script uses stand-in AI functions so it does not call OpenAI.
