import React, { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import QuestionSetsPage from "./QuestionSetsPage.jsx";
import { RECORD_LABEL, STATE_LABEL, answerText, fmtWhen } from "./util.js";

const QUICK = ["Yes", "No", "I am busy, call me later", "Tomorrow at 4 pm", "About 30 days", "40000", "Stop calling me"];

export function Answers({ answers }) {
  if (!answers?.length) return null;
  return (
    <dl className="answers">
      {answers.map((a, i) => <React.Fragment key={i}><dt>{a.question}</dt><dd>{answerText(a)}</dd></React.Fragment>)}
    </dl>
  );
}

function Simulator({ recordId, onClose, onChanged }) {
  const [rec, setRec] = useState(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const end = useRef();

  useEffect(() => { api.callRecord(recordId).then(setRec).catch((e) => setError(e.message)); }, [recordId]);
  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth" }); }, [rec?.transcript?.length, rec?.status]);

  async function act(fn) {
    setBusy(true);
    setError("");
    try {
      setRec(await fn());
      onChanged();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  const send = (t) => {
    const v = (t ?? text).trim();
    if (!v || busy) return;
    setText("");
    act(() => api.simSay(recordId, v));
  };

  return (
    <div className="drawer-wrap center-modal" onClick={onClose}>
      <div className="phone" onClick={(e) => e.stopPropagation()}>
        <div className="phone-head">
          <div>
            <b>{rec?.candidate.name || "…"}</b>
            <div className="small">{rec ? `Attempt ${rec.attempt} · ${RECORD_LABEL[rec.status] || rec.status}` : ""}</div>
          </div>
          <button className="x" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="sim-note">Simulator: you play the candidate. No real call is made.</div>

        <div className="chat">
          {rec?.status === "ringing" && <div className="ringing">📞 Ringing…</div>}
          {(rec?.transcript || []).map((m, i) => <div key={i} className={`bubble ${m.who}`}>{m.text}</div>)}
          {rec && !["ringing", "in_progress"].includes(rec.status) && (
            <div className="result">
              <b>{RECORD_LABEL[rec.status] || rec.status}</b>
              {rec.recall_at && <div>Call back: {fmtWhen(rec.recall_at)}</div>}
              <Answers answers={rec.answers} />
            </div>
          )}
          <div ref={end} />
        </div>

        {error && <div className="alert error">{error}</div>}

        {rec?.status === "ringing" && (
          <div className="phone-actions">
            <button className="primary" disabled={busy} onClick={() => act(() => api.simAnswer(recordId))}>Answer</button>
            <button disabled={busy} onClick={() => act(() => api.simOutcome(recordId, "no_answer"))}>No answer</button>
            <button disabled={busy} onClick={() => act(() => api.simOutcome(recordId, "busy"))}>Busy tone</button>
          </div>
        )}
        {rec?.status === "in_progress" && (
          <>
            <div className="quick">{QUICK.map((q) => <button key={q} disabled={busy} onClick={() => send(q)}>{q}</button>)}</div>
            <form className="phone-actions" onSubmit={(e) => { e.preventDefault(); send(); }}>
              <input autoFocus value={text} onChange={(e) => setText(e.target.value)} placeholder="Type what the candidate says" />
              <button className="primary" disabled={busy || !text.trim()}>Send</button>
              <button type="button" className="danger" disabled={busy} onClick={() => act(() => api.simHangup(recordId))}>Hang up</button>
            </form>
          </>
        )}
      </div>
    </div>
  );
}

function Activity({ goSets }) {
  const [data, setData] = useState({ jobs: [], counts: {}, provider: "mock" });
  const [open, setOpen] = useState(null);
  const [error, setError] = useState("");

  const load = useCallback(() => api.calls().then(setData).catch((e) => setError(e.message)), []);
  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  async function run(fn) {
    setError("");
    try { await fn(); await load(); } catch (e) { setError(e.message); }
  }
  const c = data.counts;
  const active = (c.calling || 0) + (c.queued || 0) + (c.retry_wait || 0) + (c.recall_wait || 0);

  return (
    <>
      {data.provider === "mock" && (
        <div className="alert info">
          <b>Simulator mode.</b> No real phone calls are made and nothing costs money. Open a ringing call and play the
          candidate to test the whole flow. Real calls need a phone provider such as Twilio.
        </div>
      )}
      {data.provider !== "mock" && data.settings && (
        <div className="alert live">
          <div>
            <b>Live mode.</b> Calls are real: they ring candidates' phones and use your calling credit. Calls are placed
            {data.settings.ignore_hours ? " at any hour" : ` between ${data.settings.hours_start}:00 and ${data.settings.hours_end}:00 IST`},
            at most {data.settings.max_concurrent} at a time{data.settings.max_per_day ? `, up to ${data.settings.max_per_day} per day` : ""}.
            An unanswered call is tried up to {data.settings.max_attempts} times, {data.settings.retry_minutes} minutes apart.
          </div>
          {active > 0 && (
            <button className="danger" onClick={() => window.confirm(`Stop all ${active} waiting or running call(s)? Nobody will be called again until you start new calls.`) && run(() => api.cancelAllCalls())}>
              Stop all calls
            </button>
          )}
        </div>
      )}
      {error && <div className="alert error">{error}</div>}

      <div className="summary">
        <div className="stat"><b>{c.calling || 0}</b><span>Calling now</span></div>
        <div className="stat"><b>{(c.queued || 0) + (c.retry_wait || 0)}</b><span>Waiting / retrying</span></div>
        <div className="stat"><b>{c.recall_wait || 0}</b><span>Callbacks booked</span></div>
        <div className="stat good"><b>{c.done || 0}</b><span>Done</span></div>
        <div className="stat warn"><b>{c.gave_up || 0}</b><span>Gave up</span></div>
      </div>

      <ul className="people calls">
        {data.jobs.map((j) => {
          const live = ["ringing", "in_progress"].includes(j.record_status) && j.state === "calling";
          return (
            <li key={j.id}>
              <div className="who job">
                <b>{j.name}</b>
                <span className="meta">
                  <span className={`state ${j.state}`}>{STATE_LABEL[j.state] || j.state}</span>
                  {j.role && <span className="tag role">{j.role}</span>}
                  {j.question_set && <span className="tag">{j.question_set}</span>}
                  <span className="tag">Unanswered {j.attempts} of {j.max_attempts}</span>
                </span>
                <span className="muted small">
                  {j.last_outcome}
                  {j.next_run_at && ["queued", "retry_wait", "recall_wait"].includes(j.state) && <> · next call {fmtWhen(j.next_run_at)}</>}
                  {j.answered > 0 && <> · {j.answered} answer(s) saved</>}
                </span>
              </div>
              <div className="row gap">
                {j.record_id && <button className={live ? "primary" : ""} onClick={() => setOpen(j.record_id)}>{live ? "Open call" : "View"}</button>}
                {["queued", "retry_wait", "recall_wait"].includes(j.state) && <button onClick={() => run(() => api.callNow(j.id))}>Call now</button>}
                {["done", "gave_up", "cancelled"].includes(j.state) && (
                  <button onClick={() => window.confirm(`Call ${j.name} again?${data.provider !== "mock" ? " This is a real phone call." : ""}`) &&
                    run(async () => {
                      const r = await api.startCalls([j.candidate_id], null, true);
                      if (r.skipped.length) throw new Error(`${r.skipped[0].name}: ${r.skipped[0].reason}`);
                    })}>Call again</button>
                )}
                {["queued", "calling", "retry_wait", "recall_wait"].includes(j.state) && <button className="danger" onClick={() => run(() => api.cancelCall(j.id))}>Cancel</button>}
              </div>
            </li>
          );
        })}
        {data.jobs.length === 0 && (
          <li className="muted empty-row">No calls yet. Tick candidates on the Search page and click “Start calls”. <button className="link" onClick={goSets}>Edit questions</button></li>
        )}
      </ul>

      {open && <Simulator recordId={open} onClose={() => setOpen(null)} onChanged={load} />}
    </>
  );
}

function Results() {
  const [only, setOnly] = useState("completed");
  const [merge, setMerge] = useState(false);
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => { setData(null); api.callResults(only).then(setData).catch((e) => setError(e.message)); }, [only]);

  return (
    <>
      {error && <div className="alert error">{error}</div>}
      <div className="toolbar">
        <div className="row gap">
          <select value={only} onChange={(e) => setOnly(e.target.value)} aria-label="Who to show">
            <option value="completed">People who answered all questions</option>
            <option value="all">Everyone who was called</option>
          </select>
          <span className="muted">{data ? `${data.count} ${data.count === 1 ? "person" : "people"}` : "…"}</span>
        </div>
        <div className="row gap">
          <label className="check"><input type="checkbox" checked={merge} onChange={(e) => setMerge(e.target.checked)} /> Merge with candidate record</label>
          <a className="btn" href={api.callExportUrl({ only, merge, format: "xlsx" })}>Download Excel</a>
          <a className="btn" href={api.callExportUrl({ only, merge, format: "csv" })}>Download CSV</a>
        </div>
      </div>
      <p className="muted small">
        {merge ? "The download has each candidate’s full record from the sheet, followed by their call answers." : "The download has only the call details: name, phone, email, call outcome and the answers."}
      </p>
      {data && data.count === 0 && <div className="card muted">No results yet. Answers appear here once a candidate finishes the questions.</div>}
      {data && data.count > 0 && (
        <div className="tablewrap">
          <table className="results">
            <thead><tr>{data.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
            <tbody>
              {data.rows.map((r, i) => <tr key={i}>{r.map((v, k) => <td key={k}>{v === null || v === undefined ? "" : String(v)}</td>)}</tr>)}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

export default function CallsPage() {
  const [sub, setSub] = useState("activity");
  return (
    <div className="page">
      <div className="page-head">
        <h1>Calls</h1>
        <p className="muted">Automatic screening calls: retries when nobody answers, and callbacks at the time a candidate asks for.</p>
        <div className="subtabs">
          <button className={sub === "activity" ? "active" : ""} onClick={() => setSub("activity")}>Activity</button>
          <button className={sub === "results" ? "active" : ""} onClick={() => setSub("results")}>Results</button>
          <button className={sub === "sets" ? "active" : ""} onClick={() => setSub("sets")}>Question sets</button>
        </div>
      </div>
      {sub === "activity" ? <Activity goSets={() => setSub("sets")} /> : sub === "results" ? <Results /> : <QuestionSetsPage />}
    </div>
  );
}
