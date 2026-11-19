import React, { useEffect, useState } from "react";
import { api } from "./api.js";
import { Answers } from "./CallsPage.jsx";
import { RECORD_LABEL, fmtWhen } from "./util.js";

export function StartCallsModal({ ids, onClose, goCalls }) {
  const [sets, setSets] = useState([]);
  const [setId, setSetId] = useState("");
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [live, setLive] = useState(null);   // settings of the phone line, when it is a real one
  const [agree, setAgree] = useState(false);
  const [again, setAgain] = useState(false); // also call people who already finished the questions
  const [typed, setTyped] = useState("");
  const needTyped = ids.length > 10;
  const confirmed = !live || (agree && (!needTyped || typed.trim() === String(ids.length)));

  useEffect(() => {
    api.calls().then((d) => setLive(d.provider !== "mock" ? d.settings : null)).catch(() => {});
    api.questionSets().then((s) => {
      setSets(s);
      setSetId(String((s.find((x) => x.is_default) || s[0])?.id || ""));
    }).catch((e) => setError(e.message));
  }, []);

  const chosen = sets.find((s) => String(s.id) === setId);

  async function start() {
    setBusy(true);
    setError("");
    try { setResult(await api.startCalls(ids, Number(setId), again)); } catch (e) { setError(e.message); } finally { setBusy(false); }
  }

  return (
    <div className="drawer-wrap center-modal" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="row between">
          <h2>Start calls</h2>
          <button className="x" onClick={onClose} aria-label="Close">×</button>
        </div>
        {error && <div className="alert error">{error}</div>}

        {!result ? (
          <>
            <p>Call <b>{ids.length}</b> {ids.length === 1 ? "person" : "people"} automatically. If someone does not answer, the system tries again; if they are busy, it asks when to call back and rings them then.</p>
            <label className="field"><span>Questions to ask</span>
              <select value={setId} onChange={(e) => setSetId(e.target.value)}>
                {sets.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
            </label>
            <label className="check" style={{ margin: "10px 0" }}>
              <input type="checkbox" checked={again} onChange={(e) => setAgain(e.target.checked)} />
              Call again people who already answered the questions
            </label>
            {chosen && (
              <ol className="preview-qs">{chosen.questions.map((q) => <li key={q.id}>{q.text}</li>)}</ol>
            )}
            {live && (
              <div className="alert live confirm">
                <div>
                  <b>These are real phone calls.</b> {ids.length} {ids.length === 1 ? "person" : "people"} will get an automated call
                  {live.ignore_hours ? "" : `, between ${live.hours_start}:00 and ${live.hours_end}:00 IST`}. Each unanswered call is tried up to
                  {" "}{live.max_attempts} times. This uses your calling credit and cannot be undone once people are called. You can use
                  “Stop all calls” on the Calls page.
                  <label className="check"><input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} />
                    I want to call {ids.length === 1 ? "this person" : `these ${ids.length} people`} now</label>
                  {needTyped && (
                    <label className="field">Type {ids.length} to confirm
                      <input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder={String(ids.length)} inputMode="numeric" />
                    </label>
                  )}
                </div>
              </div>
            )}
            <div className="row end gap">
              <button onClick={onClose}>Cancel</button>
              <button className="primary" disabled={busy || !setId || !confirmed} onClick={start}>{busy ? "Starting…" : `Call ${ids.length}`}</button>
            </div>
          </>
        ) : (
          <>
            <div className="alert ok"><b>{result.queued.length}</b> {result.queued.length === 1 ? "call is" : "calls are"} queued.
              {result.starts_at && <> They start at {fmtWhen(result.starts_at)}, when calling hours open.</>}</div>
            {result.skipped.length > 0 && (
              <div className="alert warn">
                <b>{result.skipped.length} left out:</b>
                <ul className="skips">{result.skipped.map((s) => <li key={s.id}>{s.name}: {s.reason}</li>)}</ul>
              </div>
            )}
            <div className="row end gap">
              <button onClick={onClose}>Close</button>
              <button className="primary" onClick={() => { onClose(); goCalls(); }}>View calls</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export function CallHistory({ id }) {
  const [rows, setRows] = useState(null);
  useEffect(() => { setRows(null); api.candidateCalls(id).then(setRows).catch(() => setRows([])); }, [id]);
  if (!rows?.length) return null;
  return (
    <>
      <h4>Calls</h4>
      {rows.map((r) => (
        <div className="callrow" key={r.id}>
          <div><b>{RECORD_LABEL[r.status] || r.status}</b> <span className="muted small">· attempt {r.attempt} · {fmtWhen(r.started_at)}</span></div>
          {r.recall_at && <div className="small">Call back: {fmtWhen(r.recall_at)}</div>}
          <Answers answers={r.answers} />
        </div>
      ))}
    </>
  );
}
