// Overall Status Bar — single source of truth at the top of the page.
//
// Reads runner heartbeat metadata.local_factory.control_state (written
// by control_state.py:aggregate). The aggregator already reconciled
// every sub-system's view, so this bar never has to compute its own
// status — it just renders the verdict.
//
// Rules the aggregator enforces (and that this bar therefore reflects):
//   - Watchdog HEALTHY + Progress STUCK → impossible
//   - Pipeline HEALTHY + Agent Accountability BLOCKED → impossible
//   - Deploy failed + changed_files=0 → reclassified to no_changes
//   - completed + meaningful_change=false → refused, falls to blocked
//   - operator_request stale qa_failed → treated as healthy (degraded)

const STATUS_TONE = {
  idle:              { label: "IDLE",              color: "#94a3b8" },
  running:           { label: "RUNNING",           color: "#34d399" },
  blocked:           { label: "BLOCKED",           color: "#fb923c" },
  failed:            { label: "FAILED",            color: "#f87171" },
  operator_required: { label: "OPERATOR REQUIRED", color: "#f87171" },
  completed:         { label: "COMPLETED",         color: "#34d399" },
  unknown:           { label: "UNKNOWN",           color: "#64748b" },
};

const PROGRESS_TONE = {
  progressing:       { label: "PROGRESSING",  color: "#34d399" },
  blocked:           { label: "BLOCKED",      color: "#fb923c" },
  stuck:             { label: "STUCK",        color: "#f87171" },
  planning_only:     { label: "PLANNING ONLY",color: "#a78bfa" },
  no_progress:       { label: "NO PROGRESS",  color: "#fb923c" },
  operator_required: { label: "OPERATOR",     color: "#f87171" },
  idle:              { label: "IDLE",         color: "#94a3b8" },
};

function pickControlState(runners = []) {
  for (const r of runners) {
    const cs = r?.metadata_json?.local_factory?.control_state;
    if (cs?.available) return cs;
  }
  return null;
}

function pickFactoryStatus(runners = []) {
  for (const r of runners) {
    const lf = r?.metadata_json?.local_factory;
    if (lf?.status) return String(lf.status).toUpperCase();
  }
  return "IDLE";
}

function pickRunnerStatus(runners = []) {
  for (const r of runners) {
    if (r?.status) return r.status === "online" ? "ONLINE" : "OFFLINE";
  }
  return "OFFLINE";
}

function StatusPill({ label, color, dim = false }) {
  return (
    <span
      className="rounded px-2 py-0.5 text-[10px] font-bold tracking-[0.25em]"
      style={{
        color: dim ? `${color}aa` : color,
        border: `1px solid ${color}66`,
        backgroundColor: "#0a1228",
        whiteSpace: "nowrap",
      }}
    >
      {label}
    </span>
  );
}

export default function OverallStatusBar({ runners = [] }) {
  const cs = pickControlState(runners);
  const runnerStatus = pickRunnerStatus(runners);
  const factoryStatus = pickFactoryStatus(runners);

  if (!cs) {
    return (
      <section
        className="flex flex-wrap items-center gap-2 px-3 py-2"
        data-testid="overall-status-bar"
        style={{
          backgroundColor: "#0e1a35",
          border: "1.5px solid #0e4a3a",
          borderRadius: 6,
          fontFamily: "ui-monospace, monospace",
        }}
      >
        <span className="text-[10px] tracking-[0.3em] text-emerald-300">
          OVERALL
        </span>
        <span className="text-[11px] text-slate-500">
          control_state 가 아직 작성되지 않음 — runner heartbeat 대기 중
        </span>
      </section>
    );
  }

  const overallTone = STATUS_TONE[cs.status] || STATUS_TONE.unknown;
  const pipelineKey = cs.pipeline?.status || "idle";
  const progressTone = PROGRESS_TONE[pipelineKey] || PROGRESS_TONE.idle;
  const kernelStatus = cs.execution_kernel?.status;
  const meaningful = cs.agent_accountability?.meaningful_change;
  const ticket = cs.agent_accountability?.implementation_ticket_exists;
  const changedCount = cs.deploy?.changed_files_count ?? 0;
  const blockingAgent = cs.agent_accountability?.blocking_agent;

  return (
    <section
      className="flex flex-col gap-1.5 px-3 py-2"
      data-testid="overall-status-bar"
      style={{
        backgroundColor: "#0e1a35",
        border: `1.5px solid ${overallTone.color}66`,
        borderRadius: 6,
        fontFamily: "ui-monospace, monospace",
        boxShadow: `0 0 14px ${overallTone.color}22`,
      }}
    >
      {/* Top row: three canonical pills (Runner / Factory / Progress)
          + the unified OVERALL pill that's the spec's source of truth. */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[9px] tracking-[0.3em] text-slate-500">
          STAMPPORT FACTORY
        </span>
        <span className="text-[9px] tracking-[0.3em] text-slate-500">
          RUNNER
        </span>
        <StatusPill
          label={runnerStatus}
          color={runnerStatus === "ONLINE" ? "#34d399" : "#94a3b8"}
        />
        <span className="text-[9px] tracking-[0.3em] text-slate-500">
          FACTORY
        </span>
        <StatusPill
          label={factoryStatus}
          color={
            factoryStatus === "RUNNING" ? "#38bdf8"
            : factoryStatus === "PAUSED" ? "#fbbf24"
            : "#94a3b8"
          }
        />
        <span className="text-[9px] tracking-[0.3em] text-slate-500">
          PROGRESS
        </span>
        <StatusPill label={progressTone.label} color={progressTone.color} />
        <span className="ml-auto text-[9px] tracking-[0.3em] text-slate-500">
          OVERALL
        </span>
        <StatusPill label={overallTone.label} color={overallTone.color} />
      </div>

      {/* Reason + next action row. Only renders when there's a real
          message to show — keeps the bar one line tall on healthy
          cycles. */}
      {(cs.summary || cs.blocking_reason || cs.next_action) && (
        <div
          className="rounded px-2 py-1 text-[11px] leading-snug"
          style={{
            backgroundColor: "#0a1228",
            border: `1px solid ${overallTone.color}33`,
            color: "#cbd5e1",
          }}
        >
          {cs.diagnostic_code && cs.diagnostic_code !== "healthy" && (
            <span className="mr-2 text-[10px] tracking-widest text-slate-500">
              code:{" "}
              <span style={{ color: overallTone.color, fontWeight: 700 }}>
                {cs.diagnostic_code}
              </span>
            </span>
          )}
          {cs.failed_stage && (
            <span className="mr-2 text-[10px] tracking-widest text-slate-500">
              failed stage:{" "}
              <span style={{ color: "#f87171" }}>{cs.failed_stage}</span>
            </span>
          )}
          {cs.blocking_reason && (
            <div style={{ color: overallTone.color }}>
              {cs.blocking_reason}
            </div>
          )}
          {cs.next_action && (
            <div className="mt-0.5 text-amber-200">▶ {cs.next_action}</div>
          )}
        </div>
      )}

      {/* Compact detail row — change count + ticket + meaningful +
          continuous-stop signal. Reads at a glance whether this cycle
          actually shipped anything. */}
      <div className="flex flex-wrap items-center gap-1.5 text-[10px] tracking-widest">
        <span
          className="rounded px-1.5 py-0.5 font-bold"
          style={{
            color: changedCount > 0 ? "#34d399" : "#94a3b8",
            border: `1px solid ${changedCount > 0 ? "#34d39966" : "#1e293b"}`,
            backgroundColor: "#0a1228",
          }}
        >
          CHANGED · {changedCount}
        </span>
        <span
          className="rounded px-1.5 py-0.5 font-bold"
          style={{
            color: ticket ? "#34d399" : "#f87171",
            border: `1px solid ${ticket ? "#34d39966" : "#f8717166"}`,
            backgroundColor: "#0a1228",
          }}
        >
          {ticket ? "✓" : "✗"} TICKET
        </span>
        <span
          className="rounded px-1.5 py-0.5 font-bold"
          style={{
            color: meaningful ? "#34d399" : "#fb923c",
            border: `1px solid ${meaningful ? "#34d39966" : "#fb923c66"}`,
            backgroundColor: "#0a1228",
          }}
        >
          {meaningful ? "✓" : "✗"} MEANINGFUL
        </span>
        {kernelStatus && (
          <span
            className="rounded px-1.5 py-0.5 font-bold"
            style={{
              color:
                kernelStatus === "healthy" ? "#34d399"
                : kernelStatus === "degraded" ? "#fbbf24"
                : "#f87171",
              border: `1px solid ${
                kernelStatus === "healthy" ? "#34d39966"
                : kernelStatus === "degraded" ? "#fbbf2466"
                : "#f8717166"
              }`,
              backgroundColor: "#0a1228",
            }}
          >
            KERNEL · {kernelStatus.toUpperCase()}
          </span>
        )}
        {blockingAgent && (
          <span
            className="rounded px-1.5 py-0.5 font-bold"
            style={{
              color: "#f87171",
              border: "1px solid #f8717166",
              backgroundColor: "#1c0d12",
            }}
          >
            ▶ {blockingAgent}
          </span>
        )}
        {cs.should_stop_continuous && (
          <span
            className="rounded px-1.5 py-0.5 font-bold"
            style={{
              color: "#fbbf24",
              border: "1px solid #fbbf2466",
              backgroundColor: "#0a1228",
            }}
          >
            CONTINUOUS · STOP
          </span>
        )}
        <span className="ml-auto text-[9.5px] text-slate-500">
          {cs.updated_at ? cs.updated_at.slice(11, 19) : "—"}
        </span>
      </div>
    </section>
  );
}
