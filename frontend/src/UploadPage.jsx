import React, { useEffect, useRef, useState } from "react";
import { api } from "./api.js";

export default function UploadPage({ onImported }) {
  const [preview, setPreview] = useState(null);
  const [mapping, setMapping] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [drag, setDrag] = useState(false);
  const [history, setHistory] = useState([]);
  const [mappingDirty, setMappingDirty] = useState(false);
  const [askClear, setAskClear] = useState(false);
  const [typed, setTyped] = useState("");
  const [cleared, setCleared] = useState(null);
  const input = useRef();

  const loadHistory = () => api.imports().then(setHistory).catch(() => {});
  useEffect(() => { loadHistory(); }, []);

  async function handleFile(file) {
    if (!file) return;
    setError("");
    setBusy(true);
    try {
      const p = await api.upload(file);
      setPreview(p);
      setMapping(p.mapping);
      setMappingDirty(false);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function applyMapping() {
    setBusy(true);
    setError("");
    try {
      const p = await api.remap(preview.batch_id, mapping);
      setPreview(p);
      setMapping(p.mapping);
      setMappingDirty(false);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    setBusy(true);
    setError("");
    try {
      await api.confirm(preview.batch_id);
      setPreview(null);
      loadHistory();
      onImported();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function clearAll() {
    setBusy(true);
    setError("");
    try {
      setCleared(await api.resetData(typed));
      setAskClear(false);
      setHistory([]);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const needsMap =preview && (preview.missing_core.length > 0 || preview.unmatched_columns.length > 0);

  return (
    <div className="page">
      <div className="page-head">
        <h1>Upload sheet</h1>
        <p className="muted">Choose the Career Form file (.xlsx or .csv). Check the summary, then add the people.</p>
        <div className="steps">
          <div className={`step ${preview ? "done" : "on"}`}><i>{preview ? "✓" : "1"}</i> Choose file</div>
          <div className={`step ${preview ? "on" : ""}`}><i>2</i> Review &amp; match columns</div>
          <div className="step"><i>3</i> Save</div>
        </div>
      </div>
      {error && <div className="alert error">{error}</div>}

      {!preview && (
        <div
          className={`drop ${drag ? "over" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); handleFile(e.dataTransfer.files[0]); }}
          onClick={() => input.current.click()}
        >
          <input ref={input} type="file" accept=".xlsx,.xlsm,.csv" hidden onChange={(e) => handleFile(e.target.files[0])} />
          {busy ? "Reading the file…" : (
            <>
              <div className="up">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 16V4m0 0-4 4m4-4 4 4" /><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3" /></svg>
              </div>
              <b>Drop the file here</b><div>or click to choose it (.xlsx, .csv)</div>
            </>
          )}
        </div>
      )}

      {preview && (
        <section className="card">
          <div className="row between">
            <h2>{preview.file_name}</h2>
            <button className="link" onClick={() => setPreview(null)}>Choose another file</button>
          </div>
          <div className="summary">
            <div className="stat good"><b>{preview.counts.new}</b><span>New</span></div>
            {preview.counts.updated > 0 && <div className="stat"><b>{preview.counts.updated}</b><span>Updated</span></div>}
            {preview.counts.unchanged > 0 && <div className="stat"><b>{preview.counts.unchanged}</b><span>Already in</span></div>}
            {preview.counts.merged > 0 && <div className="stat"><b>{preview.counts.merged}</b><span>Repeats merged</span></div>}
            {preview.counts.need_fix > 0 && <div className="stat warn"><b>{preview.counts.need_fix}</b><span>Need a look</span></div>}
          </div>

          {preview.missing_core.length > 0 && (
            <div className="alert warn">These fields were not found: {preview.missing_core.join(", ")}. Match them below if the sheet has them.</div>
          )}

          {Object.keys(preview.issue_summary).length > 0 && (
            <ul className="issues">
              {Object.entries(preview.issue_summary).map(([k, v]) => <li key={k}>{v} · {k}</li>)}
            </ul>
          )}

          <details className="fold" open={needsMap}>
            <summary>{needsMap ? "Match the columns that were not recognised" : "Column matching looks fine. Change it only if a field is wrong."}</summary>
            <div className="mapgrid">
              {preview.headers.map((h) => (
                <label key={h} className={!mapping[h] ? "unmapped" : ""}>
                  <span title={h}>{h}</span>
                  <select value={mapping[h] || ""} onChange={(e) => { setMapping({ ...mapping, [h]: e.target.value || null }); setMappingDirty(true); }}>
                    <option value="">Skip</option>
                    {preview.field_options.map((o) => <option key={o.field} value={o.field}>{o.label}</option>)}
                  </select>
                </label>
              ))}
            </div>
            {mappingDirty && <button onClick={applyMapping} disabled={busy}>Update the summary</button>}
          </details>

          <div className="row end">
            <button className="primary" disabled={busy || mappingDirty} onClick={confirm}>
              {busy ? "Saving…" : preview.counts.new + preview.counts.updated === 0
                ? "Already up to date"
                : `Save ${preview.counts.new} new${preview.counts.updated ? `, ${preview.counts.updated} updated` : ""}`}
            </button>
          </div>
        </section>
      )}

      {history[0] && !preview && (
        <p className="muted">Last upload: {history[0].file_name}, {history[0].rows_added} added.</p>
      )}

      {!preview && (
        <section className="card danger-zone">
          <h2>Start fresh</h2>
          <p className="muted">
            Removes all candidates, uploads, call history and saved searches, and stops any waiting calls. Your question sets stay,
            and people marked “do not contact” stay blocked if you upload them again. A backup copy is saved first.
          </p>
          {cleared && <div className="alert ok">Cleared {cleared.removed} candidates. Backup saved as {cleared.backup}. You can upload your new file now.</div>}
          {!askClear ? (
            <button className="danger" onClick={() => { setAskClear(true); setTyped(""); setCleared(null); }}>Clear all data…</button>
          ) : (
            <div className="row gap">
              <input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="Type DELETE to confirm" style={{ maxWidth: 220 }} />
              <button className="danger" disabled={busy || typed !== "DELETE"} onClick={clearAll}>Yes, clear everything</button>
              <button onClick={() => setAskClear(false)}>Cancel</button>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
