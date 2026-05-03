// Unified time formatter — fixes the "STARTED 10:51 / log 1:51" drift
// the user reported in KST. Backend emits two flavors of ISO:
//
//   * autopilot.started_at        →  "2026-05-02T01:51:00.000000Z"
//   * event_bus.created_at        →  "2026-05-02T01:51:00.000000"  (naive)
//
// Native `new Date(naive)` parses the second string as LOCAL time
// instead of UTC. In KST that flipped a 10:51 KST event to 1:51 KST
// when displayed via toLocaleTimeString. The control_tower API stores
// every timestamp in UTC, so the rule is simple: if an ISO has no Z
// suffix and no ±HH:MM offset, append "Z" before parsing.
//
// Every component MUST use these helpers — never `iso.slice(11, 19)`
// (raw UTC substring) or `new Date(naive).toLocaleString()` (silently
// local-interpreted UTC).

const TZ_RE = /([Zz]|[+-]\d{2}:?\d{2})$/;

// Parse a backend ISO string. Returns a Date or null. Falls back to
// "treat as UTC" when no offset marker is present — that matches the
// FastAPI/SQLAlchemy default of naive UTC datetimes.
export function parseUtcIso(iso) {
  if (!iso) return null;
  const raw = String(iso).trim();
  if (!raw) return null;
  const s = TZ_RE.test(raw) ? raw : raw + "Z";
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return null;
  return d;
}

const TIME_FMT = { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false };
const DATETIME_FMT = {
  year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
};
const LOCALE = "ko-KR";

export function fmtTime(iso) {
  const d = parseUtcIso(iso);
  if (!d) return "—";
  return d.toLocaleTimeString(LOCALE, TIME_FMT);
}

export function fmtDateTime(iso) {
  const d = parseUtcIso(iso);
  if (!d) return "—";
  return d.toLocaleString(LOCALE, DATETIME_FMT);
}

// "now - started_at" elapsed in h/m/s. isRunning toggles the right-
// edge between Date.now() (live) and ended_at parse.
export function fmtElapsedFrom(startedIso, endedIso = null, isRunning = false) {
  const start = parseUtcIso(startedIso);
  if (!start) return "—";
  let end;
  if (isRunning) {
    end = Date.now();
  } else {
    const ended = parseUtcIso(endedIso);
    end = ended ? ended.getTime() : start.getTime();
  }
  const sec = Math.max(0, Math.floor((end - start.getTime()) / 1000));
  if (sec === 0 && !isRunning) return "—";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

// Compare timestamps for "is this event part of the current run?"
// Returns true when ev_iso is at-or-after run_started_iso. Used by
// SystemLogPanel to filter pre-run events into a "이전 명령 로그
// 보기" details/summary block.
export function isAfterStart(ev_iso, run_started_iso) {
  const ev = parseUtcIso(ev_iso);
  const start = parseUtcIso(run_started_iso);
  if (!ev || !start) return false;
  return ev.getTime() >= start.getTime();
}
