import React, { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";

const fmt = (iso) => (iso ? new Date(iso).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "");
const num = (v) => (v === null || v === undefined ? "" : Number.isInteger(v) ? v : Math.round(v * 100) / 100);
const KIND_LABEL = { tab_hidden: "Switched tab", window_blur: "Left the window", fullscreen_exit: "Left full screen" };

const blankMcq = () => ({ kind: "mcq", text: "", marks: 1, options: ["", "", "", ""], correct: [] });
const blankCoding = () => ({ kind: "coding", text: "", marks: 10, starter_code: "", test_cases: [{ input: "", expected: "", hidden: false }, { input: "", expected: "", hidden: true }] });
const BLANK = { title: "", instructions: "", duration_minutes: 60, negative_marking: 0, max_violations: 2, show_score: false, questions: [blankMcq()] };

/* ------------------------------------------------------------------ builder */
function McqEditor({ q, set }) {
  const setOpt = (i, v) => set({ options: q.options.map((o, k) => (k === i ? v : o)) });
  const toggle = (i) => set({ correct: q.correct.includes(i) ? q.correct.filter((x) => x !== i) : [...q.correct, i].sort() });
  const remove = (i) => set({ options: q.options.filter((_, k) => k !== i), correct: q.correct.filter((x) => x !== i).map((x) => (x > i ? x - 1 : x)) });
  return (
    <div>
      <div className="muted small" style={{ marginBottom: 6 }}>Tick the right choice. Tick more than one if several are right (candidates then see “choose all that apply”).</div>
      {q.options.map((o, i) => (
        <div className="optrow" key={i}>
          <input type="checkbox" checked={q.correct.includes(i)} onChange={() => toggle(i)} aria-label={`Choice ${i + 1} is correct`} />
          <input value={o} onChange={(e) => setOpt(i, e.target.value)} placeholder={`Choice ${i + 1}`} />
          <button type="button" className="mini" disabled={q.options.length <= 2} onClick={() => remove(i)} aria-label="Remove choice">×</button>
        </div>
      ))}
      {q.options.length < 8 && <button type="button" className="mini-add" onClick={() => set({ options: [...q.options, ""] })}>+ Add choice</button>}
    </div>
  );
}

function CodingEditor({ q, set }) {
  const setCase = (i, patch) => set({ test_cases: q.test_cases.map((c, k) => (k === i ? { ...c, ...patch } : c)) });
  return (
    <div>
      <label className="field"><span>Starter code (optional)</span>
        <textarea className="mono" rows={3} value={q.starter_code} onChange={(e) => set({ starter_code: e.target.value })} placeholder={"# Read input with input() and print the answer"} />
      </label>
      <div className="muted small" style={{ margin: "10px 0 6px" }}>
        Test cases. The code is run once per case with “Input” typed in as its input, and its printed output must match “Expected output”. Sample cases are shown to the candidate; hidden ones are used only for marking. Marks are split equally across the cases.
      </div>
      {q.test_cases.map((c, i) => (
        <div className="casebox" key={i}>
          <label className="field"><span>Input</span><textarea className="mono" rows={2} value={c.input} onChange={(e) => setCase(i, { input: e.target.value })} /></label>
          <label className="field"><span>Expected output</span><textarea className="mono" rows={2} value={c.expected} onChange={(e) => setCase(i, { expected: e.target.value })} /></label>
          <div className="casectl">
            <label className="check"><input type="checkbox" checked={c.hidden} onChange={(e) => setCase(i, { hidden: e.target.checked })} /> Hidden</label>
            <button type="button" className="mini" disabled={q.test_cases.length <= 1} onClick={() => set({ test_cases: q.test_cases.filter((_, k) => k !== i) })} aria-label="Remove test case">×</button>
          </div>
        </div>
      ))}
      <button type="button" className="mini-add" onClick={() => set({ test_cases: [...q.test_cases, { input: "", expected: "", hidden: true }] })}>+ Add test case</button>
    </div>
  );
}

const VERDICT = { correct: "correct", mostly_correct: "mostly correct", partly_correct: "partly correct", wrong: "wrong", empty: "empty" };

function Editor({ start, onDone, onCancel }) {
  const [d, setD] = useState(start.draft);
  const started = !!start.started; // candidates have already taken this test: saving recalculates their scores
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const setQ = (i, patch) => setD({ ...d, questions: d.questions.map((q, k) => (k === i ? { ...q, ...patch } : q)) });
  const move = (i, by) => {
    const j = i + by;
    if (j < 0 || j >= d.questions.length) return;
    const qs = [...d.questions];
    [qs[i], qs[j]] = [qs[j], qs[i]];
    setD({ ...d, questions: qs });
  };
  const total = d.questions.reduce((s, q) => s + (Number(q.marks) || 0), 0);

  async function save() {
    setBusy(true);
    setError("");
    try {
      const saved = await api.saveExam(start.id, {
        ...d, duration_minutes: Number(d.duration_minutes), negative_marking: Number(d.negative_marking), max_violations: Number(d.max_violations),
        questions: d.questions.map((q) => ({ ...q, marks: Number(q.marks) })),
      });
      onDone(saved);
    } catch (e) { setError(e.message); window.scrollTo({ top: 0, behavior: "smooth" }); } finally { setBusy(false); }
  }

  return (
    <div className="page">
      <div className="page-head row between">
        <div><h1>{start.id ? "Edit test" : "New test"}</h1><p className="muted">{d.questions.length} questions · {total} marks in total</p></div>
        <div className="row gap"><button onClick={onCancel}>Cancel</button><button className="primary" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save test"}</button></div>
      </div>
      {error && <div className="alert error">{error}</div>}
      {started && <div className="alert warn"><b>{start.started} {start.started === 1 ? "candidate has" : "candidates have"} already taken this test.</b> When you save, the scores of everyone who finished are recalculated with your changes (marks you gave by hand are kept). If you change the order or number of choices in a multiple-choice question, check its correct answer. You cannot save while someone is taking the test right now.</div>}
      <div className="card">
        <div className="grid2">
          <label className="field"><span>Title</span><input value={d.title} onChange={(e) => setD({ ...d, title: e.target.value })} placeholder="For example: AI Developer Intern hackathon" /></label>
          <label className="field"><span>Time limit (minutes)</span><input type="number" min="5" max="480" value={d.duration_minutes} onChange={(e) => setD({ ...d, duration_minutes: e.target.value })} /></label>
          <label className="field"><span>Wrong MCQ answer costs</span>
            <select value={d.negative_marking} onChange={(e) => setD({ ...d, negative_marking: e.target.value })}>
              <option value={0}>Nothing (no negative marking)</option><option value={0.25}>A quarter of its marks</option><option value={0.33}>A third of its marks</option><option value={0.5}>Half of its marks</option><option value={1}>All of its marks</option>
            </select>
          </label>
          <label className="field"><span>Warnings before the test ends</span>
            <select value={d.max_violations} onChange={(e) => setD({ ...d, max_violations: e.target.value })}>
              {[0, 1, 2, 3, 4].map((n) => <option key={n} value={n}>{n} {n === 1 ? "warning" : "warnings"}{n === 2 ? " (recommended)" : ""}</option>)}
            </select>
          </label>
        </div>
        <label className="field" style={{ marginTop: 12 }}><span>Instructions shown to candidates</span>
          <textarea rows={4} value={d.instructions} onChange={(e) => setD({ ...d, instructions: e.target.value })} placeholder="What the test covers, how it is marked, anything candidates should know." />
        </label>
        <label className="check" style={{ marginTop: 12 }}><input type="checkbox" checked={d.show_score} onChange={(e) => setD({ ...d, show_score: e.target.checked })} /> Show candidates their score after they submit</label>
        <p className="muted small">Leaving the window or full screen more than the allowed warnings ends the test and submits the answers so far.</p>
      </div>

      {d.questions.map((q, i) => (
        <div className="card qbox" key={i}>
          <div className="row between">
            <b>Question {i + 1} · {q.kind === "mcq" ? "Multiple choice" : "Coding (Python)"}</b>
            <div className="row gap">
              <button type="button" className="mini" disabled={i === 0} onClick={() => move(i, -1)} aria-label="Move up">↑</button>
              <button type="button" className="mini" disabled={i === d.questions.length - 1} onClick={() => move(i, 1)} aria-label="Move down">↓</button>
              <button type="button" className="mini" onClick={() => setD({ ...d, questions: d.questions.filter((_, k) => k !== i) })} aria-label="Remove question">Delete</button>
            </div>
          </div>
          <div className="grid2" style={{ marginTop: 10 }}>
            <label className="field wide2"><span>Question</span><textarea rows={q.kind === "coding" ? 4 : 2} value={q.text} onChange={(e) => setQ(i, { text: e.target.value })} /></label>
            <label className="field"><span>Marks</span><input type="number" min="0.5" step="0.5" value={q.marks} onChange={(e) => setQ(i, { marks: e.target.value })} /></label>
          </div>
          {q.kind === "mcq" ? <McqEditor q={q} set={(p) => setQ(i, p)} /> : <CodingEditor q={q} set={(p) => setQ(i, p)} />}
        </div>
      ))}
      <div className="row gap" style={{ marginTop: 16 }}>
        <button onClick={() => setD({ ...d, questions: [...d.questions, blankMcq()] })}>+ Multiple choice question</button>
        <button onClick={() => setD({ ...d, questions: [...d.questions, blankCoding()] })}>+ Coding question</button>
      </div>
      <div className="row end gap"><button onClick={onCancel}>Cancel</button><button className="primary" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save test"}</button></div>
    </div>
  );
}

/* ------------------------------------------------------------------ one candidate's attempt */
function Attempt({ inviteId, onClose, onChanged }) {
  const [a, setA] = useState(null);
  const [error, setError] = useState("");
  const [marks, setMarks] = useState({});
  const [pw, setPw] = useState(null);
  const load = useCallback(() => api.examAttempt(inviteId).then((x) => { setA(x); setMarks({}); }).catch((e) => setError(e.message)), [inviteId]);
  useEffect(() => { load(); }, [load]);

  async function mark(body) {
    setError("");
    try { setA(await api.markExamAttempt(inviteId, body)); setMarks({}); onChanged(); } catch (e) { setError(e.message); }
  }
  async function reset() {
    if (!window.confirm("Make a new password for this candidate? The old one stops working.")) return;
    try { setPw(await api.resetExamPassword(inviteId)); } catch (e) { setError(e.message); }
  }

  return (
    <div className="drawer-wrap" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="row between"><h2>{a ? a.candidate.name : "Attempt"}</h2><button className="x" onClick={onClose} aria-label="Close">×</button></div>
        {error && <div className="alert error">{error}</div>}
        {a && (
          <>
            <p className="muted">{a.email}{a.candidate.ref ? ` · ${a.candidate.ref}` : ""} · {a.exam.title}</p>
            <div className="facts">
              <span className="tag role">{a.status_label}</span>
              {a.voided && <span className="tag bad">Voided</span>}
              <span className="tag">MCQ {num(a.mcq_score)}</span>
              <span className="tag">Coding {a.coding_score === null ? (a.grading === "running" ? "being marked…" : "needs marking") : num(a.coding_score)}</span>
              <span className="tag role">Total {num(a.total_score)} / {num(a.max_score)}</span>
            </div>
            <p className="muted small">Started {fmt(a.started_at) || "—"} · Finished {fmt(a.submitted_at) || "—"}</p>

            {pw && <div className="alert warn">New password for {pw.email}: <code>{pw.password}</code> (shown once). {pw.email_sent ? "It was also emailed to the candidate." : pw.email_enabled ? `Email not sent: ${pw.email_error}` : "Email is not set up, so give it to them yourself."}</div>}
            {a.status !== "submitted" && a.status !== "timed_out" && a.status !== "terminated" && <button onClick={reset}>Reset password</button>}

            <h3>Policy violations ({a.violations.length})</h3>
            {a.violations.length === 0 ? <p className="muted small">None.</p> : (
              <ul className="vlist">{a.violations.map((v, i) => <li key={i}>{fmt(v.at)} · {KIND_LABEL[v.kind] || v.kind}</li>)}</ul>
            )}
            {a.status === "terminated" && <div className="alert warn">This test was ended automatically for going over the allowed warnings. If you think it was a mistake (for example a system notification), you can void it below.</div>}
            <div className="row gap" style={{ margin: "10px 0" }}>
              <button onClick={() => mark({ voided: !a.voided })}>{a.voided ? "Restore this attempt" : "Void this attempt"}</button>
            </div>

            <h3>Answers</h3>
            {a.questions.map((q, i) => (
              <div className="ans" key={q.id}>
                <div className="row between"><b>{i + 1}. {q.text}</b><span className="muted small">{q.awarded === null || q.awarded === undefined ? "—" : num(q.awarded)} / {num(q.marks)}</span></div>
                {q.kind === "mcq" ? (
                  <ul className="mcq">
                    {q.options.map((o, k) => {
                      const picked = q.choice.includes(k), right = q.correct.includes(k);
                      return <li key={k} className={(right ? "right " : "") + (picked && !right ? "wrong" : "")}>{picked ? "● " : "○ "}{o}{right ? "  ✓" : ""}</li>;
                    })}
                    {q.choice.length === 0 && <li className="muted">Not answered</li>}
                  </ul>
                ) : (
                  <>
                    <pre className="code">{q.code || "(no code written)"}</pre>
                    {q.detail && q.detail.by !== "ai" && <p className="muted small">Passed {q.detail.passed} of {q.detail.total} test cases{q.detail.cases?.some((c) => c.timeout) ? " (some ran out of time)" : ""}.</p>}
                    {q.detail?.by === "ai" && <p className="muted small"><b>Marked by AI</b> by reading the code ({num(q.detail.ai.percent)}%, {VERDICT[q.detail.ai.verdict] || q.detail.ai.verdict}). It is an estimate, so please check it and type your own marks below if you disagree.</p>}
                    {q.detail?.ai?.feedback && <div className="aibox"><b>AI review</b><br />{q.detail.ai.feedback}</div>}
                    {!q.detail && <p className="muted small">{a.grading === "running" ? "Being marked automatically…" : "Automatic marking is not available (no Docker and no OpenAI key), so mark this one by hand."}</p>}
                    <div className="row gap">
                      <label className="field" style={{ width: 150 }}><span>Marks (0 to {num(q.marks)})</span>
                        <input type="number" min="0" max={q.marks} step="0.5" value={marks[q.id] ?? (q.manual ?? "")} placeholder={q.detail ? String(num(q.detail.marks)) : ""} onChange={(e) => setMarks({ ...marks, [q.id]: e.target.value })} />
                      </label>
                      <button disabled={marks[q.id] === undefined} onClick={() => mark({ manual_coding: { [q.id]: marks[q.id] } })} style={{ alignSelf: "flex-end" }}>Save marks</button>
                    </div>
                  </>
                )}
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ results */
function Results({ exam, onBack }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(null);
  const load = useCallback(() => api.examResults(exam.id).then(setData).catch((e) => setError(e.message)), [exam.id]);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);

  return (
    <div className="page">
      <div className="page-head row between">
        <div><h1>{exam.title}</h1><p className="muted">Results update by themselves every few seconds.</p></div>
        <div className="row gap">
          <button onClick={onBack}>← All tests</button>
          <a className="btn" href={api.examExportUrl(exam.id, "xlsx")}>Download Excel</a>
          <a className="btn" href={api.examExportUrl(exam.id, "csv")}>Download CSV</a>
        </div>
      </div>
      {error && <div className="alert error">{error}</div>}
      {data && (
        <>
          <div className="stats">
            <div className="stat"><b>{data.exam.invited}</b><span>invited</span></div>
            <div className="stat"><b>{data.exam.started}</b><span>started</span></div>
            <div className="stat"><b>{data.exam.finished}</b><span>finished</span></div>
            <div className="stat"><b>{data.exam.average ?? "—"}</b><span>average of {data.exam.total_marks}</span></div>
          </div>
          {data.rows.length === 0 ? <div className="card muted">Nobody is invited yet. On the Search page, tick candidates and press “Send test”.</div> : (
            <div className="tablewrap">
              <table className="results">
                <thead><tr><th>Rank</th><th>Candidate ID</th><th>Name</th><th>Email</th><th>Status</th><th>MCQ</th><th>Coding</th><th>Total</th><th>%</th><th>Violations</th></tr></thead>
                <tbody>
                  {data.rows.map((r) => (
                    <tr key={r.invite_id} className="click" onClick={() => setOpen(r.invite_id)}>
                      <td>{r.voided ? "" : r.rank ?? ""}</td><td>{r.candidate_ref}</td><td>{r.name}</td><td>{r.email}</td>
                      <td>{r.status_label}{r.voided ? " · voided" : ""}</td>
                      <td>{num(r.mcq_score)}</td>
                      <td>{r.coding_score === null ? (r.status === "invited" || r.status === "in_progress" ? "" : r.grading === "running" ? "marking…" : "needs marking") : num(r.coding_score)}</td>
                      <td><b>{r.status === "invited" ? "" : num(r.total_score)}</b></td>
                      <td>{r.percent ?? ""}</td>
                      <td className={r.violations ? "badtext" : ""}>{r.violations || ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
      {open && <Attempt inviteId={open} onClose={() => setOpen(null)} onChanged={load} />}
    </div>
  );
}

/* ------------------------------------------------------------------ list */
export default function TestsPage() {
  const [exams, setExams] = useState(null);
  const [view, setView] = useState({ name: "list" });
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const load = useCallback(() => api.exams().then(setExams).catch((e) => setError(e.message)), []);
  useEffect(() => { load(); }, [load]);

  async function edit(e, copy = false) {
    try {
      const full = await api.exam(e.id);
      setView({ name: "edit", id: copy ? null : e.id, started: copy ? 0 : e.started, draft: { title: copy ? `${full.title} (copy)` : full.title, instructions: full.instructions, duration_minutes: full.duration_minutes,
        negative_marking: full.negative_marking, max_violations: full.max_violations, show_score: full.show_score,
        questions: full.questions.map((q) => ({ id: copy ? undefined : q.id, kind: q.kind, text: q.text, marks: q.marks, options: q.options.length ? q.options : ["", ""], correct: q.correct, starter_code: q.starter_code, test_cases: q.test_cases.length ? q.test_cases : [{ input: "", expected: "", hidden: false }] })) } });
    } catch (x) { setError(x.message); }
  }
  async function remove(e) {
    const note = e.started > 0
      ? `${e.started} candidate${e.started === 1 ? " has" : "s have"} already started it (${e.finished} finished). Their answers, scores and logins will be deleted for good and cannot be brought back. Download the results first if you need them.`
      : "Its invites and logins are removed too.";
    if (!window.confirm(`Delete “${e.title}”?

${note}`)) return;
    try { await api.deleteExam(e.id); load(); } catch (x) { setError(x.message); }
  }

  if (view.name === "edit") return <Editor start={view} onCancel={() => setView({ name: "list" })} onDone={(saved) => { setView({ name: "list" }); setNotice(saved?.recalculated ? `Saved. The scores of ${saved.recalculated} finished ${saved.recalculated === 1 ? "candidate were" : "candidates were"} recalculated.` : "Saved."); load(); }} />;
  if (view.name === "results") return <Results exam={view.exam} onBack={() => { setView({ name: "list" }); load(); }} />;

  return (
    <div className="page">
      <div className="page-head row between">
        <div><h1>Tests</h1><p className="muted">Build a test with multiple choice and coding questions, invite candidates from the Search page, and see ranked results here.</p></div>
        <button className="primary" onClick={() => setView({ name: "edit", id: null, draft: BLANK })}>+ New test</button>
      </div>
      {error && <div className="alert error">{error}</div>}
      {notice && <div className="alert info" onClick={() => setNotice("")}>{notice}</div>}
      {exams && exams.some((e) => e.coding_count > 0) && !exams[0].code_runner && (exams[0].ai_grading
        ? <div className="alert info"><b>Coding answers are marked by AI.</b> Docker is off, so the AI reads each answer, traces it against the test cases and proposes the marks. You can always type your own marks. For exact marking by running the code, start Docker Desktop and run <code>docker pull python:3.12-slim</code> once.</div>
        : <div className="alert warn"><b>Automatic coding marks are off.</b> Add <code>OPENAI_API_KEY</code> to backend/.env for AI marking, or start Docker Desktop and run <code>docker pull python:3.12-slim</code> once. Until then you mark coding answers by hand.</div>)}
      {exams && exams.length === 0 && <div className="card muted">No tests yet. Press “New test” to build one.</div>}
      {exams && exams.length > 0 && (
        <div className="tablewrap">
          <table className="results">
            <thead><tr><th>Test</th><th>Questions</th><th>Time</th><th>Invited</th><th>Finished</th><th>Average</th><th></th></tr></thead>
            <tbody>
              {exams.map((e) => (
                <tr key={e.id}>
                  <td><b>{e.title}</b></td>
                  <td>{e.mcq_count} MCQ · {e.coding_count} coding · {e.total_marks} marks</td>
                  <td>{e.duration_minutes} min</td><td>{e.invited}</td><td>{e.finished}</td><td>{e.average ?? "—"}</td>
                  <td className="actions">
                    <button className="primary" onClick={() => setView({ name: "results", exam: e })}>Results</button>
                    <button onClick={() => edit(e)}>Edit</button>{e.locked && <button onClick={() => edit(e, true)}>Copy</button>}
                    <button onClick={() => remove(e)}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
