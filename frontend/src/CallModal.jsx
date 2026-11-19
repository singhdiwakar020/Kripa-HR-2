import React, { useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { Answers } from "./Answers.jsx";
import { RECORD_LABEL, fmtWhen } from "./util.js";

const QUICK = ["Yes", "No", "I am busy, call me later", "Tomorrow at 4 pm", "About 30 days", "40000", "Stop calling me"];

export function CallModal({ recordId, onClose, onChanged }) {
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
