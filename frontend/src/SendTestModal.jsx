import React, { useEffect, useState } from "react";
import { api } from "./api.js";

const csvCell = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;

export function downloadCredentials(res) {
  const rows = [["Candidate ID", "Name", "Email", "Password", "Test link"],
    ...res.created.map((c) => [c.candidate_ref || "", c.name, c.email, c.password, res.exam_url])];
  const blob = new Blob(["﻿" + rows.map((r) => r.map(csvCell).join(",")).join("\r\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `test-logins-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

/** Pick a test, create a login (email + password) for each ticked candidate, and show the passwords once. */
export function SendTestModal({ ids, onClose, goTests }) {
  const [exams, setExams] = useState([]);
  const [examId, setExamId] = useState("");
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    api.exams().then((list) => { setExams(list); setExamId(String(list[0]?.id || "")); }).catch((e) => setError(e.message));
  }, []);

  async function send() {
    setBusy(true);
    setError("");
    try { setRes(await api.inviteExam(Number(examId), ids)); } catch (e) { setError(e.message); } finally { setBusy(false); }
  }

  async function copyAll() {
    const text = res.created.map((c) => `${c.name}\t${c.email}\t${c.password}`).join("\n");
    try { await navigator.clipboard.writeText(`Test link: ${res.exam_url}\n\n${text}`); setCopied(true); } catch { setError("Could not copy. Use the download instead."); }
  }

  const chosen = exams.find((e) => String(e.id) === examId);
  const noPublic = res && !res.exam_url.startsWith("https://");

  return (
    <div className="drawer-wrap center-modal" onClick={onClose}>
      <div className="modal wide" onClick={(e) => e.stopPropagation()}>
        <div className="row between"><h2>Send a test</h2><button className="x" onClick={onClose} aria-label="Close">×</button></div>
        {error && <div className="alert error">{error}</div>}
        {!res ? (
          exams.length === 0 ? (
            <>
              <p>You have not built a test yet.</p>
              <div className="row end gap"><button onClick={onClose}>Close</button><button className="primary" onClick={goTests}>Build a test</button></div>
            </>
          ) : (
            <>
              <p>Give <b>{ids.length}</b> {ids.length === 1 ? "candidate" : "candidates"} a login for this test. Each gets their own password, and they log in with the email in their record.</p>
              <label className="field"><span>Test</span>
                <select value={examId} onChange={(e) => setExamId(e.target.value)}>
                  {exams.map((x) => <option key={x.id} value={x.id}>{x.title}</option>)}
                </select>
              </label>
              {chosen && <p className="muted small">{chosen.question_count} questions · {chosen.total_marks} marks · {chosen.duration_minutes} minutes</p>}
              <div className="row end gap"><button onClick={onClose}>Cancel</button><button className="primary" disabled={busy || !examId} onClick={send}>{busy ? "Creating…" : `Create ${ids.length} login${ids.length === 1 ? "" : "s"}`}</button></div>
            </>
          )
        ) : (
          <>
            {res.created.length > 0 && (
              <>
                {res.email_enabled ? (
                  <div className={`alert ${res.created.every((c) => c.email_sent) ? "info" : "warn"}`}>
                    <b>{res.created.filter((c) => c.email_sent).length} of {res.created.length} emailed</b> with the test link, their login email and password.
                    {res.created.some((c) => !c.email_sent) && " For the ones marked “Not sent”, give them the password below yourself."}
                  </div>
                ) : (
                  <div className="alert warn"><b>Emails are not set up yet</b>, so nothing was sent. Add the SMTP settings to backend/.env (see below), or give candidates the passwords yourself.</div>
                )}
                <div className="alert warn"><b>Passwords are shown only this once</b> and cannot be read back later. If a candidate loses theirs, use “Reset password” in the test’s results (it emails the new one).</div>
                <p>Candidates open <b>{res.exam_url}</b>, log in with their email and the password below, and start the test in full screen.</p>
                {res.created.some((c) => c.email_error) && <div className="alert error">Email problem: {res.created.find((c) => c.email_error).email_error}</div>}
                {noPublic &&<div className="alert error">The public address is not set (PUBLIC_BASE_URL in backend/.env), so candidates outside this computer cannot open the link yet.</div>}
                <div className="tablewrap short">
                  <table className="results"><thead><tr><th>Candidate ID</th><th>Name</th><th>Email</th><th>Password</th><th>Email</th></tr></thead>
                    <tbody>{res.created.map((c) => <tr key={c.invite_id}><td>{c.candidate_ref}</td><td>{c.name}</td><td>{c.email}</td><td><code>{c.password}</code></td>
                      <td title={c.email_error || ""}>{c.email_sent ? "Sent" : res.email_enabled ? "Not sent" : "—"}</td></tr>)}</tbody></table>
                </div>
                <div className="row gap" style={{ marginTop: 12 }}>
                  <button className="primary" onClick={() => downloadCredentials(res)}>Download logins (CSV)</button>
                  <button onClick={copyAll}>{copied ? "Copied" : "Copy all"}</button>
                </div>
              </>
            )}
            {(res.skipped.length > 0 || res.existing.length > 0) && (
              <div className="alert info">
                {res.skipped.length > 0 && <div><b>Not invited:</b> {res.skipped.map((s) => `${s.name} (${s.reason})`).join("; ")}</div>}
                {res.existing.length > 0 && <div style={{ marginTop: 6 }}><b>Already invited:</b> {res.existing.map((s) => s.name).join(", ")}. Use “Reset password” in the results if they lost it.</div>}
              </div>
            )}
            <div className="row end gap"><button onClick={onClose}>Close</button><button className="primary" onClick={goTests}>Open Tests</button></div>
          </>
        )}
      </div>
    </div>
  );
}
