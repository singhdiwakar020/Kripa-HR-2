import React, { useEffect, useState } from "react";
import { api } from "./api.js";

const KINDS = [["text", "Free answer"], ["yesno", "Yes / No"], ["number", "Number"]];
const BLANK = { name: "", greeting: "Hello {name}, this is the hiring team calling about your {role} application.",
  closing: "Thank you for your time. Our team will be in touch. Goodbye.", language: "en-IN", about: "", is_default: false,
  questions: [{ text: "", kind: "text" }] };

export default function QuestionSetsPage() {
  const [sets, setSets] = useState([]);
  const [draft, setDraft] = useState(null);
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");

  const load = () => api.questionSets().then((s) => { setSets(s); return s; }).catch((e) => setError(e.message));
  useEffect(() => { load().then((s) => s?.length && setDraft(structuredClone(s.find((x) => x.is_default) || s[0]))); }, []);

  const setQ = (i, patch) => setDraft({ ...draft, questions: draft.questions.map((q, k) => (k === i ? { ...q, ...patch } : q)) });
  const move = (i, d) => {
    const qs = [...draft.questions];
    const j = i + d;
    if (j < 0 || j >= qs.length) return;
    [qs[i], qs[j]] = [qs[j], qs[i]];
    setDraft({ ...draft, questions: qs });
  };

  async function save() {
    setError(""); setMsg("");
    try {
      const saved = await api.saveQuestionSet(draft);
      await load();
      setDraft(saved);
      setMsg("Saved.");
    } catch (e) { setError(e.message); }
  }
  async function remove() {
    if (!window.confirm(`Delete “${draft.name}”?`)) return;
    setError(""); setMsg("");
    try {
      await api.deleteQuestionSet(draft.id);
      const s = await load();
      setDraft(s?.length ? structuredClone(s[0]) : null);
    } catch (e) { setError(e.message); }
  }

  return (
    <div className="qs">
      <aside className="qs-list">
        {sets.map((s) => (
          <button key={s.id} className={draft?.id === s.id ? "active" : ""} onClick={() => { setDraft(structuredClone(s)); setMsg(""); setError(""); }}>
            {s.name}{s.is_default && <span className="pill">default</span>}
            <span className="small muted">{s.questions.length} questions</span>
          </button>
        ))}
        <button className="primary" onClick={() => { setDraft(structuredClone(BLANK)); setMsg(""); setError(""); }}>+ New question set</button>
      </aside>

      {draft && (
        <section className="card qs-edit">
          {error && <div className="alert error">{error}</div>}
          {msg && <div className="alert ok">{msg}</div>}
          <label className="field"><span>Name</span>
            <input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="For example: AI intern screening" />
          </label>
          <label className="field"><span>What the caller says first</span>
            <textarea rows={2} value={draft.greeting} onChange={(e) => setDraft({ ...draft, greeting: e.target.value })} />
            <small>Use {"{name}"} for the candidate’s first name and {"{role}"} for their role. Then it asks if it is a good time.</small>
          </label>

          <label className="field"><span>Company and job details (the caller answers candidates’ questions from this)</span>
            <textarea rows={9} value={draft.about || ""} onChange={(e) => setDraft({ ...draft, about: e.target.value })}
              placeholder={"About the company: what it does, size, since when.\nJob description: role, daily work, skills needed, internship or full time.\nLocation: office address, work from office or home, timings.\nPay and benefits: salary or stipend, only if you want it shared.\nHiring process: what happens next."} />
            <small>If a candidate asks about the company, the job description, the location, the pay or anything else, the caller answers using only what you write here, in short spoken sentences. For anything not written here it says the hiring team will share it, and it never makes up pay, benefits or promises.</small>
          </label>

          <h4>Questions, asked in this order</h4>
          {draft.questions.map((q, i) => (
            <div className="qrow" key={i}>
              <span className="qnum">{i + 1}</span>
              <input value={q.text} onChange={(e) => setQ(i, { text: e.target.value })} placeholder="What the caller will ask" />
              <select value={q.kind} onChange={(e) => setQ(i, { kind: e.target.value })} aria-label="Answer type">
                {KINDS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
              <button type="button" aria-label="Move up" onClick={() => move(i, -1)}>↑</button>
              <button type="button" aria-label="Move down" onClick={() => move(i, 1)}>↓</button>
              <button type="button" className="danger" aria-label="Remove" onClick={() => setDraft({ ...draft, questions: draft.questions.filter((_, k) => k !== i) })}>×</button>
            </div>
          ))}
          <button type="button" onClick={() => setDraft({ ...draft, questions: [...draft.questions, { text: "", kind: "text" }] })}>+ Add question</button>

          <label className="field" style={{ marginTop: 18 }}><span>What the caller says at the end</span>
            <textarea rows={2} value={draft.closing} onChange={(e) => setDraft({ ...draft, closing: e.target.value })} />
          </label>
          <label className="check"><input type="checkbox" checked={!!draft.is_default} onChange={(e) => setDraft({ ...draft, is_default: e.target.checked })} /> Use this set by default</label>

          <div className="row between end">
            {draft.id ? <button className="danger" onClick={remove}>Delete</button> : <span />}
            <button className="primary" onClick={save}>Save question set</button>
          </div>
        </section>
      )}
    </div>
  );
}
