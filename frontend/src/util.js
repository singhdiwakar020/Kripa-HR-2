// Times come from the server as UTC ISO strings. HR works in India time.
export function fmtWhen(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata", weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit",
  }) + " IST";
}

export const STATE_LABEL = {
  queued: "Waiting to call", calling: "Calling now", retry_wait: "Will retry", recall_wait: "Callback booked",
  done: "Done", gave_up: "Gave up", cancelled: "Cancelled",
};

export const RECORD_LABEL = {
  ringing: "Ringing", in_progress: "On the call", completed: "Answered all questions", callback_requested: "Asked for a callback",
  no_answer: "No answer", busy: "Line busy", failed: "Call failed", incomplete: "Ended early", opted_out: "Asked not to be called",
  not_interested: "Not interested",
};

export function answerText(a) {
  if (a.kind === "yesno") return a.value === true ? "Yes" : a.value === false ? "No" : a.raw;
  if (a.kind === "number" && a.value != null) return String(a.value);
  return a.raw;
}
