import React, { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { CallHistory, StartCallsModal } from "./StartCallsModal.jsx";
import { SendTestModal } from "./SendTestModal.jsx";

const EMPTY = {};

function chips(f) {
  const c = [];
  if (f.name) c.push(["name", `Name: ${f.name}`]);
  if (f.text) c.push(["text", `Contains: ${f.text}`]);
  if (f.role) c.push(["role", f.role]);
  if (f.min_score != null) c.push(["min_score", `Score ${Number(f.min_score).toFixed(1).replace(/\.0$/, "")}+`]);
  if (f.max_score != null) c.push(["max_score", `Score up to ${f.max_score}`]);
  if (f.location) c.push(["location", f.location]);
  if (f.include_relocators) c.push(["include_relocators", "Include people ready to relocate"]);
  if (f.status) c.push(["status", f.status]);
  if (f.ready_to_relocate) c.push(["ready_to_relocate", "Ready to relocate"]);
  if (f.max_experience != null) c.push(["max_experience", `Up to ${f.max_experience} years`]);
  if (f.min_experience != null) c.push(["min_experience", `${f.min_experience}+ years`]);
  if (f.max_notice_days != null) c.push(["max_notice_days", `Notice within ${f.max_notice_days} days`]);
  if (f.max_expectation != null) c.push(["max_expectation", `Expected salary up to ${f.max_expectation}`]);
  if (f.keywords?.length) c.push(["keywords", f.keywords.map((k) => k.split("|")[0]).join(", ")]);
  return c;
}

const PALETTES = [
  ["#5b4df0", "#8b5cf6"], ["#0ea5e9", "#6366f1"], ["#10b981", "#0ea5e9"], ["#f59e0b", "#ef4444"],
  ["#ec4899", "#8b5cf6"], ["#14b8a6", "#22c55e"], ["#f97316", "#ec4899"],
];
function initials(name) {
  const p = (name || "?").trim().split(/\s+/);
  return ((p[0]?.[0] || "?") + (p.length > 1 ? p[p.length - 1][0] : "")).toUpperCase();
}
function Avatar({ name, id }) {
  const [a, b] = PALETTES[(id || 0) % PALETTES.length];
  return <span className="avatar" style={{ "--h1": a, "--h2": b }}>{initials(name)}</span>;
}
const EXAMPLES = [
  "AI interns in Bhopal with a score of 7 or more",
  "Freshers ready to relocate",
  "Deepak Singh",
  "Python developers, notice under 30 days",
];

function PhoneEditor({ c, onSaved }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function save() {
    setBusy(true);
    setError("");
    try {
      onSaved(await api.editCandidate(c.id, { phone: value }));
      setEditing(false);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <dt>Phone</dt>
      <dd>
        {!editing ? (
          <span className="row gap">
            <span>{c.phone || <span className="muted">none</span>}</span>
            <button className="link" onClick={() => { setValue(c.phone || ""); setError(""); setEditing(true); }}>Edit</button>
          </span>
        ) : (
          <form onSubmit={(e) => { e.preventDefault(); save(); }}>
            <div className="row gap">
              <input autoFocus value={value} onChange={(e) => setValue(e.target.value)} placeholder="9876543210" inputMode="tel" />
              <button className="primary" disabled={busy}>{busy ? "Saving…" : "Save"}</button>
              <button type="button" onClick={() => setEditing(false)}>Cancel</button>
            </div>
            {error && <div className="small" style={{ color: "var(--bad)", marginTop: 4 }}>{error}</div>}
          </form>
        )}
      </dd>
    </>
  );
}

function Field({ label, children }) {
  return <label className="field"><span>{label}</span>{children}</label>;
}

function useSpeech(onText) {
  const rec = useRef(null);
  const [listening, setListening] = useState(false);
  const Ctor = typeof window !== "undefined" && (window.SpeechRecognition || window.webkitSpeechRecognition);
  const toggle = () => {
    if (!Ctor) return;
    if (listening) { rec.current?.stop(); return; }
    const r = new Ctor();
    r.lang = "en-IN";
    r.onresult = (e) => onText(e.results[0][0].transcript);
    r.onend = () => setListening(false);
    r.onerror = () => setListening(false);
    rec.current = r;
    setListening(true);
    r.start();
  };
  return { supported: !!Ctor, listening, toggle };
}

export default function SearchPage({ goUpload, goCalls, goTests }) {
  const [calling, setCalling] = useState(false);
  const [facets, setFacets] = useState({ roles: [], locations: [], statuses: [], total: 0 });
  const [q, setQ] = useState("");
  const [filters, setFilters] = useState(EMPTY);
  const [sort, setSort] = useState("relevance");
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(new Set());
  const [testing, setTesting] = useState(false);
  const [open, setOpen] = useState(null);
  const [saved, setSaved] = useState([]);
  const [showFilters, setShowFilters] = useState(false);
  const runRef = useRef(0);

  const run = useCallback(async (query, f, s, offset = 0) => {
    const ticket = ++runRef.current;
    setLoading(true);
    setError("");
    try {
      const res = await api.search(query, f, s, offset);
      if (ticket !== runRef.current) return;
      setData((prev) => (offset > 0 && prev ? { ...res, results: [...prev.results, ...res.results] } : res));
      return res;
    } catch (e) {
      if (ticket === runRef.current) setError(e.message);
    } finally {
      if (ticket === runRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    api.facets().then(setFacets).catch((e) => setError(e.message));
    api.savedList().then(setSaved).catch(() => {});
    run("", EMPTY, "relevance");
  }, [run]);

  async function submitQuery(text = q) {
    setQ(text);
    setSelected(new Set());
    const res = await run(text, EMPTY, sort);
    if (res) setFilters(res.filters || EMPTY);
  }

  function changeFilters(next) {
    setFilters(next);
    setSelected(new Set());
    run("", next, sort);
  }

  function setFilter(k, v) {
    const next = { ...filters };
    if (v === "" || v === null || v === undefined || v === false || (Array.isArray(v) && !v.length)) delete next[k];
    else next[k] = v;
    if (k === "role") delete next.role_match; // a role picked by hand means exactly that role
    changeFilters(next);
  }

  const speech = useSpeech((t) => submitQuery(t));
  const results = data?.results || [];
  const active = chips(filters);

  async function saveSearch() {
    const name = window.prompt("Name this search so you can use it again");
    if (!name) return;
    await api.savedCreate(name, q, filters);
    setSaved(await api.savedList());
  }

  async function toggleDnc(c) {
    const updated = await api.setDnc(c.id, !c.do_not_contact);
    setOpen(updated);
    run("", filters, sort);
  }

  if (facets.total === 0 && !loading && !error) {
    return (
      <div className="page empty">
        <div className="big-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 16V4m0 0-4 4m4-4 4 4" /><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3" /></svg>
        </div>
        <h1>Upload a sheet to start</h1>
        <p className="muted">The Career Form file becomes the candidate list you can search.</p>
        <button className="primary" onClick={goUpload}>Upload sheet</button>
      </div>
    );
  }

  return (
    <div className="page">
      <div className="hero">
        <h1>Find the right candidate</h1>
        <p className="muted">Describe who you need in plain words. Kripa filters and ranks your database.</p>
      </div>
      <form className="searchbar" onSubmit={(e) => { e.preventDefault(); submitQuery(); }}>
        <span className="sicon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
        </span>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search a name, email or phone, or describe who you need: AI interns in Bhopal, score 7+"
        />
        {speech.supported && (
          <button type="button" className={speech.listening ? "mic on" : ""} onClick={speech.toggle}>
            {speech.listening ? "Listening…" : "Speak"}
          </button>
        )}
        <button className="primary" type="submit">{loading ? "Searching…" : "Search"}</button>
      </form>
      {!q && active.length === 0 && (
        <div className="examples">
          {EXAMPLES.map((ex) => <button key={ex} type="button" onClick={() => submitQuery(ex)}>{ex}</button>)}
        </div>
      )}

      {error && <div className="alert error">{error}</div>}
      {data?.notice && !error && <div className="alert info">{data.notice}</div>}

      {active.length > 0 && (
        <div className="chips">
          {active.map(([k, label]) => (
            <span className="chip" key={k}>
              {label}
              <button aria-label={`Remove ${label}`} onClick={() => setFilter(k, k === "keywords" ? [] : "")}>×</button>
            </span>
          ))}
          <button className="link" onClick={() => { setQ(""); changeFilters(EMPTY); }}>Clear</button>
        </div>
      )}

      {(data?.suggestions || []).map((s, i) => (
        <div className="suggest" key={i}>
          <span>{s.label}</span>
          <button onClick={() => {
            const next = { ...filters };
            Object.entries(s.patch).forEach(([k, v]) => (v === null ? delete next[k] : (next[k] = v)));
            changeFilters(next);
          }}>Yes</button>
        </div>
      ))}

      <div className="toolbar">
        <div className="count">
          <b>{loading ? "…" : data?.total ?? 0}</b> {data?.total === 1 ? "person" : "people"}
          {selected.size > 0 && <span className="pill">{selected.size} ticked</span>}
        </div>
        <div className="row gap">
          <button onClick={() => setShowFilters((v) => !v)}>{showFilters ? "Hide filters" : "Filters"}</button>
          <select value={sort} aria-label="Sort" onChange={(e) => { setSort(e.target.value); run("", filters, e.target.value); }}>
            <option value="relevance">Best match</option>
            <option value="score">Highest score</option>
            <option value="experience">Most experience</option>
            <option value="name">Name</option>
          </select>
          {saved.length > 0 && (
            <select aria-label="Saved searches" defaultValue="" onChange={(e) => {
              const s = saved.find((x) => String(x.id) === e.target.value);
              e.target.value = "";
              if (!s) return;
              setQ(s.query);
              setFilters(s.filters || EMPTY);
              run("", s.filters || EMPTY, sort);
            }}>
              <option value="">Saved searches</option>
              {saved.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          )}
          <button onClick={saveSearch}>Save</button>
          {selected.size > 0 && <button className="primary" onClick={() => setCalling(true)}>Start calls ({selected.size})</button>}
          {selected.size > 0 && <button onClick={() => setTesting(true)}>Send test ({selected.size})</button>}
          <a className="btn" href={api.exportUrl([...selected])}>{selected.size ? `Download ${selected.size}` : "Download"}</a>
        </div>
      </div>

      {showFilters && (
        <div className="filterbar">
          <Field label="Name">
            <input value={filters.name || ""} placeholder="Any name" onChange={(e) => setFilter("name", e.target.value)} />
          </Field>
          <Field label="Role">
            <select value={filters.role || ""} onChange={(e) => setFilter("role", e.target.value)}>
              <option value="">Any</option>
              {facets.roles.map((r) => <option key={r}>{r}</option>)}
            </select>
          </Field>
          <Field label="Minimum score">
            <input type="number" min="0" max="10" step="0.5" value={filters.min_score ?? ""}
              onChange={(e) => setFilter("min_score", e.target.value === "" ? "" : Number(e.target.value))} />
          </Field>
          <Field label="City">
            <input value={filters.location || ""} placeholder="Any city"
              onChange={(e) => setFilter("location", e.target.value)} />
          </Field>
          <Field label="Status">
            <select value={filters.status || ""} onChange={(e) => setFilter("status", e.target.value)}>
              <option value="">Any</option>
              {facets.statuses.filter((s) => s !== "Do not contact").map((s) => <option key={s}>{s}</option>)}
            </select>
          </Field>
          <Field label="Skills">
            <input value={(filters.keywords || []).map((k) => k.split("|")[0]).join(", ")} placeholder="Python, RAG"
              onChange={(e) => setFilter("keywords", e.target.value.split(",").map((s) => s.trim()).filter(Boolean))} />
          </Field>
        </div>
      )}

      <ul className="people">
        {results.map((r) => {
          const note = r.match.find((m) => m !== r.best_role && !String(m).startsWith("score ") && !String(m).endsWith(" yrs") && m !== "experience listed" && m !== r.experience_text && m !== r.location?.split(",")[0]);
          const needsLook = r.issues.some((i) => /phone|email|resume/i.test(i));
          return (
          <li key={r.id} className={selected.has(r.id) ? "on" : ""}>
            <input type="checkbox" checked={selected.has(r.id)} aria-label={`Select ${r.name}`}
              onChange={() => {
                const n = new Set(selected);
                n.has(r.id) ? n.delete(r.id) : n.add(r.id);
                setSelected(n);
              }} />
            <button className="person" onClick={() => api.candidate(r.id).then(setOpen)}>
              <Avatar name={r.name} id={r.id} />
              <span className="who">
                <b>{r.name || "No name"}</b>
                <span className="meta">
                  {r.best_role && <span className="tag role">{r.best_role}</span>}
                  {r.location && <span className="tag">{r.location.split(",")[0]}</span>}
                  {r.experience_text && <span className="tag">{r.experience_text}</span>}
                </span>
                {note && <span className="why">✓ {note}</span>}
                {needsLook && <span className="flag">Needs contact or resume</span>}
              </span>
              <span className={`score ${r.score >= 7 ? "hi" : r.score >= 5 ? "mid" : "lo"}`}>{r.score ?? "–"}</span>
            </button>
          </li>
          );
        })}
        {results.length === 0 && !loading && <li className="muted empty-row">No one matches. Remove a filter or try a wider search.</li>}
        {results.length === 0 && loading && <li className="muted empty-row">Searching…</li>}
      </ul>

      {data && results.length < data.total && (
        <div className="center more">
          <button onClick={() => run("", filters, sort, results.length)}>Show more</button>
        </div>
      )}

      {calling && <StartCallsModal ids={[...selected]} onClose={() => setCalling(false)} goCalls={goCalls} />}
      {testing && <SendTestModal ids={[...selected]} onClose={() => setTesting(false)} goTests={goTests} />}

      {open && (
        <div className="drawer-wrap" onClick={() => setOpen(null)}>
          <aside className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="row between">
              <div className="head">
                <Avatar name={open.name} id={open.id} />
                <h2>{open.name}</h2>
              </div>
              <button className="x" onClick={() => setOpen(null)} aria-label="Close">×</button>
            </div>
            <p className="muted">{[open.best_role, open.status].filter(Boolean).join(" · ")} {open.score != null && <>· score <b>{open.score}</b></>}</p>
            {open.issues.length > 0 && <div className="alert warn">{open.issues.join(" · ")}</div>}
            {!open.callable && <div className="alert error">No email or phone, so this person cannot be called yet.</div>}
            <CallHistory id={open.id} />
            <h4>Why they were scored</h4>
            <p>{open.reasoning || "No reason was recorded."}</p>
            <dl>
              {open.email && <><dt>Email</dt><dd>{open.email}</dd></>}
              <PhoneEditor c={open} onSaved={(u) => { setOpen(u); run("", filters, sort); }} />
              {open.location && <><dt>Location</dt><dd>{open.location}</dd></>}
              {open.company && <><dt>Company</dt><dd>{open.company}</dd></>}
              {open.experience_text && <><dt>Experience</dt><dd>{open.experience_text}</dd></>}
              {open.notice_period_text && <><dt>Notice period</dt><dd>{open.notice_period_text}</dd></>}
              {open.expectation != null && <><dt>Expected salary</dt><dd>{open.expectation}</dd></>}
              {open.ready_to_relocate != null && <><dt>Ready to relocate</dt><dd>{open.ready_to_relocate ? "Yes" : "No"}</dd></>}
              {open.hr_notes && <><dt>Notes</dt><dd>{open.hr_notes}</dd></>}
            </dl>
            <div className="row gap">
              {open.resume_link && <a className="btn" href={open.resume_link} target="_blank" rel="noreferrer">Open resume</a>}
              <button className={open.do_not_contact ? "" : "danger"} onClick={() => toggleDnc(open)}>
                {open.do_not_contact ? "Allow contact again" : "Do not contact"}
              </button>
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}
