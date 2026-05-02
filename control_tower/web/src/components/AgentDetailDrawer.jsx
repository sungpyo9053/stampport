import { useEffect, useMemo } from "react";
import { buildSystemLogEntries } from "../utils/eventClassifier.js";
import {
  FRESHNESS_LABEL,
  derivePhase,
  freshnessOf,
  hasActiveCycle,
  isRunningPhase,
  pickRunnerMeta,
  stageToWorkingAgent,
} from "../utils/autopilotPhase.js";

// AgentDetailDrawer — slide-in detail panel for a single agent.
//
// Sources of truth (priority order):
//   1. autopilot_state            (heartbeat .autopilot)
//   2. factory_smoke_state.json   (heartbeat .factory_smoke / .smoke)
//   3. factory_state.json         (heartbeat .factory_state)
//   4. agent_accountability       (heartbeat .agent_accountability)
//   5. system log (events)        (heartbeat-derived event_bus rows)
//   6. fallback static role map
//
// Required sections (per spec):
//   - 현재 역할
//   - 현재 작업
//   - 마지막 명령
//   - 최근 로그 5개
//   - 실패 원인
//   - 다음 액션
//   - 관련 파일 변경 목록

const ROLE_MAP = {
  pm:       { label: "PM",       role: "프로덕트 매니저",     accent: "#d4a843" },
  planner:  { label: "Planner",  role: "기획자",              accent: "#7dd3fc" },
  designer: { label: "Designer", role: "디자이너",            accent: "#f472b6" },
  frontend: { label: "Frontend", role: "FE 엔지니어",         accent: "#38bdf8" },
  backend:  { label: "Backend",  role: "BE 엔지니어",         accent: "#34d399" },
  ai:       { label: "AI",       role: "AI Architect",        accent: "#a78bfa" },
  qa:       { label: "QA",       role: "QA 엔지니어",         accent: "#fb923c" },
  deploy:   { label: "Deploy",   role: "배포 담당",           accent: "#facc15" },
};

// Path roots that "belong" to each engineering agent.
const AGENT_FILE_ROOTS = {
  frontend: ["app/web/", "control_tower/web/"],
  backend:  ["app/api/", "control_tower/local_runner/", "control_tower/api/"],
  ai:       ["app/ai/", "agents/", "kick_point", "ai_"],
};

// agent_id → keywords used to filter system-log entries by agent.
const AGENT_LOG_KEYWORDS = {
  pm:       [/pm[_\s]decision/i, /implementation\s+ticket/i, /pm\s+/i, /scope/i],
  planner:  [/planner/i, /기획자/i, /planner_proposal/i, /planner_revision/i],
  designer: [/designer/i, /디자이너/i, /design_spec/i],
  frontend: [/frontend/i, /\bfe\b/i, /app\/web/i, /vite|tailwind|jsx|tsx/i],
  backend:  [/backend/i, /\bbe\b/i, /app\/api/i, /fastapi|sqlalchemy/i],
  ai:       [/\bai\b/i, /kick[_\s-]point/i, /agent_/i, /architect/i],
  qa:       [/\bqa\b/i, /render\s+smoke/i, /scope[_\s]gate/i, /validation/i],
  deploy:   [/deploy/i, /push|pushed|pushing/i, /github\s+actions/i, /healthcheck/i],
};

function pickMeta(runners = []) {
  for (const r of runners) {
    const lf = r?.metadata_json?.local_factory;
    if (lf) return lf;
  }
  return {};
}

function fileMatchesAgent(path, agentId) {
  const roots = AGENT_FILE_ROOTS[agentId];
  if (!roots) return null;
  return roots.some((root) => {
    if (root.endsWith("/")) return path.startsWith(root);
    return path.includes(root);
  });
}

function deriveCurrentTask(agentId, meta, ctx) {
  const aa = meta?.agent_accountability || {};
  const fs = meta?.factory_state || {};
  const ap = meta?.autopilot || {};
  const pr = meta?.pipeline_recovery || {};
  const { phase, workingAgentId } = ctx;

  // Honest answer first: if autopilot isn't running, this agent has
  // no current task. Don't grasp at stale fields.
  if (!isRunningPhase(phase)) {
    return { text: "대기 중 (Auto Pilot 정지됨)", source: "autopilot_state" };
  }
  // No active cycle at all → loop is between cycles.
  if (!hasActiveCycle(meta)) {
    return { text: "다음 사이클 시작 대기 중", source: "autopilot_state" };
  }
  // Active stage owns me?
  const stage = pr.current_stage || fs.current_stage;
  if (stage && workingAgentId === agentId) {
    return { text: `현재 stage: ${stage} (이 에이전트 작업 중)`, source: "factory_state" };
  }
  if (stage && workingAgentId && workingAgentId !== agentId) {
    return {
      text: `대기 중 — 현재 stage ${stage} (${workingAgentId.toUpperCase()} 작업 중)`,
      source: "factory_state",
    };
  }
  if (stage) {
    return { text: `현재 stage: ${stage}`, source: "factory_state" };
  }
  return { text: "대기 중", source: "autopilot_state" };
}

function deriveFailureReason(agentId, meta, ctx) {
  const aa = meta?.agent_accountability || {};
  const acc = (aa.agents || {})[agentId] || {};
  const isFresh = ctx.freshness === "current_run" || ctx.freshness === "current_cycle";

  // Only render a "current failure" when the data is fresh. Stale
  // problems get bumped to the previousIssue derivation below.
  if (isFresh) {
    if (Array.isArray(acc.problems) && acc.problems.length > 0) {
      return { text: acc.problems.join(" / "), source: "agent_accountability" };
    }
    if (aa.blocking_agent === agentId && aa.blocking_reason) {
      return { text: aa.blocking_reason, source: "agent_accountability" };
    }
    if (agentId === "qa") {
      const qa = meta?.qa_gate || {};
      if (qa.qa_status === "failed") {
        return { text: qa.qa_failed_reason || "QA failed", source: "factory_state" };
      }
    }
    if (agentId === "deploy") {
      const pub = meta?.publish || {};
      if (pub.last_push_status === "failed") {
        return { text: pub.last_push_reason || "push failed", source: "publish" };
      }
    }
    const smoke = meta?.factory_smoke || meta?.smoke || {};
    if (smoke.failure_code) {
      return { text: `smoke failure_code: ${smoke.failure_code}`, source: "factory_smoke_state" };
    }
  }
  return null;
}

function derivePreviousIssue(agentId, meta, ctx) {
  // Surface a prior failure/problem WITHOUT painting it as current.
  // This is what fills the "이전 cycle 미해결" collapsible.
  if (ctx.freshness === "current_run" || ctx.freshness === "current_cycle") return null;
  const aa = meta?.agent_accountability || {};
  const acc = (aa.agents || {})[agentId] || {};
  if (Array.isArray(acc.problems) && acc.problems.length > 0) {
    return {
      text: acc.problems.join(" / "),
      source: "agent_accountability (stale)",
    };
  }
  if (aa.blocking_agent === agentId && aa.blocking_reason) {
    return { text: aa.blocking_reason, source: "agent_accountability (stale)" };
  }
  return null;
}

function deriveNextAction(agentId, meta, ctx) {
  const aa = meta?.agent_accountability || {};
  const acc = (aa.agents || {})[agentId] || {};
  const isFresh = ctx.freshness === "current_run" || ctx.freshness === "current_cycle";
  if (isFresh && acc.retry_prompt) {
    return { text: acc.retry_prompt, source: "agent_accountability" };
  }
  if (isFresh && aa.blocking_agent === agentId && aa.next_action) {
    return { text: aa.next_action, source: "agent_accountability" };
  }
  const fs = meta?.factory_state || {};
  if (agentId === "pm" && fs.claude_rework_prompt) {
    return { text: fs.claude_rework_prompt, source: "factory_state" };
  }
  if (fs.claude_repair_prompt) {
    return { text: fs.claude_repair_prompt, source: "factory_state" };
  }
  const ap = meta?.autopilot || {};
  if (ap.stop_reason) {
    return { text: `정지 사유: ${ap.stop_reason}`, source: "autopilot_state" };
  }
  return { text: "다음 cycle 대기", source: "fallback" };
}

function deriveChangedFiles(agentId, meta) {
  const fs = meta?.factory_state || {};
  const aa = meta?.agent_accountability || {};
  const all = []
    .concat(Array.isArray(fs.claude_apply_changed_files) ? fs.claude_apply_changed_files : [])
    .concat(Array.isArray(fs.implementation_ticket_target_files) ? fs.implementation_ticket_target_files : [])
    .concat(Array.isArray(aa.changed_files) ? aa.changed_files : []);
  const dedup = Array.from(new Set(all.filter(Boolean).map(String)));

  const roots = AGENT_FILE_ROOTS[agentId];
  if (roots) {
    const filtered = dedup.filter((p) => fileMatchesAgent(p, agentId));
    if (filtered.length > 0) return filtered.slice(0, 12);
  }
  // Plan-side / QA / Deploy don't own a file root — show artifact docs.
  const PLAN_ARTIFACT_MAP = {
    pm: ["pm_decision.md", "implementation_ticket.md"],
    planner: ["planner_proposal.md", "planner_revision.md"],
    designer: ["designer_critique.md", "designer_final_review.md", "design_spec.md"],
    qa: ["qa_report.md", "qa_diagnostics.json"],
    deploy: ["autopilot_report.md"],
  };
  const arts = PLAN_ARTIFACT_MAP[agentId];
  if (arts) {
    return arts.filter((p) =>
      dedup.some((d) => d.endsWith(p)) || true,
    );
  }
  if (dedup.length === 0) return [];
  return dedup.slice(0, 12);
}

function findRecentLogs(agentId, events) {
  const entries = buildSystemLogEntries(events);
  const keys = AGENT_LOG_KEYWORDS[agentId] || [];
  const matches = entries.filter((e) => {
    const msg = e.ev?.message || "";
    return keys.some((kw) => kw.test(msg));
  });
  return matches.slice(0, 5);
}

function findLastCommand(agentId, events) {
  const entries = buildSystemLogEntries(events);
  const keys = AGENT_LOG_KEYWORDS[agentId] || [];
  const m = entries.find((e) => {
    if (e.category !== "Command" && e.category !== "Claude") return false;
    const msg = e.ev?.message || "";
    return keys.length === 0 || keys.some((kw) => kw.test(msg));
  });
  return m || null;
}

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString([], { hour12: false });
  } catch {
    return String(iso).slice(0, 19);
  }
}

function Section({ title, source, children }) {
  return (
    <div className="agent-detail-section flex flex-col gap-1.5">
      <div className="flex items-center gap-2 flex-wrap">
        <h4 className="text-[10px] font-bold uppercase tracking-[0.3em] text-amber-300">
          {title}
        </h4>
        {source && (
          <span className="agent-detail-source" data-testid="agent-detail-source">
            source: {source}
          </span>
        )}
      </div>
      <div className="text-[12px] leading-snug text-slate-200">{children}</div>
    </div>
  );
}

function LogRow({ entry }) {
  const sevColor = {
    info:    "#94a3b8",
    success: "#34d399",
    warn:    "#fbbf24",
    error:   "#f87171",
  }[entry.severity] || "#94a3b8";
  return (
    <li
      className="flex flex-col gap-0.5 rounded px-2 py-1.5 text-[11px]"
      style={{ backgroundColor: "#0a1228", border: `1px solid ${sevColor}33` }}
    >
      <div className="flex items-center gap-2 text-[9.5px] tracking-widest text-slate-500">
        <span
          className="inline-block h-1.5 w-1.5 rounded-full"
          style={{ backgroundColor: sevColor }}
        />
        <span>{fmtTime(entry.ev?.created_at)}</span>
        <span className="font-bold" style={{ color: sevColor }}>
          {entry.category}
        </span>
        <span className="ml-auto text-slate-600">#{entry.ev?.id}</span>
      </div>
      <span className="text-[11.5px] text-slate-200">
        {entry.ev?.message || "(no message)"}
      </span>
    </li>
  );
}

export default function AgentDetailDrawer({
  agentId,
  runners = [],
  events = [],
  onClose,
}) {
  const meta = useMemo(() => pickMeta(runners), [runners]);

  const role = ROLE_MAP[agentId] || {
    label: String(agentId || ""),
    role: "에이전트",
    accent: "#cbd5e1",
  };

  // Body-scroll lock while drawer open.
  useEffect(() => {
    if (!agentId) return undefined;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [agentId]);

  const phase = useMemo(() => derivePhase(meta), [meta]);
  const workingAgentId = useMemo(() => stageToWorkingAgent(meta, phase), [meta, phase]);
  const freshness = useMemo(
    () => freshnessOf({
      artifactCycleId: meta?.agent_accountability?.cycle_id,
      artifactAt: meta?.agent_accountability?.evaluated_at,
      autopilot: meta?.autopilot,
    }),
    [meta],
  );
  const ctx = { phase, workingAgentId, freshness };

  const currentTask = useMemo(
    () => (agentId ? deriveCurrentTask(agentId, meta, ctx) : null),
    [agentId, meta, phase, workingAgentId, freshness],
  );
  const failureReason = useMemo(
    () => (agentId ? deriveFailureReason(agentId, meta, ctx) : null),
    [agentId, meta, phase, workingAgentId, freshness],
  );
  const previousIssue = useMemo(
    () => (agentId ? derivePreviousIssue(agentId, meta, ctx) : null),
    [agentId, meta, phase, workingAgentId, freshness],
  );
  const nextAction = useMemo(
    () => (agentId ? deriveNextAction(agentId, meta, ctx) : null),
    [agentId, meta, phase, workingAgentId, freshness],
  );
  const changedFiles = useMemo(
    () => (agentId ? deriveChangedFiles(agentId, meta) : []),
    [agentId, meta],
  );
  const recentLogs = useMemo(
    () => (agentId ? findRecentLogs(agentId, events) : []),
    [agentId, events],
  );
  const lastCommand = useMemo(
    () => (agentId ? findLastCommand(agentId, events) : null),
    [agentId, events],
  );

  const freshnessMeta = FRESHNESS_LABEL[freshness] || FRESHNESS_LABEL.unknown;

  if (!agentId) return null;

  return (
    <div
      className="agent-detail-drawer agent-detail-overlay fixed inset-0 z-[80] flex justify-end"
      data-testid="agent-detail-drawer"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={`${role.label} 상세`}
      style={{ backgroundColor: "rgba(5, 9, 18, 0.7)" }}
    >
      <aside
        className="agent-detail-drawer-panel agent-detail-panel relative flex h-full w-full max-w-md flex-col gap-3 overflow-y-auto p-4 sm:p-5"
        onClick={(e) => e.stopPropagation()}
        style={{
          background:
            "linear-gradient(180deg, #15264a 0%, #0e1a35 60%, #050912 100%)",
          borderLeft: `1.5px solid ${role.accent}66`,
          fontFamily: "ui-monospace, monospace",
          boxShadow: `-8px 0 40px ${role.accent}33`,
        }}
      >
        {/* Header */}
        <header className="flex items-start justify-between gap-2 flex-wrap">
          <div className="flex items-center gap-2">
            <span
              className="grid h-10 w-10 place-items-center rounded-full text-lg"
              style={{
                background: `conic-gradient(from 0deg, ${role.accent}, #d4a843, ${role.accent})`,
                padding: 2,
              }}
            >
              <span
                className="grid h-full w-full place-items-center rounded-full text-lg"
                style={{
                  backgroundColor: "#0a1228",
                  color: role.accent,
                }}
              >
                {{
                  pm: "🧭", planner: "📐", designer: "🎨",
                  frontend: "💻", backend: "🛠️", ai: "🧠",
                  qa: "🔍", deploy: "🚀",
                }[agentId] || "👤"}
              </span>
            </span>
            <div className="leading-tight">
              <div
                className="text-[14px] font-bold tracking-widest"
                style={{ color: role.accent }}
              >
                {role.label}
              </div>
              <div className="text-[10px] tracking-[0.25em] text-slate-400">
                {role.role}
              </div>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-full px-2 py-1 text-[11px] font-bold tracking-widest"
            style={{
              border: "1px solid #334155",
              color: "#cbd5e1",
              backgroundColor: "#0a1228",
              cursor: "pointer",
            }}
            aria-label="닫기"
          >
            ✕
          </button>
        </header>

        {/* Freshness pill — what cycle did this data come from? */}
        <div className="flex flex-wrap items-center gap-2">
          <span
            className="agent-detail-freshness"
            data-testid="agent-detail-freshness"
            style={{
              color: freshnessMeta.color,
              borderColor: `${freshnessMeta.color}66`,
              backgroundColor: "#0a1228",
            }}
          >
            ⌚ {freshnessMeta.label}
          </span>
          <span className="text-[10px] tracking-widest text-slate-500">
            phase: {phase}
            {workingAgentId && ` · 현재 stage owner: ${workingAgentId.toUpperCase()}`}
          </span>
        </div>

        {/* 현재 역할 */}
        <Section title="현재 역할" source="role_map">
          <div className="rounded-lg px-3 py-2 text-[12px]" style={{
            backgroundColor: "#0a1228",
            border: `1.5px solid ${role.accent}66`,
          }}>
            {role.role} · <span className="text-slate-300">{role.label}</span>
          </div>
        </Section>

        {/* 현재 작업 */}
        <Section title="현재 작업" source={currentTask?.source}>
          <div className="rounded-lg px-3 py-2 text-[12px]" style={{
            backgroundColor: "#0a1228",
            border: "1.5px solid #1e293b",
          }}>
            {currentTask?.text || "—"}
          </div>
        </Section>

        {/* 마지막 명령 */}
        <Section title="마지막 명령" source={lastCommand ? "system_log" : null}>
          {lastCommand ? (
            <div
              className="rounded-lg px-3 py-2 text-[11.5px]"
              style={{
                backgroundColor: "#0a1228",
                border: "1.5px solid #1e293b",
              }}
            >
              <div className="text-[10px] tracking-widest text-slate-400">
                {fmtTime(lastCommand.ev?.created_at)} · {lastCommand.category}
              </div>
              <div className="mt-1 text-slate-100">
                {lastCommand.ev?.message || "(no message)"}
              </div>
            </div>
          ) : (
            <div className="text-[11.5px] text-slate-300">아직 명령 없음</div>
          )}
        </Section>

        {/* 최근 로그 5개 */}
        <Section
          title="최근 로그"
          source={recentLogs.length > 0 ? "system_log" : null}
        >
          {recentLogs.length === 0 ? (
            <div className="text-[11.5px] text-slate-300">관련 로그 없음</div>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {recentLogs.map((entry) => (
                <LogRow key={entry.ev?.id || Math.random()} entry={entry} />
              ))}
            </ul>
          )}
        </Section>

        {/* 실패 원인 — fresh failures only. Stale failures bubble up
            in the "previous issue" collapsible below so they don't
            mislead the operator. */}
        <Section title="실패 원인" source={failureReason?.source}>
          <div
            className="rounded-lg px-3 py-2 text-[12px]"
            style={{
              backgroundColor: failureReason ? "#1c0d12" : "#0a1228",
              border: `1.5px solid ${failureReason ? "#f8717166" : "#1e293b"}`,
              color: failureReason ? "#fecaca" : "#cbd5e1",
            }}
          >
            {failureReason
              ? failureReason.text
              : isRunningPhase(phase)
                ? "현재 cycle 기준 실패 없음"
                : "현재 실행 없음"}
          </div>
        </Section>

        {/* 이전 cycle 미해결 — collapsible, never red. */}
        {previousIssue && (
          <Section
            title="이전 cycle 미해결"
            source={previousIssue.source}
          >
            <details className="agent-detail-previous-issue">
              <summary className="cursor-pointer text-[11px] tracking-widest text-slate-400">
                이전 기록 보기
              </summary>
              <div className="mt-2 text-[11.5px] text-slate-300">
                {previousIssue.text}
              </div>
            </details>
          </Section>
        )}

        {/* 다음 액션 */}
        <Section title="다음 액션" source={nextAction?.source}>
          <div
            className="rounded-lg px-3 py-2 text-[12px]"
            style={{
              backgroundColor: "#0a1228",
              border: "1.5px solid #d4a84366",
              color: "#fde68a",
            }}
          >
            ▶ {nextAction?.text || "다음 cycle 대기"}
          </div>
        </Section>

        {/* 관련 파일 변경 목록 */}
        <Section
          title="관련 파일 변경"
          source={changedFiles.length > 0 ? "factory_state.claude_apply_changed_files" : null}
        >
          {changedFiles.length === 0 ? (
            <div className="text-[11.5px] text-slate-300">변경 파일 없음</div>
          ) : (
            <ul className="flex flex-col gap-1">
              {changedFiles.map((f) => (
                <li
                  key={f}
                  className="rounded px-2 py-1 text-[11px] font-mono text-slate-100 autopilot-stat-ellipsis-wide"
                  style={{
                    backgroundColor: "#0a1228",
                    border: "1.5px solid #1e293b",
                  }}
                  title={f}
                >
                  {f}
                </li>
              ))}
            </ul>
          )}
        </Section>
      </aside>
    </div>
  );
}
