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
