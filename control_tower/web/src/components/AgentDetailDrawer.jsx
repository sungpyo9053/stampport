import { useEffect, useMemo } from "react";
import { buildSystemLogEntries } from "../utils/eventClassifier.js";

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

function deriveCurrentTask(agentId, meta) {
  const aa = meta?.agent_accountability || {};
  const fs = meta?.factory_state || {};
  const ap = meta?.autopilot || {};
  const pr = meta?.pipeline_recovery || {};

  // Stage-based.
  const stage = pr.current_stage || fs.current_stage;
  if (stage) {
    return `현재 stage: ${stage}`;
  }

  // Per-agent specific.
  if (agentId === "pm") {
    if (fs.pm_decision_status) return `pm_decision_status: ${fs.pm_decision_status}`;
  }
  if (agentId === "planner") {
    if (fs.product_planner_status) return `product_planner: ${fs.product_planner_status}`;
  }
  if (agentId === "qa") {
    const qa = meta?.qa_gate || {};
    if (qa.qa_status) return `qa_status: ${qa.qa_status}`;
  }
  if (agentId === "deploy") {
    const pub = meta?.publish || {};
    if (pub.last_push_status) return `last_push: ${pub.last_push_status}`;
  }

  if (ap.status === "running") {
    return `Auto Pilot ${ap.mode || ""} 진행 중 — cycle ${ap.cycle_count || 0}/${ap.max_cycles || "?"}`;
  }
  return "다음 cycle 대기";
}

function deriveFailureReason(agentId, meta) {
  const aa = meta?.agent_accountability || {};
  const fs = meta?.factory_state || {};
  const acc = (aa.agents || {})[agentId] || {};
  if (Array.isArray(acc.problems) && acc.problems.length > 0) {
    return acc.problems.join(" / ");
  }
  if (aa.blocking_agent === agentId && aa.blocking_reason) {
    return aa.blocking_reason;
  }
  if (agentId === "qa") {
    const qa = meta?.qa_gate || {};
    if (qa.qa_status === "failed") return qa.qa_failed_reason || "QA failed";
  }
  if (agentId === "deploy") {
    const pub = meta?.publish || {};
    if (pub.last_push_status === "failed") return pub.last_push_reason || "push failed";
  }
  // Smoke failure_code if relevant.
  const smoke = meta?.factory_smoke || meta?.smoke || {};
  if (smoke.failure_code) {
    return `smoke failure_code: ${smoke.failure_code}`;
  }
  return null;
}

function deriveNextAction(agentId, meta) {
  const aa = meta?.agent_accountability || {};
  const acc = (aa.agents || {})[agentId] || {};
  if (acc.retry_prompt) return acc.retry_prompt;
  if (aa.blocking_agent === agentId && aa.next_action) return aa.next_action;
  const fs = meta?.factory_state || {};
  if (agentId === "pm" && fs.claude_rework_prompt) return fs.claude_rework_prompt;
  if (fs.claude_repair_prompt) return fs.claude_repair_prompt;
  const ap = meta?.autopilot || {};
  if (ap.stop_reason) return `정지 사유: ${ap.stop_reason}`;
  return "다음 cycle 대기";
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

function Section({ title, children }) {
  return (
    <div className="agent-detail-section flex flex-col gap-1.5">
      <h4 className="text-[9.5px] font-bold uppercase tracking-[0.3em] text-amber-300">
        {title}
      </h4>
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

  const currentTask = useMemo(
    () => (agentId ? deriveCurrentTask(agentId, meta) : ""),
    [agentId, meta],
  );
  const failureReason = useMemo(
    () => (agentId ? deriveFailureReason(agentId, meta) : null),
    [agentId, meta],
  );
  const nextAction = useMemo(
    () => (agentId ? deriveNextAction(agentId, meta) : ""),
    [agentId, meta],
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

  if (!agentId) return null;

  return (
    <div
      className="agent-detail-overlay fixed inset-0 z-[80] flex justify-end"
      data-testid="agent-detail-drawer"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={`${role.label} 상세`}
      style={{ backgroundColor: "rgba(5, 9, 18, 0.7)" }}
    >
      <aside
        className="agent-detail-panel relative flex h-full w-full max-w-md flex-col gap-3 overflow-y-auto p-4 sm:p-5"
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
        <header className="flex items-start justify-between gap-2">
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

        {/* 현재 역할 */}
        <Section title="현재 역할">
          <div className="rounded-lg px-3 py-2 text-[12px]" style={{
            backgroundColor: "#0a1228",
            border: `1px solid ${role.accent}55`,
          }}>
            {role.role} · <span className="text-slate-400">{role.label}</span>
          </div>
        </Section>

        {/* 현재 작업 */}
        <Section title="현재 작업">
          <div className="rounded-lg px-3 py-2 text-[12px]" style={{
            backgroundColor: "#0a1228",
            border: "1px solid #1e293b",
          }}>
            {currentTask}
          </div>
        </Section>

        {/* 마지막 명령 */}
        <Section title="마지막 명령">
          {lastCommand ? (
            <div
              className="rounded-lg px-3 py-2 text-[11.5px]"
              style={{
                backgroundColor: "#0a1228",
                border: "1px solid #1e293b",
              }}
            >
              <div className="text-[9.5px] tracking-widest text-slate-500">
                {fmtTime(lastCommand.ev?.created_at)} · {lastCommand.category}
              </div>
              <div className="mt-1 text-slate-200">
                {lastCommand.ev?.message || "(no message)"}
              </div>
            </div>
          ) : (
            <div className="text-[11.5px] text-slate-500">아직 명령 없음</div>
          )}
        </Section>

        {/* 최근 로그 5개 */}
        <Section title="최근 로그">
          {recentLogs.length === 0 ? (
            <div className="text-[11.5px] text-slate-500">관련 로그 없음</div>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {recentLogs.map((entry) => (
                <LogRow key={entry.ev?.id || Math.random()} entry={entry} />
              ))}
            </ul>
          )}
        </Section>

        {/* 실패 원인 */}
        <Section title="실패 원인">
          <div
            className="rounded-lg px-3 py-2 text-[12px]"
            style={{
              backgroundColor: failureReason ? "#1c0d12" : "#0a1228",
              border: `1px solid ${failureReason ? "#f8717155" : "#1e293b"}`,
              color: failureReason ? "#fecaca" : "#94a3b8",
            }}
          >
            {failureReason || "현재 실패 없음"}
          </div>
        </Section>

        {/* 다음 액션 */}
        <Section title="다음 액션">
          <div
            className="rounded-lg px-3 py-2 text-[12px]"
            style={{
              backgroundColor: "#0a1228",
              border: "1px solid #d4a84355",
              color: "#fde68a",
            }}
          >
            ▶ {nextAction || "다음 cycle 대기"}
          </div>
        </Section>

        {/* 관련 파일 변경 목록 */}
        <Section title="관련 파일 변경">
          {changedFiles.length === 0 ? (
            <div className="text-[11.5px] text-slate-500">변경 파일 없음</div>
          ) : (
            <ul className="flex flex-col gap-1">
              {changedFiles.map((f) => (
                <li
                  key={f}
                  className="rounded px-2 py-1 text-[11px] font-mono text-slate-200"
                  style={{
                    backgroundColor: "#0a1228",
                    border: "1px solid #1e293b",
                  }}
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
