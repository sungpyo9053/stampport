// "이번 사이클 실제 구현" panel.
//
// Reads the runner heartbeat's metadata.local_factory.cycle_effectiveness
// block and tells the operator the *only* question that matters: did
// this cycle ship code, or did we just spin? Cycle Board / ArtifactBoard
// already show planning artifacts; this panel is the antidote — if a
// dozen artifact cards landed but no real code changed, this panel
// shouts about it.
//
// Status meanings (cycle.py main()):
//   succeeded       → 실제 코드 변경 + 검증 통과
//   docs_only       → 변경은 있었지만 docs/config 만 (사용자 영향 없음)
//   planning_only   → 기획/디자인 산출물만 생성, 코드 변경 없음
//   no_code_change  → 산출물도 없음 — claude_apply 미실행 등
//   failed          → 단계 중간 실패
//
// We deliberately keep this small enough to live next to PingPongBoard.

const STATUS_TONE = {
  succeeded:       { label: "실제 코드 변경", color: "#34d399", glow: "#34d39955" },
  docs_only:       { label: "Docs/Config 만",  color: "#fbbf24", glow: "#fbbf2455" },
  planning_only:   { label: "기획만",          color: "#a78bfa", glow: "#a78bfa55" },
  no_code_change:  { label: "변경 없음",        color: "#94a3b8", glow: "#94a3b855" },
  failed:          { label: "실패",            color: "#f87171", glow: "#f8717155" },
  running:         { label: "진행 중",         color: "#38bdf8", glow: "#38bdf855" },
};

function pickPrimaryRunner(runners = []) {
  const ranked = [...runners].sort((a, b) => {
    const order = (s) => (s === "online" ? 0 : s === "busy" ? 1 : 2);
    return order(a?.status) - order(b?.status);
  });
  return ranked.find((r) => r?.metadata_json?.local_factory) || null;
}

function Pill({ children, color, dim = false }) {
  return (
    <span
      className="rounded px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest"
      style={{
        color: dim ? `${color}aa` : color,
        border: `1px solid ${color}66`,
        backgroundColor: "#0a1228",
      }}
    >
      {children}
    </span>
  );
}

function TierBadge({ active, label, color }) {
  return (
    <span
      className="rounded px-1.5 py-0.5 text-[10px] font-bold tracking-widest"
      style={{
        color: active ? color : "#475569",
        border: `1px solid ${active ? color : "#1e293b"}66`,
        backgroundColor: active ? `${color}11` : "transparent",
      }}
    >
      {active ? "●" : "○"} {label}
    </span>
  );
}

export default function CycleEffectivenessPanel({ runners = [] }) {
  const runner = pickPrimaryRunner(runners);
  const ce = runner?.metadata_json?.local_factory?.cycle_effectiveness || null;

  // Default empty-state copy — keep the panel rendering even when
  // no runner is reporting yet, so the operator sees the "shape" of
  // what's coming.
  const status = ce?.status || "running";
  const tone = STATUS_TONE[status] || STATUS_TONE.running;
  const cycleId = ce?.cycle_id;
  const codeChanged = !!ce?.code_changed;
  const changedFiles = Array.isArray(ce?.changed_files) ? ce.changed_files : [];
  const changedCount = ce?.changed_files_count ?? changedFiles.length;
  const ticketExists = !!ce?.implementation_ticket_exists;
  const ticketStatus = ce?.implementation_ticket_status || "skipped";
  const ticketTargets = Array.isArray(ce?.implementation_ticket_target_files)
    ? ce.implementation_ticket_target_files
    : [];
  const ticketTargetsCount =
    ce?.implementation_ticket_target_files_count ?? ticketTargets.length;
  const ticketFeature =
    ce?.implementation_ticket_selected_feature || "(미선택)";
  const ticketPreview = ce?.implementation_ticket_preview || "";
  const validationStatus = ce?.validation_status || "skipped";
  const claudeApplyStatus = ce?.last_claude_apply_status || "skipped";
  const claudeApplyDidRun =
    claudeApplyStatus === "applied" || claudeApplyStatus === "no_changes";
  const commitShort = ce?.commit_hash_short || null;
  const pushStatus = ce?.push_status || null;
  const noCodeChangeReason = ce?.no_code_change_reason || null;
  const failedStage = ce?.failed_stage || null;
  const failedReason = ce?.failed_reason || null;
  const suggestedAction = ce?.suggested_action || null;

  // Headline copy — operator-readable single sentence.
  let headline = "";
  if (status === "succeeded") {
    const fe = ce?.frontend_changed ? "프론트" : null;
    const be = ce?.backend_changed ? "백엔드" : null;
    const ct = ce?.control_tower_changed ? "관제실" : null;
    const tiers = [fe, be, ct].filter(Boolean).join(" + ") || "코드";
    headline = `이번 사이클은 실제 ${tiers} 코드를 ${changedCount}개 변경했습니다.`;
  } else if (status === "docs_only") {
    headline =
      `이번 사이클은 docs/config 파일 ${changedCount}개만 바뀌어, ` +
      "사용자 영향이 있는 코드 변경은 없습니다.";
  } else if (status === "planning_only") {
    headline =
      ticketStatus === "missing"
        ? "Implementation Ticket 의 수정 대상 파일이 없어 개발 단계로 넘어가지 않았습니다."
        : "이번 사이클은 기획 산출물만 생성되어 코드 변경이 없습니다.";
  } else if (status === "no_code_change") {
    headline = "이번 사이클은 코드 변경이 발생하지 않았습니다.";
  } else if (status === "failed") {
    headline =
      failedStage
        ? `${failedStage} 단계에서 실패해 push 하지 않았습니다.`
        : "단계 실패 — push 하지 않았습니다.";
  } else {
    headline = "사이클 진행 중 — 결과 대기 중";
  }

  return (
    <section
      className="flex flex-col gap-2 p-3"
      data-testid="cycle-effectiveness-panel"
      style={{
        backgroundColor: "#0e1a35",
        border: "1.5px solid #0e4a3a",
        borderRadius: 6,
        fontFamily: "ui-monospace, monospace",
        boxShadow: tone.glow ? `0 0 14px ${tone.glow}` : "none",
      }}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span
            className="inline-block h-2 w-2"
            style={{ backgroundColor: tone.color }}
          />
          <span
            className="text-[10px] font-bold uppercase tracking-[0.3em]"
            style={{ color: tone.color }}
          >
            이번 사이클 실제 구현
          </span>
          {cycleId != null && (
            <span className="text-[9.5px] tracking-widest text-slate-500">
              #{cycleId}
            </span>
          )}
        </div>
        <Pill color={tone.color}>{tone.label}</Pill>
      </div>

      <div
        className="rounded p-2 text-[12px] leading-snug"
        style={{
          backgroundColor: "#0a1228",
          color: "#f5e9d3",
          border: `1px solid ${tone.color}55`,
        }}
      >
        {headline}
      </div>

      {/* Tier badges + counts */}
      <div className="flex flex-wrap items-center gap-1.5">
        <TierBadge
          active={!!ce?.frontend_changed}
          label="프론트"
          color="#facc15"
        />
        <TierBadge
          active={!!ce?.backend_changed}
          label="백엔드"
          color="#34d399"
        />
        <TierBadge
          active={!!ce?.control_tower_changed}
          label="관제실"
          color="#a78bfa"
        />
        <span className="ml-auto text-[10px] tracking-widest text-slate-500">
          변경 파일{" "}
          <span
            className="text-[12px] font-bold"
            style={{ color: codeChanged ? "#34d399" : "#94a3b8" }}
          >
            {changedCount}
          </span>
        </span>
      </div>

      {/* Implementation Ticket */}
      <div
        className="rounded p-2"
        style={{
          backgroundColor: "#0a1228",
          border: ticketExists ? "1px solid #d4a84355" : "1px dashed #f8717188",
        }}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="text-[10px] font-bold uppercase tracking-[0.3em] text-[#d4a843]">
            Implementation Ticket
          </span>
          {ticketStatus === "generated" && (
            <Pill color="#34d399">{`대상 파일 ${ticketTargetsCount}개`}</Pill>
          )}
          {ticketStatus === "missing" && <Pill color="#f87171">MISSING</Pill>}
          {ticketStatus === "skipped" && !ticketExists && (
            <Pill color="#94a3b8" dim>SKIPPED</Pill>
          )}
        </div>
        {!claudeApplyDidRun && (
          <div className="mt-1 flex items-center gap-1.5 text-[10.5px] tracking-wider">
            <Pill color="#fb923c">개발 단계 미실행</Pill>
            <span className="text-slate-400">
              claude_apply={claudeApplyStatus} — 이번 사이클은 코드 변경 없이 종료됩니다.
            </span>
          </div>
        )}
        <div className="mt-1 text-[11px] tracking-wider text-slate-300">
          선정 기능: <span className="font-bold">{ticketFeature}</span>
        </div>
        {ticketTargets.length > 0 && (
          <ul className="mt-1 max-h-24 overflow-y-auto pr-1 text-[10.5px] leading-snug text-slate-400">
            {ticketTargets.map((p) => (
              <li key={p} className="truncate">
                · {p}
              </li>
            ))}
          </ul>
        )}
        {ticketPreview && ticketTargets.length === 0 && (
          <pre className="mt-1 max-h-20 overflow-y-auto whitespace-pre-wrap text-[10px] leading-snug text-slate-500">
            {ticketPreview}
          </pre>
        )}
      </div>

      {/* Validation + commit/push row */}
      <div className="grid grid-cols-2 gap-2">
        <div
          className="rounded p-2 text-[10.5px] tracking-wider"
          style={{ backgroundColor: "#0a1228", border: "1px solid #1e293b" }}
        >
          <div className="text-[9.5px] uppercase tracking-[0.3em] text-slate-500">
            검증
          </div>
          <div
            className="mt-0.5 text-[12px] font-bold"
            style={{
              color:
                validationStatus === "passed"
                  ? "#34d399"
                  : validationStatus === "failed"
                  ? "#f87171"
                  : "#94a3b8",
            }}
          >
            {validationStatus === "passed"
              ? "PASSED"
              : validationStatus === "failed"
              ? "FAILED"
              : "SKIPPED"}
          </div>
        </div>
        <div
          className="rounded p-2 text-[10.5px] tracking-wider"
          style={{ backgroundColor: "#0a1228", border: "1px solid #1e293b" }}
        >
          <div className="text-[9.5px] uppercase tracking-[0.3em] text-slate-500">
            commit · push
          </div>
          <div className="mt-0.5 flex items-center gap-1.5">
            <span
              className="text-[12px] font-bold"
              style={{ color: commitShort ? "#d4a843" : "#475569" }}
            >
              {commitShort || "—"}
            </span>
            {pushStatus && (
              <Pill
                color={
                  pushStatus === "ok"
                    ? "#34d399"
                    : pushStatus === "failed"
                    ? "#f87171"
                    : "#94a3b8"
                }
              >
                {pushStatus.toUpperCase()}
              </Pill>
            )}
          </div>
        </div>
      </div>

      {/* No-op / failure detail */}
      {(noCodeChangeReason || failedReason || suggestedAction) && (
        <div
          className="rounded p-2 text-[10.5px] leading-snug"
          style={{
            backgroundColor: "#0a1228",
            border: `1px solid ${tone.color}33`,
            color: "#cbd5e1",
          }}
        >
          {failedReason && (
            <div>
              <span className="text-rose-400">실패 사유:</span> {failedReason}
            </div>
          )}
          {noCodeChangeReason && !failedReason && (
            <div>
              <span className="text-amber-300">사유:</span> {noCodeChangeReason}
            </div>
          )}
          {suggestedAction && (
            <div className="mt-0.5 text-slate-400">
              <span className="text-slate-500">권장 조치:</span> {suggestedAction}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
