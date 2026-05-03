// Agent Accountability — operator-facing snapshot of the Agent
// Supervisor's verdict.
//
// Reads runner heartbeat metadata.local_factory.agent_accountability
// (written by runner._build_agent_accountability_meta, sourced from
// agent_supervisor.run_supervisor()). The supervisor's job is to refuse
// "succeeded" when agents only produced artifacts. This panel makes
// that refusal visible — per-agent pass/fail, the blocking_agent, the
// retry prompt, and meaningful-change evidence.

import { classifyAccountabilityFreshness } from "../utils/autopilotPhase.js";

const AGENT_LABELS = {
  planner:  "기획자",
  designer: "디자이너",
  pm:       "PM",
  frontend: "FE",
  backend:  "BE",
  ai:       "AI",
  qa:       "QA",
  deploy:   "배포",
};

const AGENT_ORDER = ["planner", "designer", "pm", "frontend", "backend", "ai", "qa", "deploy"];

const STATUS_TONE = {
  pass:    { label: "PASS",    color: "#34d399" },
  fail:    { label: "RETRY",   color: "#f87171" },
  skipped: { label: "SKIP",    color: "#94a3b8" },
};

const OVERALL_TONE = {
  pass:             { label: "PASS",          color: "#34d399" },
  retry_required:   { label: "RETRY",         color: "#fb923c" },
  planning_only:    { label: "PLANNING ONLY", color: "#a78bfa" },
  blocked:          { label: "BLOCKED",       color: "#f87171" },
  failed:           { label: "FAILED",        color: "#f87171" },
  unknown:          { label: "UNKNOWN",       color: "#64748b" },
};

function pickAccountability(runners = []) {
  for (const r of runners) {
    const aa = r?.metadata_json?.local_factory?.agent_accountability;
    if (aa) return aa;
  }
  return null;
}

function fmtScore(n) {
  if (n == null) return "—";
  return `${Math.round(Number(n) || 0)}점`;
}

function AgentRow({ name, row }) {
  const status = row?.status || "skipped";
  const tone = STATUS_TONE[status] || STATUS_TONE.skipped;
  const score = row?.score ?? 0;
  const problems = Array.isArray(row?.problems) ? row.problems : [];
  const evidence = Array.isArray(row?.evidence) ? row.evidence : [];
  const retryPrompt = row?.retry_prompt || "";
  const required = !!row?.required_retry;

  return (
    <div
      className="rounded p-2"
      style={{
        backgroundColor: "#0a1228",
        border: `1px solid ${tone.color}55`,
      }}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span
            className="rounded px-1.5 py-0.5 text-[9.5px] font-bold tracking-widest"
            style={{
              color: tone.color,
              border: `1px solid ${tone.color}66`,
              backgroundColor: "#050912",
            }}
          >
            {tone.label}
          </span>
          <span className="text-[11px] font-bold tracking-widest text-slate-200">
            {AGENT_LABELS[name] || name}
          </span>
          {required && (
            <span className="text-[9px] tracking-widest text-rose-300">
              · RETRY REQUIRED
            </span>
          )}
        </div>
        <span className="text-[10px] tracking-widest text-slate-500">
          {fmtScore(score)}
        </span>
      </div>

      {(problems.length > 0 || evidence.length > 0) && (
        <details className="mt-1 text-[10px] text-slate-500">
          <summary className="cursor-pointer hover:text-slate-300">
            세부 ({problems.length}건 problem / {evidence.length}건 evidence)
          </summary>
          {problems.length > 0 && (
            <ul className="mt-1 space-y-0.5">
              {problems.slice(0, 6).map((p, i) => (
                <li key={`p-${i}`} className="text-rose-300">· {p}</li>
              ))}
            </ul>
          )}
          {evidence.length > 0 && (
            <ul className="mt-1 space-y-0.5">
              {evidence.slice(0, 6).map((e, i) => (
                <li key={`e-${i}`} className="text-emerald-300">✓ {e}</li>
              ))}
            </ul>
          )}
        </details>
      )}

      {required && retryPrompt && (
        <div
          className="mt-1 rounded p-1.5 text-[10.5px] leading-snug"
          style={{
            backgroundColor: "#1c0d12",
            border: "1px solid #f8717155",
            color: "#fecaca",
          }}
        >
          ▶ {retryPrompt}
        </div>
      )}
    </div>
  );
}

export default function AgentAccountabilityPanel({ runners = [] }) {
  const aa = pickAccountability(runners);
  const freshness = classifyAccountabilityFreshness(aa, runners);

  // Stale gate — when the supervisor blob is from a previous cycle,
  // collapse the entire panel under a "이전 사이클 산출물" details so
  // it never paints over the current Auto Pilot status. The operator
  // can still expand it when debugging an old failure.
  if (aa && aa.available && freshness === "stale") {
    return (
      <section
        className="flex flex-col gap-2 p-3"
        data-testid="agent-accountability-panel"
        data-freshness="stale"
        style={{
          backgroundColor: "#0e1a35",
          border: "1.5px solid #1e293b",
          borderRadius: 6,
          fontFamily: "ui-monospace, monospace",
        }}
      >
        <div className="flex items-center gap-2 flex-wrap">
          <span className="inline-block h-2 w-2" style={{ backgroundColor: "#94a3b8" }} />
          <span className="text-[10px] font-bold uppercase tracking-[0.3em] text-slate-300">
            AGENT ACCOUNTABILITY
          </span>
          <span
            className="rounded-full px-2 py-0.5 text-[9.5px] font-bold tracking-widest"
            style={{
              color: "#94a3b8",
              border: "1px solid #94a3b855",
              backgroundColor: "#0a1228",
            }}
          >
            {aa.run_id && aa.run_id !== (runners?.[0]?.metadata_json?.local_factory?.autopilot?.current_run_id || "")
              ? "PREVIOUS RUN"
              : "PREVIOUS CYCLE"}
          </span>
          <span className="text-[10px] tracking-widest text-slate-500">
            cycle #{aa.cycle_id ?? "—"} · run {aa.run_id ?? "—"}
          </span>
        </div>
        <div className="text-[11px] text-slate-400">
          현재 Auto Pilot run/cycle과 다른 산출물입니다 — 이전 사이클 결과는 접혔습니다.
        </div>
        <details className="text-[11px] text-slate-400">
          <summary className="cursor-pointer hover:text-slate-200">
            이전 사이클 산출물 보기
          </summary>
          <div className="mt-2 rounded p-2" style={{ backgroundColor: "#0a1228", border: "1px dashed #1e293b" }}>
            <div>blocking_agent: {aa.blocking_agent || "—"}</div>
            <div>blocking_reason: {aa.blocking_reason || "—"}</div>
            <div>overall_status: {aa.overall_status || "—"}</div>
            <div>evaluated_at: {aa.evaluated_at || "—"}</div>
            {aa.next_action && <div className="mt-1 text-amber-200">▶ {aa.next_action}</div>}
          </div>
        </details>
      </section>
    );
  }

  if (!aa || !aa.available) {
    return (
      <section
        className="flex flex-col gap-2 p-3"
        data-testid="agent-accountability-panel"
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
            AGENT ACCOUNTABILITY
          </span>
        </div>
        <div className="text-[11px] text-slate-500">
          Agent Supervisor 가 아직 실행되지 않았습니다 — runner heartbeat / cycle 종료 후 결과가 표시됩니다.
        </div>
      </section>
    );
  }

  const overallKey = aa.overall_status || "unknown";
  const overallTone = OVERALL_TONE[overallKey] || OVERALL_TONE.unknown;
  const blocking = aa.blocking_agent;
  const blockingLabel = blocking ? (AGENT_LABELS[blocking] || blocking) : null;
  const ticketOk = !!aa.implementation_ticket_exists;
  const meaningful = !!aa.meaningful_change;
  const screens = Array.isArray(aa.affected_screens) ? aa.affected_screens : [];
  const flows = Array.isArray(aa.affected_flows) ? aa.affected_flows : [];
  const qaScenarios = Array.isArray(aa.qa_scenarios) ? aa.qa_scenarios : [];

  // Friendly headline copy that mirrors the spec's prescriptive
  // messages ("PM이 Implementation Ticket을 만들지 않아 ..." etc.).
  let headline;
  if (overallKey === "pass") {
    headline = "모든 에이전트 산출물이 기준을 통과했고 실제 제품 변경이 발생했습니다.";
  } else if (!ticketOk && (blocking === "pm" || blocking === "implementation_ticket")) {
    headline = "PM이 Implementation Ticket을 만들지 않아 개발 단계가 차단되었습니다.";
  } else if (blocking === "designer") {
    headline = "디자이너 산출물이 추상적이라 FE 구현 지시로 사용할 수 없습니다.";
  } else if (blocking === "frontend") {
    headline = "FE가 의미 있는 화면 변경을 만들지 않아 재작업이 필요합니다.";
  } else if (overallKey === "planning_only") {
    headline = "기획/디자인 산출물은 있으나 실제 코드 변경으로 이어지지 않았습니다.";
  } else if (blocking) {
    headline = `${blockingLabel} 단계에서 막혔습니다 — ${aa.blocking_reason || "기준 미달"}`;
  } else {
    headline = aa.blocking_reason || "supervisor 결과를 확인하세요.";
  }

  return (
    <section
      className="flex flex-col gap-2 p-3"
      data-testid="agent-accountability-panel"
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
            style={{ backgroundColor: overallTone.color }}
          />
          <span className="text-[10px] font-bold uppercase tracking-[0.3em] text-emerald-300">
            AGENT ACCOUNTABILITY
          </span>
          <span
            className="rounded px-1.5 py-0.5 text-[9px] font-bold tracking-widest"
            style={{
              color: overallTone.color,
              border: `1px solid ${overallTone.color}66`,
              backgroundColor: "#0a1228",
            }}
          >
            {overallTone.label}
          </span>
          {blockingLabel && (
            <span
              className="rounded px-1.5 py-0.5 text-[9px] font-bold tracking-widest"
              style={{
                color: "#f87171",
                border: "1px solid #f8717166",
                backgroundColor: "#1c0d12",
              }}
            >
              ▶ {blockingLabel}
            </span>
          )}
        </div>
        <span className="text-[10px] tracking-widest text-slate-500">
          cycle #{aa.cycle_id ?? "—"}
        </span>
      </div>

      <div
        className="rounded p-2 text-[12px] leading-snug"
        style={{
          backgroundColor: "#0a1228",
          color: "#f5e9d3",
          border: `1px solid ${overallTone.color}55`,
        }}
      >
        {headline}
        {aa.next_action && (
          <div className="mt-1 text-[11px] text-amber-200">▶ {aa.next_action}</div>
        )}
      </div>

      {/* Meaningful change + ticket flags */}
      <div className="flex flex-wrap items-center gap-1.5 text-[10px] tracking-widest">
        <span
          className="rounded px-1.5 py-0.5 font-bold"
          style={{
            color: ticketOk ? "#34d399" : "#f87171",
            border: `1px solid ${ticketOk ? "#34d39966" : "#f8717166"}`,
            backgroundColor: "#0a1228",
          }}
        >
          {ticketOk ? "✓" : "✗"} TICKET
        </span>
        <span
          className="rounded px-1.5 py-0.5 font-bold"
          style={{
            color: meaningful ? "#34d399" : "#fb923c",
            border: `1px solid ${meaningful ? "#34d39966" : "#fb923c66"}`,
            backgroundColor: "#0a1228",
          }}
        >
          {meaningful ? "✓" : "✗"} MEANINGFUL CHANGE
        </span>
        {aa.commit_hash && (
          <span
            className="rounded px-1.5 py-0.5 font-bold"
            style={{
              color: "#d4a843",
              border: "1px solid #d4a84366",
              backgroundColor: "#0a1228",
            }}
          >
            {String(aa.commit_hash).slice(0, 8)}
          </span>
        )}
      </div>

      {/* Affected screens / flows / QA */}
      {(screens.length > 0 || flows.length > 0 || qaScenarios.length > 0) && (
        <div className="rounded p-2 text-[10.5px] leading-snug"
          style={{ backgroundColor: "#0a1228", border: "1px solid #1e293b" }}>
          {screens.length > 0 && (
            <div className="text-slate-300">
              <span className="text-slate-500">screens:</span>{" "}
              {screens.join(", ")}
            </div>
          )}
          {flows.length > 0 && (
            <div className="text-slate-300">
              <span className="text-slate-500">flows:</span>{" "}
              {flows.join(", ")}
            </div>
          )}
          {qaScenarios.length > 0 && (
            <div className="text-slate-300">
              <span className="text-slate-500">QA:</span>{" "}
              {qaScenarios.slice(0, 3).join(" · ")}
            </div>
          )}
        </div>
      )}

      {/* Per-agent grid */}
      <div className="grid grid-cols-1 gap-1.5">
        {AGENT_ORDER.map((name) => {
          const row = (aa.agents && aa.agents[name]) || null;
          if (!row) return null;
          return <AgentRow key={name} name={name} row={row} />;
        })}
      </div>
    </section>
  );
}
