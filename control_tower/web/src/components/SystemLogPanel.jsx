import { useMemo, useState } from "react";
import {
  SYSTEM_LOG_CATEGORIES,
  buildSystemLogEntries,
} from "../utils/eventClassifier.js";

// SystemLogPanel — the operator's "what's happening on the runner /
// what just broke" feed. Distinct from EventFeed (general timeline)
// and PingPongBoard (agent activity). Categories live in
// utils/eventClassifier.js so the same source of truth drives both
// the chip filter and the row styling.
//
// Larger than ArtifactBoard by design — when something is wrong,
// this is the panel the operator stares at.

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
  Error:   "#f87171",
};

const PHASE_LABEL = {
  started:   "시작",
  completed: "완료",
  failed:    "실패",
  queued:    "대기",
  info:      "",
};

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour12: false });
  } catch {
    return String(iso).slice(0, 19);
  }
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
  return (
    <li
      className="rounded px-2.5 py-1.5"
      style={{
        backgroundColor: "#0a1228",
        border: `1px solid ${sev.dot}33`,
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
        <span className="ml-auto text-[9px] text-slate-600">#{entry.ev.id}</span>
      </div>
      <div
        className="mt-1 text-[12px] leading-snug"
        style={{ color: sev.text }}
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

export default function SystemLogPanel({ events = [] }) {
  const [filter, setFilter] = useState("All");

  // Build classified entries once per `events` change. Cap to
  // MAX_ENTRIES so a long-running session doesn't render thousands
  // of DOM nodes.
  const allEntries = useMemo(() => buildSystemLogEntries(events), [events]);

  const filtered = useMemo(() => {
    const visible = filter === "All"
      ? allEntries
      : allEntries.filter((e) => e.category === filter);
    return visible.slice(0, MAX_ENTRIES);
  }, [allEntries, filter]);

  // Per-category counts for the chip badges. "All" shows the total.
  const counts = useMemo(() => {
    const m = { All: allEntries.length };
    for (const cat of SYSTEM_LOG_CATEGORIES) {
      if (cat === "All") continue;
      m[cat] = allEntries.filter((e) => e.category === cat).length;
    }
    return m;
  }, [allEntries]);

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
            runner · command · claude · build · QA · git · deploy
          </span>
        </div>
        <span className="text-[10px] tracking-widest text-slate-500">
          {filtered.length} / {allEntries.length}
        </span>
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
