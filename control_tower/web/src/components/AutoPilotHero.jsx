import { useEffect, useMemo, useState } from "react";

// AutoPilotHero — top-of-page hero block.
//
// Single source of truth: runners[].metadata_json.local_factory.autopilot.
// Renders an Instagram-Story style banner with status, mode, cycle
// progress, elapsed time, and a strip of "last X" stats. The full form
// (start/stop, knobs) lives in AutoPilotPanel below; this hero only
// reads.

const STATUS_TONE = {
  idle:     { label: "READY",     color: "#94a3b8", glow: "#94a3b833" },
  running:  { label: "RUNNING",   color: "#34d399", glow: "#34d39955" },
  stopping: { label: "STOPPING",  color: "#fbbf24", glow: "#fbbf2455" },
  stopped:  { label: "STOPPED",   color: "#94a3b8", glow: "#94a3b833" },
  failed:   { label: "FAILED",    color: "#f87171", glow: "#f8717155" },
  unknown:  { label: "UNKNOWN",   color: "#64748b", glow: "#64748b33" },
};

const MODE_LABEL = {
  safe_run:     "SAFE RUN",
  auto_commit:  "AUTO COMMIT",
  auto_publish: "AUTO PUBLISH",
};

function pickAutopilot(runners = []) {
  for (const r of runners) {
    const ap = r?.metadata_json?.local_factory?.autopilot;
    if (ap) return ap;
  }
  return null;
}

function fmtElapsed(startedIso, endedIso, isRunning) {
  if (!startedIso) return "—";
  try {
    const start = new Date(startedIso).getTime();
    const end = isRunning
      ? Date.now()
      : (endedIso ? new Date(endedIso).getTime() : Date.now());
    const sec = Math.max(0, Math.floor((end - start) / 1000));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) return `${h}h ${m}m ${s}s`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
  } catch {
    return "—";
  }
}

function fmtIso(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleTimeString("ko-KR", { hour12: false });
  } catch {
    return iso;
  }
}

function StatChip({ label, value, color }) {
  return (
    <div
      className="autopilot-stat-chip flex flex-col gap-0.5 rounded-lg px-2.5 py-1.5"
      style={{
        backgroundColor: "#0a1228",
        border: `1px solid ${color || "#1e293b"}55`,
        minWidth: 88,
      }}
    >
      <span className="text-[8.5px] font-bold uppercase tracking-[0.25em] text-slate-500">
        {label}
      </span>
      <span
        className="truncate text-[11px] font-bold"
        style={{ color: color || "#cbd5e1" }}
        title={value}
      >
        {value || "—"}
      </span>
    </div>
  );
}

function ProgressBar({ value, max, color }) {
  const pct = Math.min(100, Math.max(0, max > 0 ? (value / max) * 100 : 0));
  return (
    <div
      className="relative h-1.5 w-full overflow-hidden rounded-full"
      style={{ backgroundColor: "#0a1228", border: "1px solid #1e293b" }}
    >
      <div
        className="autopilot-progress-fill h-full rounded-full transition-all duration-500 ease-out"
        style={{
          width: `${pct}%`,
          backgroundColor: color,
          boxShadow: `0 0 10px ${color}aa`,
        }}
      />
    </div>
  );
}

export default function AutoPilotHero({ runners = [] }) {
  const state = useMemo(() => pickAutopilot(runners), [runners]);

  // Re-render every second while running so elapsed counts up live.
  const [, force] = useState(0);
  const isRunning = state?.status === "running";
  useEffect(() => {
    if (!isRunning) return;
    const id = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [isRunning]);

  if (!state) {
    return (
      <section
        className="autopilot-hero-empty flex flex-col gap-1 rounded-xl px-4 py-3"
        data-testid="autopilot-hero"
        style={{
          backgroundColor: "#0e1a35",
          border: "1.5px solid #d4a84355",
          fontFamily: "ui-monospace, monospace",
        }}
      >
        <div className="flex items-center gap-2">
          <span className="text-[9px] font-bold uppercase tracking-[0.3em] text-amber-300">
            AUTO PILOT
          </span>
          <span className="rounded-full bg-slate-800 px-2 py-0.5 text-[9px] tracking-widest text-slate-400">
            STANDBY
          </span>
        </div>
        <p className="text-[12px] leading-snug text-slate-300">
          러너 heartbeat 대기 중 — runner가 등록되면 Auto Pilot 상태가 여기에 표시됩니다.
        </p>
      </section>
    );
  }

  const status = state.status || "unknown";
  const tone = STATUS_TONE[status] || STATUS_TONE.unknown;
  const mode = state.mode || "safe_run";
  const cycleCount = state.cycle_count ?? 0;
  const maxCycles = state.max_cycles ?? 0;
  const lastVerdict = state.last_verdict || "—";
  const lastFailure = state.last_failure_code || "";
  const lastCommit = (state.last_commit_hash || "").slice(0, 8) || "—";
  const lastPush = state.last_push_status || "—";
  const lastHealth = state.last_health_status || "—";
  const lastRender = state.last_render_status || "—";
  const stopReason = state.stop_reason || "";
  const reportPath = state.report_path || ".runtime/autopilot_report.md";
  const elapsed = fmtElapsed(state.started_at, state.ended_at, isRunning);
  const maxHours = state.max_hours || 0;

  const verdictColor =
    lastVerdict === "PASS" || lastVerdict === "passed"
      ? "#34d399"
      : lastVerdict === "FAIL" || lastVerdict === "failed"
      ? "#f87171"
      : lastVerdict === "HOLD"
      ? "#a78bfa"
      : "#cbd5e1";

  return (
    <section
      className="autopilot-hero relative flex flex-col gap-2 overflow-hidden rounded-xl px-4 py-3"
      data-testid="autopilot-hero"
      style={{
        background:
          "linear-gradient(135deg, #0e1a35 0%, #15264a 60%, #0a1228 100%)",
        border: `1.5px solid ${tone.color}66`,
        boxShadow: `0 0 24px ${tone.glow}`,
        fontFamily: "ui-monospace, monospace",
      }}
    >
      {/* Story-style top progress (cycles). */}
      <div className="flex items-center gap-1.5">
        {Array.from({ length: Math.max(1, maxCycles) }).map((_, i) => {
          const done = i < cycleCount;
          const active = i === cycleCount && isRunning;
          return (
            <div
              key={i}
              className="autopilot-story-tick h-[3px] flex-1 rounded-full"
              style={{
                backgroundColor: done
                  ? tone.color
                  : active
                  ? `${tone.color}aa`
                  : "#1e293b",
                boxShadow: done ? `0 0 6px ${tone.color}aa` : "none",
              }}
            />
          );
        })}
      </div>

      {/* Headline row */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-2">
          <span
            className={`autopilot-status-dot inline-block h-2.5 w-2.5 rounded-full ${
              isRunning ? "autopilot-pulse" : ""
            }`}
            style={{
              backgroundColor: tone.color,
              boxShadow: `0 0 10px ${tone.color}`,
            }}
          />
          <span className="text-[10px] font-bold uppercase tracking-[0.35em] text-amber-300">
            AUTO PILOT
          </span>
          <span
            className="rounded-full px-2 py-0.5 text-[9.5px] font-bold tracking-[0.25em]"
            style={{ backgroundColor: tone.color, color: "#0a1228" }}
          >
            {tone.label}
          </span>
          <span
            className="rounded-full px-2 py-0.5 text-[9.5px] font-bold tracking-[0.25em]"
            style={{
              color: "#d4a843",
              border: "1px solid #d4a84366",
              backgroundColor: "#0a1228",
            }}
          >
            {MODE_LABEL[mode] || mode.toUpperCase()}
          </span>
        </div>
        <div className="ml-auto flex items-center gap-2 text-[10px] tracking-widest text-slate-400">
          <span>
            CYCLE{" "}
            <span
              style={{ color: tone.color, fontWeight: 700 }}
              data-testid="autopilot-cycle-count"
            >
              {cycleCount}
            </span>
            <span className="text-slate-600"> / {maxCycles || "∞"}</span>
          </span>
          <span className="hidden text-slate-600 sm:inline">·</span>
          <span>
            ELAPSED{" "}
            <span style={{ color: "#cbd5e1", fontWeight: 700 }}>{elapsed}</span>
            {maxHours > 0 && (
              <span className="text-slate-600"> / {maxHours}h</span>
            )}
          </span>
        </div>
      </div>

      <ProgressBar
        value={cycleCount}
        max={Math.max(1, maxCycles)}
        color={tone.color}
      />

      {/* Last X strip — wraps cleanly on mobile */}
      <div className="flex flex-wrap gap-1.5">
        <StatChip
          label="VERDICT"
          value={lastFailure ? `${lastVerdict} · ${lastFailure}` : lastVerdict}
          color={verdictColor}
        />
        <StatChip label="COMMIT" value={lastCommit} color="#22d3ee" />
        <StatChip label="PUSH" value={lastPush} color="#f472b6" />
        <StatChip label="RENDER" value={lastRender} color="#34d399" />
        <StatChip label="HEALTH" value={lastHealth} color="#34d399" />
        <StatChip
          label="STARTED"
          value={fmtIso(state.started_at)}
          color="#cbd5e1"
        />
      </div>

      {/* Stop reason / report — only when there's something to say */}
      {(stopReason || status === "failed") && (
        <div
          className="rounded-lg px-2.5 py-1.5 text-[11px]"
          style={{
            backgroundColor: "#1c0d12",
            border: `1px solid ${tone.color}55`,
            color: "#fecaca",
          }}
        >
          <span className="mr-1 text-[9.5px] font-bold tracking-widest text-rose-300">
            STOP REASON
          </span>
          <span>{stopReason || "(no reason recorded)"}</span>
        </div>
      )}

      <div className="flex items-center justify-between text-[10px] tracking-widest text-slate-500">
        <span>
          REPORT ·{" "}
          <span className="font-mono text-slate-400">{reportPath}</span>
        </span>
        <span data-testid="autopilot-mode-marker" className="text-slate-600">
          mode={mode}
        </span>
      </div>
    </section>
  );
}
