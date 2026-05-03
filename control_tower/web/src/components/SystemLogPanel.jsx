import { useMemo, useState } from "react";
import {
  SYSTEM_LOG_CATEGORIES,
  buildSystemLogEntries,
} from "../utils/eventClassifier.js";
import { fmtTime, isAfterStart, parseUtcIso } from "../utils/time.js";
import { pickAutopilot } from "../utils/autopilotPhase.js";

// SystemLogPanel — the operator's "what's happening on the runner /
// what just broke" feed. Distinct from EventFeed (general timeline)
// and PingPongBoard (agent activity). Categories live in
// utils/eventClassifier.js so the same source of truth drives both
// the chip filter and the row styling.
//
// Heartbeat collapse:
//   - heartbeat events (local_runner_heartbeat) are NOISY — one tick
//     every 1.5s drowns out the events the operator actually wants
//     to see. They're hidden by default with a "heartbeat 보기" toggle.
//     A small status line at the top summarizes runner online + last
//     heartbeat time so the operator still has the pulse signal.
//   - lifecycle events (start/stop autopilot, cycle start/verdict,
//     git push, render smoke, health) get a gold "highlight" glow so
//     they pop out of the firehose.

const MAX_ENTRIES = 80;

const SEVERITY_STYLE = {
  info:    { dot: "#94a3b8", text: "#cbd5e1" },
  success: { dot: "#34d399", text: "#86efac" },
  warn:    { dot: "#fbbf24", text: "#fde68a" },
  error:   { dot: "#f87171", text: "#fecaca" },
};

const CATEGORY_COLOR = {
  Runner:  "#38bdf8",
  Command: "#a78bfa",
  Claude:  "#facc15",
  Build:   "#34d399",
  QA:      "#fb923c",
  Git:     "#22d3ee",
  Deploy:  "#f472b6",
  Doctor:  "#a78bfa",
  Error:   "#f87171",
};

const PHASE_LABEL = {
  started:   "시작",
  completed: "완료",
  failed:    "실패",
  queued:    "대기",
  info:      "",
};

// Event types / message patterns that are LIFECYCLE — start/stop
// autopilot, cycle pulses, commit/push, render/health. They get a
// gold glow border so they don't drown in the firehose.
const HIGHLIGHT_PATTERNS = [
  /start_autopilot/i,
  /stop_autopilot/i,
  /autopilot\s+(started|stopped|failed|completed)/i,
  /cycle\s+(started|completed|failed|produced)/i,
  /verdict/i,
  /commit\s+created|git\s+commit/i,
  /git\s+push\s+(started|completed|done|success|failed)/i,
  /render\s+smoke/i,
  /production\s+health|health\s*check/i,
];

// fmtTime now imported from utils/time.js — naive ISO strings are
// treated as UTC so the timestamp matches AutoPilotPanel STARTED.

function isHeartbeatEvent(ev) {
  return ev?.type === "local_runner_heartbeat";
}

function isHighlightEvent(ev) {
  if (!ev) return false;
  const msg = ev.message || "";
  if (!msg) return false;
  for (const pat of HIGHLIGHT_PATTERNS) {
    if (pat.test(msg)) return true;
  }
  return false;
}

function ActorPill({ actor }) {
  if (!actor) return null;
  const label = {
    runner: "RUNNER",
    claude: "CLAUDE",
    factory: "FACTORY",
    github: "GITHUB",
    system: "SYSTEM",
  }[actor] || String(actor).toUpperCase();
  return (
    <span
      className="rounded px-1.5 py-0.5 text-[8.5px] font-bold tracking-widest text-slate-400"
      style={{ border: "1px solid #1e293b", backgroundColor: "#0a1228" }}
    >
      {label}
    </span>
  );
}

function CategoryPill({ category }) {
  const color = CATEGORY_COLOR[category] || "#94a3b8";
  return (
    <span
      className="rounded px-1.5 py-0.5 text-[9px] font-bold tracking-widest"
      style={{
        color,
        border: `1px solid ${color}66`,
        backgroundColor: "#0a1228",
      }}
    >
      {category.toUpperCase()}
    </span>
  );
}

function LogRow({ entry }) {
  const sev = SEVERITY_STYLE[entry.severity] || SEVERITY_STYLE.info;
  const phaseText = PHASE_LABEL[entry.phase] || "";
  const detail = entry.ev.payload && Object.keys(entry.ev.payload).length > 0
    ? entry.ev.payload
    : null;
  const highlighted = isHighlightEvent(entry.ev);
  const borderColor = highlighted ? "#d4a843" : `${sev.dot}33`;
  return (
    <li
      className={
        "rounded px-2.5 py-1.5 " +
        (highlighted ? "system-log-highlight" : "")
      }
      style={{
        backgroundColor: highlighted ? "#150f04" : "#0a1228",
        border: `1px solid ${borderColor}`,
        boxShadow: highlighted ? "0 0 14px #d4a84366" : "none",
      }}
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] tracking-wider">
        <span
          className="inline-block h-1.5 w-1.5 rounded-full"
          style={{ backgroundColor: sev.dot }}
        />
        <span className="text-slate-500">{fmtTime(entry.ev.created_at)}</span>
        <CategoryPill category={entry.category} />
        <ActorPill actor={entry.actor} />
        {phaseText && (
          <span
            className="text-[9.5px] font-bold tracking-widest"
            style={{ color: sev.text }}
          >
            {phaseText}
          </span>
        )}
        {highlighted && (
          <span
            className="rounded px-1.5 py-0.5 text-[8.5px] font-bold tracking-widest"
            style={{
              color: "#0a1228",
              backgroundColor: "#d4a843",
            }}
          >
            ★ KEY
          </span>
        )}
        <span className="ml-auto text-[9px] text-slate-600">#{entry.ev.id}</span>
      </div>
      <div
        className="mt-1 text-[12px] leading-snug"
        style={{ color: highlighted ? "#fde68a" : sev.text }}
      >
        {entry.ev.message || "(no message)"}
      </div>
      {detail && (
        <details className="mt-1 text-[10px] text-slate-500">
          <summary className="cursor-pointer text-slate-500 hover:text-slate-300">
            detail
          </summary>
          <pre
            className="mt-1 overflow-x-auto whitespace-pre-wrap break-all rounded p-2 text-[10px] text-slate-400"
            style={{ backgroundColor: "#050912", border: "1px solid #1e293b" }}
          >
            {JSON.stringify(detail, null, 2)}
          </pre>
        </details>
      )}
    </li>
  );
}

export default function SystemLogPanel({ events = [], runners = [] }) {
  const [filter, setFilter] = useState("All");
  const [showHeartbeat, setShowHeartbeat] = useState(false);
  const [showOlder, setShowOlder] = useState(false);

  // Auto Pilot started_at — when present and the loop is running we
  // default to "current run only" so a previous start_autopilot
  // failure doesn't masquerade as a fresh error. Older events are
  // collapsed under a "이전 명령 로그 보기" toggle.
  const autopilot = useMemo(() => pickAutopilot(runners), [runners]);
  const runStartIso = autopilot?.started_at;
  const isAutopilotRunning = String(autopilot?.status || "").toLowerCase() === "running";
  const filterCurrentRun = isAutopilotRunning && !!runStartIso;

  // Build classified entries once per `events` change. Cap to
  // MAX_ENTRIES so a long-running session doesn't render thousands
  // of DOM nodes.
  const allEntries = useMemo(() => buildSystemLogEntries(events), [events]);

  // Heartbeat summary — runner online + last heartbeat time. Computed
  // from raw events because allEntries already filters them out via
  // the classifier (heartbeats sit at category=Runner severity=info,
  // type=local_runner_heartbeat).
  const heartbeatSummary = useMemo(() => {
    let last = null;
    let count = 0;
    for (const ev of events) {
      if (isHeartbeatEvent(ev)) {
        count += 1;
        if (!last || (ev.id || 0) > (last.id || 0)) last = ev;
      }
    }
    return { last, count };
  }, [events]);

  // Split entries into current-run vs older. When autopilot is
  // running we default to the current-run partition; the operator
  // can flip `showOlder` to expand the prior log. When autopilot is
  // idle/stopped the partition is identity (all entries fall in
  // "current") so the chip filters work as before.
  const partitioned = useMemo(() => {
    if (!filterCurrentRun) {
      return { current: allEntries, older: [] };
    }
    const current = [];
    const older = [];
    for (const entry of allEntries) {
      if (isAfterStart(entry.ev?.created_at, runStartIso)) current.push(entry);
      else older.push(entry);
    }
    return { current, older };
  }, [allEntries, filterCurrentRun, runStartIso]);

  const filtered = useMemo(() => {
    const base = filterCurrentRun
      ? (showOlder ? allEntries : partitioned.current)
      : allEntries;
    let visible = base;
    if (!showHeartbeat) {
      visible = visible.filter((e) => !isHeartbeatEvent(e.ev));
    }
    if (filter !== "All") {
      visible = visible.filter((e) => e.category === filter);
    }
    return visible.slice(0, MAX_ENTRIES);
  }, [allEntries, partitioned, filter, showHeartbeat, showOlder, filterCurrentRun]);

  // Per-category counts for the chip badges (always exclude heartbeat
  // unless the toggle is on, so the chip numbers reflect what's
  // actually visible).
  const counts = useMemo(() => {
    const base = showHeartbeat
      ? allEntries
      : allEntries.filter((e) => !isHeartbeatEvent(e.ev));
    const m = { All: base.length };
    for (const cat of SYSTEM_LOG_CATEGORIES) {
      if (cat === "All") continue;
      m[cat] = base.filter((e) => e.category === cat).length;
    }
    return m;
  }, [allEntries, showHeartbeat]);

  const heartbeatLast = heartbeatSummary.last;
  const heartbeatLabel = heartbeatLast
    ? `RUNNER · last heartbeat ${fmtTime(heartbeatLast.created_at)} (${heartbeatSummary.count} ticks)`
    : "RUNNER · heartbeat 대기 중";

  return (
    <section
      className="flex min-h-[260px] flex-col gap-2 p-3"
      data-testid="system-log-panel"
      style={{
        backgroundColor: "#0e1a35",
        border: "1.5px solid #0e4a3a",
        borderRadius: 6,
        fontFamily: "ui-monospace, monospace",
      }}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span
            className="inline-block h-2 w-2"
            style={{ backgroundColor: "#34d399" }}
          />
          <span className="text-[10px] font-bold uppercase tracking-[0.3em] text-emerald-300">
            SYSTEM LOG
          </span>
          <span className="text-[9.5px] tracking-widest text-slate-500">
            autopilot · cycle · git · deploy · errors
          </span>
        </div>
        <span
          className="text-[10px] tracking-widest text-slate-500"
          data-testid="system-log-counts"
        >
          {filterCurrentRun
            ? `현재 run ${partitioned.current.length} / 전체 ${allEntries.length}`
            : `${filtered.length} / ${allEntries.length}`}
        </span>
      </div>

      {/* Heartbeat summary strip — replaces the heartbeat firehose with
          one line of "runner is alive". */}
      <div
        className="flex flex-wrap items-center gap-2 rounded px-2 py-1 text-[9.5px] tracking-widest"
        data-testid="system-log-heartbeat-strip"
        style={{
          backgroundColor: "#0a1228",
          border: "1px solid #1e293b",
          color: "#94a3b8",
        }}
      >
        <span
          className="inline-block h-1.5 w-1.5 rounded-full"
          style={{
            backgroundColor: heartbeatLast ? "#34d399" : "#475569",
            boxShadow: heartbeatLast ? "0 0 8px #34d39966" : "none",
          }}
        />
        <span>{heartbeatLabel}</span>
        {filterCurrentRun && (
          <button
            type="button"
            onClick={() => setShowOlder((v) => !v)}
            className="ml-auto rounded px-2 py-0.5 text-[9px] font-bold tracking-widest transition"
            style={{
              color: showOlder ? "#0a1228" : "#94a3b8",
              backgroundColor: showOlder ? "#94a3b8" : "#0a1228",
              border: "1px solid #94a3b855",
              cursor: "pointer",
            }}
            data-testid="system-log-older-toggle"
          >
            {showOlder
              ? `이전 명령 로그 숨기기 (${partitioned.older.length})`
              : `이전 명령 로그 보기 (${partitioned.older.length})`}
          </button>
        )}
        <button
          type="button"
          onClick={() => setShowHeartbeat((v) => !v)}
          className={
            "rounded px-2 py-0.5 text-[9px] font-bold tracking-widest transition " +
            (filterCurrentRun ? "" : "ml-auto")
          }
          style={{
            color: showHeartbeat ? "#0a1228" : "#94a3b8",
            backgroundColor: showHeartbeat ? "#d4a843" : "#0a1228",
            border: "1px solid #d4a84366",
            cursor: "pointer",
          }}
          data-testid="heartbeat-toggle"
        >
          {showHeartbeat ? "heartbeat 숨기기" : "heartbeat 보기"}
        </button>
      </div>

      {/* Category filter chips */}
      <div className="-mx-1 flex gap-1 overflow-x-auto px-1 pb-1">
        {SYSTEM_LOG_CATEGORIES.map((cat) => {
          const active = filter === cat;
          const color = cat === "All" ? "#d4a843" : (CATEGORY_COLOR[cat] || "#94a3b8");
          return (
            <button
              key={cat}
              type="button"
              onClick={() => setFilter(cat)}
              className="whitespace-nowrap rounded px-2 py-1 text-[10px] font-bold tracking-widest transition"
              style={{
                color: active ? "#0a1228" : color,
                backgroundColor: active ? color : "#0a1228",
                border: `1px solid ${color}66`,
                cursor: "pointer",
              }}
            >
              {cat.toUpperCase()}
              <span
                className="ml-1.5 text-[9px] opacity-80"
                style={{ color: active ? "#0a1228" : "#94a3b8" }}
              >
                {counts[cat] || 0}
              </span>
            </button>
          );
        })}
      </div>

      {/* Log feed */}
      <div className="-mx-1 flex max-h-[60vh] min-h-[240px] flex-1 flex-col overflow-y-auto px-1">
        {filtered.length === 0 ? (
          <div className="flex flex-1 items-center justify-center py-8 text-center text-[11px] text-slate-500">
            {allEntries.length === 0
              ? "아직 system 이벤트가 없습니다 — runner가 heartbeat를 보내거나 명령이 큐에 들어오면 여기에 표시됩니다."
              : `${filter} 카테고리에 일치하는 항목이 없습니다.`}
          </div>
        ) : (
          <ul className="space-y-1.5">
            {filtered.map((entry) => (
              <LogRow key={entry.ev.id} entry={entry} />
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
