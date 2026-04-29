// Pipeline Recovery Orchestrator — operator-facing panel.
//
// Reads runner heartbeat metadata.local_factory.pipeline_recovery
// (written by runner._build_pipeline_recovery_meta) and answers the
// single question the operator wants to know after a missed shift:
//
//   "어디서 막혔고, 지금 Watchdog 이 무엇을 하려고 하나?"
//
// Compact: one row of stage chips (planner → designer → ... → deploy)
// where the last successful stage shines and the failed stage flashes,
// then a recovery card with diagnostic_code / next_action /
// operator_required / retry counters / recovery history (last 5).

const STAGE_LABELS = {
  planner_proposal:           "기획",
  designer_review:            "디자인",
  pm_decision:                "PM",
  implementation_ticket:      "Ticket",
  claude_apply:               "Apply",
  validation_qa:              "QA",
  git_commit:                 "Commit",
  git_push:                   "Push",
  github_actions:             "Actions",
  server_verification:        "Server",
  browser_cache_verification: "Browser",
};

const SEVERITY_COLOR = {
  info:    "#94a3b8",
  warning: "#fbbf24",
  error:   "#f87171",
};

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString([], { hour12: false });
  } catch {
    return String(iso).slice(0, 19);
  }
}

function pickPipeline(runners = []) {
  for (const r of runners) {
    const pr = r?.metadata_json?.local_factory?.pipeline_recovery;
    if (pr) return pr;
  }
  return null;
}

function pickForwardProgress(runners = []) {
  for (const r of runners) {
    const fp = r?.metadata_json?.local_factory?.forward_progress;
    if (fp) return fp;
  }
  return null;
}

function pickFactoryStatus(runners = []) {
  for (const r of runners) {
    const lf = r?.metadata_json?.local_factory;
    if (lf) return { factoryStatus: lf.status, alive: !!lf.alive };
  }
  return { factoryStatus: null, alive: false };
}

function pickRunnerOnline(runners = []) {
  for (const r of runners) {
    if (r?.status) return r.status;
  }
  return "offline";
}

const PROGRESS_TONE = {
  progressing:        { label: "PROGRESSING",    color: "#34d399" },
  blocked:            { label: "BLOCKED",        color: "#fb923c" },
  stuck:              { label: "STUCK",          color: "#f87171" },
  planning_only:      { label: "PLANNING ONLY",  color: "#a78bfa" },
  no_progress:        { label: "NO PROGRESS",    color: "#fb923c" },
  operator_required:  { label: "OPERATOR",       color: "#f87171" },
};

function fmtElapsed(sec) {
  if (sec == null) return "—";
  const s = Number(sec) || 0;
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s/60)}m ${s%60}s`;
  return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m`;
}

function StageChip({ stage, kind }) {
  // kind: "ok" | "current" | "failed" | "todo"
  const palette = {
    ok:      { color: "#34d399", border: "#34d39966", bg: "#0a1228" },
    current: { color: "#38bdf8", border: "#38bdf899", bg: "#0a1228" },
    failed:  { color: "#f87171", border: "#f8717188", bg: "#1c0d12" },
    todo:    { color: "#475569", border: "#1e293b",   bg: "#0a1228" },
  };
  const p = palette[kind] || palette.todo;
  const label = STAGE_LABELS[stage] || stage;
  const symbol = kind === "ok" ? "✓"
    : kind === "current" ? "▸"
    : kind === "failed" ? "✗"
    : "·";
  return (
    <span
      className="rounded px-1.5 py-0.5 text-[9.5px] font-bold tracking-widest"
      style={{
        color: p.color,
        border: `1px solid ${p.border}`,
        backgroundColor: p.bg,
        whiteSpace: "nowrap",
      }}
    >
      {symbol} {label}
    </span>
  );
}

export default function PipelineRecoveryPanel({ runners = [] }) {
  const pr = pickPipeline(runners);
  const fp = pickForwardProgress(runners);
  const { factoryStatus } = pickFactoryStatus(runners);
  const runnerOnline = pickRunnerOnline(runners);
  const liveness = runnerOnline === "online" ? "ONLINE" : "OFFLINE";
  const factoryLabel = (factoryStatus || "idle").toUpperCase();
  const progressKey = fp?.status || "progressing";
  const progressTone = PROGRESS_TONE[progressKey] || PROGRESS_TONE.progressing;

  if (!pr) {
    return (
      <section
        className="flex flex-col gap-2 p-3"
        data-testid="pipeline-recovery-panel"
        style={{
          backgroundColor: "#0e1a35",
          border: "1.5px solid #0e4a3a",
          borderRadius: 6,
          fontFamily: "ui-monospace, monospace",
        }}
      >
        <div className="flex items-center gap-2">
          <span className="inline-block h-2 w-2" style={{ backgroundColor: "#64748b" }} />
          <span className="text-[10px] font-bold uppercase tracking-[0.3em] text-emerald-300">
            PIPELINE RECOVERY
          </span>
        </div>
        <div className="text-[11px] text-slate-500">
          runner heartbeat 대기 중 — Pipeline Orchestrator 가 첫 tick 을 보내면 표시됩니다.
        </div>
      </section>
    );
  }

  const stages = Array.isArray(pr.stage_order) && pr.stage_order.length > 0
    ? pr.stage_order
    : Object.keys(STAGE_LABELS);
  const lastSuccessIdx = stages.indexOf(pr.last_success_stage);
  const failedIdx = stages.indexOf(pr.failed_stage);
  const currentIdx = stages.indexOf(pr.current_stage);

  const code = pr.diagnostic_code || "healthy";
  const severity = pr.severity || "info";
  const sevColor = SEVERITY_COLOR[severity] || "#94a3b8";
  const operatorRequired = !!pr.operator_required;
  const headlineColor = operatorRequired ? "#f87171" : (code === "healthy" ? "#34d399" : sevColor);
  const retryMap = pr.retry_count_by_stage || {};
  const recoveryHistory = Array.isArray(pr.recovery_history) ? pr.recovery_history : [];
  const lastDecision = pr.last_decision || null;
  const lastResult = pr.last_result || null;
  const claudeRepairAllowed = !!pr.claude_repair_allowed;

  return (
    <section
      className="flex flex-col gap-2 p-3"
      data-testid="pipeline-recovery-panel"
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
            style={{ backgroundColor: headlineColor }}
          />
          <span className="text-[10px] font-bold uppercase tracking-[0.3em] text-emerald-300">
            PIPELINE RECOVERY
          </span>
          <span
            className="rounded px-1.5 py-0.5 text-[9px] font-bold tracking-widest"
            style={{
              color: headlineColor,
              border: `1px solid ${headlineColor}66`,
              backgroundColor: "#0a1228",
            }}
          >
            {operatorRequired ? "OPERATOR" : (code === "healthy" ? "HEALTHY" : "WORKING")}
          </span>
          <span
            className="rounded px-1.5 py-0.5 text-[9px] font-bold tracking-widest"
            style={{
              color: claudeRepairAllowed ? "#facc15" : "#64748b",
              border: `1px solid ${claudeRepairAllowed ? "#facc1566" : "#64748b66"}`,
              backgroundColor: "#0a1228",
            }}
          >
            CLAUDE REPAIR {claudeRepairAllowed ? "ON" : "OFF"}
          </span>
        </div>
        <span className="text-[10px] tracking-widest text-slate-500">
          cycle #{pr.cycle_id ?? "—"}
        </span>
      </div>

      {/* Liveness vs Factory vs Progress — three independent dimensions */}
      <div className="grid grid-cols-3 gap-2">
        <div
          className="rounded p-1.5 text-center"
          style={{ backgroundColor: "#0a1228", border: "1px solid #1e293b" }}
        >
          <div className="text-[8.5px] uppercase tracking-[0.3em] text-slate-500">
            RUNNER
          </div>
          <div
            className="text-[11.5px] font-bold tracking-widest"
            style={{ color: liveness === "ONLINE" ? "#34d399" : "#94a3b8" }}
          >
            {liveness}
          </div>
        </div>
        <div
          className="rounded p-1.5 text-center"
          style={{ backgroundColor: "#0a1228", border: "1px solid #1e293b" }}
        >
          <div className="text-[8.5px] uppercase tracking-[0.3em] text-slate-500">
            FACTORY
          </div>
          <div
            className="text-[11.5px] font-bold tracking-widest"
            style={{
              color: factoryLabel === "RUNNING" ? "#38bdf8"
                : factoryLabel === "PAUSED" ? "#fbbf24"
                : "#94a3b8",
            }}
          >
            {factoryLabel}
          </div>
        </div>
        <div
          className="rounded p-1.5 text-center"
          style={{
            backgroundColor: "#0a1228",
            border: `1px solid ${progressTone.color}66`,
          }}
        >
          <div className="text-[8.5px] uppercase tracking-[0.3em] text-slate-500">
            PROGRESS
          </div>
          <div
            className="text-[11.5px] font-bold tracking-widest"
            style={{ color: progressTone.color }}
          >
            {progressTone.label}
          </div>
        </div>
      </div>

      {/* Forward Progress detail card */}
      {fp && (
        <div
          className="rounded p-2 text-[11px] leading-snug"
          style={{
            backgroundColor: "#0a1228",
            border: `1px solid ${progressTone.color}55`,
            color: "#cbd5e1",
          }}
        >
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[10.5px]">
            <span className="text-[10px] uppercase tracking-[0.3em] text-slate-500">
              stage
            </span>
            <span className="text-slate-200">{fp.current_stage || "—"}</span>
            <span className="text-slate-500">·</span>
            <span className="text-[10px] uppercase tracking-[0.3em] text-slate-500">
              elapsed
            </span>
            <span
              style={{
                color:
                  (fp.current_stage_elapsed_sec || 0) > (fp.stage_timeout_sec || 0)
                    ? "#f87171" : "#cbd5e1",
              }}
            >
              {fmtElapsed(fp.current_stage_elapsed_sec)} /{" "}
              {fmtElapsed(fp.stage_timeout_sec)}
            </span>
            <span className="text-slate-500">·</span>
            <span className="text-[10px] uppercase tracking-[0.3em] text-slate-500">
              code
            </span>
            <span
              style={{
                color: fp.changed_files_count > 0 ? "#34d399" : "#94a3b8",
                fontWeight: 700,
              }}
            >
              {fp.changed_files_count}건
            </span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10.5px]">
            <span className="text-[10px] uppercase tracking-[0.3em] text-slate-500">
              required
            </span>
            <span style={{ color: fp.required_output_exists ? "#34d399" : "#f87171" }}>
              {fp.required_output_exists ? "✓" : "✗"} {fp.required_output || "—"}
            </span>
          </div>
          {fp.blocking_reason && (
            <div className="mt-1 text-[11px]" style={{ color: progressTone.color }}>
              {fp.blocking_reason}
            </div>
          )}
          {fp.next_action && (
            <div className="mt-1 text-[11.5px] text-amber-200">
              ▶ {fp.next_action}
            </div>
          )}
          {/* Motion timestamps row */}
          <div className="mt-1 grid grid-cols-3 gap-1 text-[9.5px] tracking-widest text-slate-500">
            <div>
              code:{" "}
              <span className="text-slate-300">
                {fp.last_code_changed_at ? fmtTime(fp.last_code_changed_at) : "—"}
              </span>
            </div>
            <div>
              commit:{" "}
              <span className="text-slate-300">
                {fp.last_commit_at ? fmtTime(fp.last_commit_at) : "—"}
              </span>
            </div>
            <div>
              push:{" "}
              <span className="text-slate-300">
                {fp.last_push_at ? fmtTime(fp.last_push_at) : "—"}
              </span>
            </div>
          </div>
        </div>
      )}

      {/* Stage chip strip */}
      <div className="-mx-1 flex flex-wrap gap-1 px-1">
        {stages.map((stage, idx) => {
          let kind = "todo";
          if (failedIdx >= 0 && idx === failedIdx) kind = "failed";
          else if (lastSuccessIdx >= 0 && idx <= lastSuccessIdx) kind = "ok";
          else if (currentIdx >= 0 && idx === currentIdx && failedIdx < 0) kind = "current";
          return <StageChip key={stage} stage={stage} kind={kind} />;
        })}
      </div>

      {/* Diagnostic + next_action card */}
      <div
        className="rounded p-2 text-[11px] leading-snug"
        style={{
          backgroundColor: "#0a1228",
          border: `1px solid ${headlineColor}55`,
          color: "#cbd5e1",
        }}
      >
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-[10px] uppercase tracking-[0.3em] text-slate-500">
            code
          </span>
          <span style={{ color: sevColor, fontWeight: 700 }}>{code}</span>
          {pr.failed_stage && (
            <>
              <span className="text-[10px] uppercase tracking-[0.3em] text-slate-500">
                failed
              </span>
              <span style={{ color: "#f87171" }}>{pr.failed_stage}</span>
            </>
          )}
          {pr.last_success_stage && (
            <>
              <span className="text-[10px] uppercase tracking-[0.3em] text-slate-500">
                last ok
              </span>
              <span style={{ color: "#34d399" }}>{pr.last_success_stage}</span>
            </>
          )}
        </div>
        {pr.root_cause && (
          <div className="mt-1 text-[11px]" style={{ color: sevColor }}>
            {pr.root_cause}
          </div>
        )}
        {pr.next_action && (
          <div className="mt-1 text-[11.5px] text-amber-200">
            ▶ {pr.next_action}
          </div>
        )}
      </div>

      {/* Last applied / skipped actions */}
      {(lastResult?.applied?.length > 0 || lastResult?.skipped?.length > 0) && (
        <details className="text-[10px] text-slate-500" open>
          <summary className="cursor-pointer text-slate-400 hover:text-slate-200">
            last actions
          </summary>
          <ul className="mt-1 space-y-0.5">
            {(lastResult?.applied || []).map((line, i) => (
              <li key={`a-${i}`} className="text-emerald-300">
                ✓ {line}
              </li>
            ))}
            {(lastResult?.skipped || []).map((line, i) => (
              <li key={`s-${i}`} className="text-amber-200">
                · {line}
              </li>
            ))}
          </ul>
        </details>
      )}

      {/* Recovery history */}
      {recoveryHistory.length > 0 && (
        <details className="text-[10px] text-slate-500">
          <summary className="cursor-pointer text-slate-400 hover:text-slate-200">
            recovery history (최근 {Math.min(5, recoveryHistory.length)}건)
          </summary>
          <ul className="mt-1 space-y-0.5">
            {recoveryHistory.slice(-5).reverse().map((h, i) => (
              <li key={i} className="text-slate-400">
                <span className="text-slate-600">{fmtTime(h.at)}</span>{" "}
                <span style={{ color: SEVERITY_COLOR[h.result === "success" ? "info" : "warning"] }}>
                  {h.diagnostic_code}
                </span>
                {h.repair_action && (
                  <>
                    {" "}→ <span className="text-slate-300">{h.repair_action}</span>
                  </>
                )}
                {" "}<span className="text-slate-500">({h.result || "—"})</span>
                {h.next_stage && (
                  <span className="text-slate-500"> · rollback→{h.next_stage}</span>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}

      {/* Retry counters */}
      {Object.keys(retryMap).length > 0 && (
        <div className="text-[10px] tracking-wider text-slate-500">
          retry:{" "}
          {Object.entries(retryMap).map(([stage, count], i, arr) => (
            <span key={stage}>
              <span className="text-slate-300">{STAGE_LABELS[stage] || stage}</span>
              =<span className="text-amber-200">{count}</span>
              {i < arr.length - 1 ? " · " : ""}
            </span>
          ))}
        </div>
      )}

      {!claudeRepairAllowed && (
        <div className="text-[10px] text-slate-500">
          Claude Repair 활성화:{" "}
          <span className="text-emerald-300">FACTORY_WATCHDOG_ALLOW_CLAUDE_REPAIR=true</span>{" "}
          + <span className="text-emerald-300">FACTORY_WATCHDOG_ENABLED=true</span>
        </div>
      )}
    </section>
  );
}
