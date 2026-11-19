async function request(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (body.detail) msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {}
    throw new Error(msg);
  }
  return res.json();
}

const json = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  upload(file) {
    const fd = new FormData();
    fd.append("file", file);
    return request("/api/imports", { method: "POST", body: fd });
  },
  remap: (id, mapping) => request(`/api/imports/${id}/preview`, json("POST", mapping)),
  confirm: (id) => request(`/api/imports/${id}/confirm`, { method: "POST" }),
  resetData: (confirm) => request("/api/reset", json("POST", { confirm })),
  imports: () => request("/api/imports"),
  facets: () => request("/api/facets"),
  search(q, filters, sort, offset = 0) {
    const p = new URLSearchParams();
    if (q) p.set("q", q);
    Object.entries(filters || {}).forEach(([k, v]) => {
      if (v === undefined || v === null || v === "" || v === false) return;
      p.set(k, Array.isArray(v) ? v.join(",") : v);
    });
    p.set("sort", sort || "relevance");
    p.set("offset", offset);
    p.set("limit", 20);
    return request(`/api/search?${p}`);
  },
  candidate: (id) => request(`/api/candidates/${id}`),
  editCandidate: (id, patch) => request(`/api/candidates/${id}`, json("PATCH", patch)),
  setDnc: (id, value) => request(`/api/candidates/${id}/do-not-contact`, json("POST", { value })),
  savedList: () => request("/api/saved-searches"),
  savedCreate: (name, query, filters) => request("/api/saved-searches", json("POST", { name, query, filters })),
  savedDelete: (id) => request(`/api/saved-searches/${id}`, { method: "DELETE" }),
  exportUrl: (ids) => `/api/export${ids?.length ? `?ids=${ids.join(",")}` : ""}`,

  // calling
  questionSets: () => request("/api/question-sets"),
  saveQuestionSet: (s) => (s.id ? request(`/api/question-sets/${s.id}`, json("PUT", s)) : request("/api/question-sets", json("POST", s))),
  deleteQuestionSet: (id) => request(`/api/question-sets/${id}`, { method: "DELETE" }),
  startCalls: (candidate_ids, question_set_id, include_screened = false) =>
    request("/api/calls/start", json("POST", { candidate_ids, question_set_id, include_screened })),
  callResults: (only) => request(`/api/calls/results?only=${only}`),
  callExportUrl: ({ only, merge, format }) => `/api/calls/export?only=${only}&merge=${merge ? 1 : 0}&format=${format}`,
  calls: () => request("/api/calls"),
  cancelCall: (id) => request(`/api/calls/jobs/${id}/cancel`, { method: "POST" }),
  cancelAllCalls: () => request("/api/calls/cancel-all", { method: "POST" }),
  callNow: (id) => request(`/api/calls/jobs/${id}/call-now`, { method: "POST" }),
  callRecord: (id) => request(`/api/calls/records/${id}`),
  candidateCalls: (id) => request(`/api/candidates/${id}/calls`),
  simAnswer: (id) => request(`/api/calls/records/${id}/sim/answer`, { method: "POST" }),
  simOutcome: (id, outcome) => request(`/api/calls/records/${id}/sim/outcome`, json("POST", { outcome })),
  simSay: (id, text) => request(`/api/calls/records/${id}/sim/say`, json("POST", { text })),
  exams: () => request("/api/exams"),
  exam: (id) => request(`/api/exams/${id}`),
  saveExam: (id, body) => request(id ? `/api/exams/${id}` : "/api/exams", json(id ? "PUT" : "POST", body)),
  deleteExam: (id) => request(`/api/exams/${id}`, { method: "DELETE" }),
  inviteExam: (id, candidate_ids) => request(`/api/exams/${id}/invite`, json("POST", { candidate_ids })),
  resetExamPassword: (inviteId) => request(`/api/exams/invites/${inviteId}/reset-password`, { method: "POST" }),
  examResults: (id) => request(`/api/exams/${id}/results`),
  examExportUrl: (id, format) => `/api/exams/${id}/export?format=${format}`,
  examAttempt: (inviteId) => request(`/api/exams/invites/${inviteId}`),
  markExamAttempt: (inviteId, body) => request(`/api/exams/invites/${inviteId}`, json("PATCH", body)),
  simHangup: (id) => request(`/api/calls/records/${id}/sim/hangup`, { method: "POST" }),
};
