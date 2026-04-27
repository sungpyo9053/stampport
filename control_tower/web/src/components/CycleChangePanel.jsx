// "이번 사이클 실제 변경" — Cycle Effectiveness panel.
//
// Sits above ReleasePreviewPanel inside ControlDock and answers ONE
// question for the operator: did the most recent factory cycle
// actually change product code, or did we just spin? Reads
// `metadata.local_factory.cycle_effectiveness` (built in runner.py
// from factory_state.json + publish_state.json) and renders:
//   - cycle id + status badge (succeeded / planning_only /
//     no_code_change / failed)
//   - changed file count + file list
//   - commit hash (linked to GitHub) + push status
//   - no-op reason when code_changed=false
//   - failed_stage / failed_reason / suggested_action when failed

const FILE_LIST_VISIBLE = 10;

const STATUS_PILL = {
  succeeded:       { label: "코드 변경 + 검증 통과", dot: "#34d399", text: "#86efac" },
  planning_only:   { label: "기획/디자인만 산출",   dot: "#fbbf24", text: "#fde68a" },
  no_code_change:  { label: "코드 변경 없음",       dot: "#94a3b8", text: "#cbd5e1" },
  failed:          { label: "사이클 실패",          dot: "#f87171", text: "#fecaca" },
  running:         { label: "사이클 진행 중",       dot: "#7dd3fc", text: "#bae6fd" },
  idle:            { label: "대기",                 dot: "#475569", text: "#94a3b8" },
};

function formatRelative(iso) {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const delta = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (delta < 60) return `${delta}초 전`;
  if (delta < 3600) return `${Math.floor(delta / 60)}분 전`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}시간 전`;
  return `${Math.floor(delta / 86400)}일 전`;
}

function StatusPill({ status }) {
  const meta = STATUS_PILL[status] || STATUS_PILL.idle;
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded px-2 py-0.5 text-[10px] font-bold tracking-widest"
      style={{
        backgroundColor: "#0a1228",
        border: `1px solid ${meta.dot}55`,
        color: meta.text,
      }}
    >
      <span
        className="inline-block h-1.5 w-1.5 rounded-full"
        style={{ backgroundColor: meta.dot }}
      />
      {meta.label}
      <span className="font-mono text-[9px] opacity-70">· {status}</span>
    </span>
  );
}

function FileChips({ files, total }) {
  if (!files || files.length === 0) return null;
  const visible = files.slice(0, FILE_LIST_VISIBLE);
  const overflow = (total ?? files.length) - visible.length;
  return (
    <div className="flex flex-wrap gap-1">
      {visible.map((p) => (
        <code
          key={p}
          title={p}
          className="rounded px-1.5 py-0.5 text-[10px] text-slate-200"
          style={{
            backgroundColor: "#0a1228",
            border: "1px solid #1e293b",
          }}
        >
          {p}
        </code>
      ))}
      {overflow > 0 && (
        <span className="text-[10px] text-slate-500">외 {overflow}개</span>
      )}
    </div>
  );
}

function PushStatusBadge({ status }) {
  if (!status) {
    return (
      <span className="text-[10.5px] text-slate-500">
        push 상태 — 아직 push되지 않음
      </span>
    );
  }
  const PILL = {
    succeeded: { color: "#34d399", text: "push 성공" },
    failed:    { color: "#f87171", text: "push 실패" },
    dry_run:   { color: "#7dd3fc", text: "dry-run" },
    noop:      { color: "#94a3b8", text: "변경 없음" },
  };
  const m = PILL[status] || { color: "#94a3b8", text: status };
  return (
    <span
      className="rounded px-1.5 py-0.5 text-[10px] font-bold tracking-widest"
      style={{ color: m.color, border: `1px solid ${m.color}55` }}
    >
      {m.text}
    </span>
  );
}

function CommitHashLine({ hash, hashShort, pushStatus, pushAt }) {
  if (!hash) {
    return (
      <p className="text-[10.5px] text-slate-500">
        commit — 이번 사이클이 만든 push가 아직 없음
      </p>
    );
  }
  const url = `https://github.com/sungpyo9053/stampport/commit/${hash}`;
  return (
    <div className="flex flex-wrap items-center gap-2 text-[11px]">
      <span className="text-slate-400">commit</span>
      <a
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        className="font-mono text-sky-300 hover:text-sky-200"
      >
        {hashShort || hash.slice(0, 8)}
      </a>
      <PushStatusBadge status={pushStatus} />
      {pushAt && (
        <span className="text-[10px] text-slate-500">
          · {formatRelative(pushAt)}
        </span>
      )}
    </div>
  );
}

function FailureBox({ stage, reason, suggested }) {
  if (!stage && !reason && !suggested) return null;
  return (
    <div
      className="grid gap-1 rounded p-2 text-[11px]"
      style={{
        backgroundColor: "rgba(248,113,113,0.08)",
        border: "1px solid #f8717155",
      }}
    >
      {stage && (
        <div>
          <span className="text-[10px] font-bold uppercase tracking-widest text-rose-300">
            실패 단계
          </span>
          <span className="ml-2 font-mono text-rose-200">{stage}</span>
        </div>
      )}
      {reason && (
        <div>
          <span className="text-[10px] font-bold uppercase tracking-widest text-rose-300">
            이유
          </span>
          <p className="mt-0.5 leading-snug text-rose-100">{reason}</p>
        </div>
      )}
      {suggested && (
        <div>
          <span className="text-[10px] font-bold uppercase tracking-widest text-rose-300">
            권장 조치
          </span>
          <p className="mt-0.5 leading-snug text-rose-100">{suggested}</p>
        </div>
      )}
    </div>
  );
}

export default function CycleChangePanel({ runner }) {
  const lf = runner?.metadata_json?.local_factory;
  const ce = lf?.cycle_effectiveness;

  // Render even when ce is missing — older runners (pre-cycle_effectiveness)
  // still send a heartbeat, and the operator should see "데이터 없음"
  // rather than the panel disappearing silently.
  if (!ce) {
    return (
      <section
        className="grid gap-2 rounded p-3"
        data-testid="cycle-change-panel"
        style={{
          backgroundColor: "#0e1a35",
          border: "1.5px solid #1e293b",
          borderRadius: 6,
          fontFamily: "ui-monospace, monospace",
        }}
      >
        <header>
          <h3 className="text-[12px] font-bold uppercase tracking-[0.3em] text-[#d4a843]">
            이번 사이클 실제 변경
          </h3>
          <p className="mt-0.5 text-[10.5px] text-slate-500">
            runner heartbeat에 cycle_effectiveness가 아직 들어오지 않았습니다.
            (구버전 runner 또는 사이클 실행 전)
          </p>
        </header>
      </section>
    );
  }

  const {
    cycle_id,
    status,
    code_changed,
    changed_files_count,
    changed_files,
    commit_hash,
    commit_hash_short,
    push_status,
    push_at,
    no_code_change_reason,
    last_claude_apply_status,
    last_claude_apply_message,
    failed_stage,
    failed_reason,
    suggested_action,
  } = ce;

  return (
    <section
      className="grid gap-2 rounded p-3"
      data-testid="cycle-change-panel"
      style={{
        backgroundColor: "#0e1a35",
        border: "1.5px solid #1e293b",
        borderRadius: 6,
        fontFamily: "ui-monospace, monospace",
      }}
    >
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-baseline gap-2">
          <h3 className="text-[12px] font-bold uppercase tracking-[0.3em] text-[#d4a843]">
            이번 사이클 실제 변경
          </h3>
          {cycle_id != null && (
            <span className="text-[10px] tracking-widest text-slate-500">
              cycle #{cycle_id}
            </span>
          )}
        </div>
        <StatusPill status={status || "idle"} />
      </header>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-300">
        <span>
          코드 변경 ·{" "}
          <span
            className="font-bold"
            style={{ color: code_changed ? "#86efac" : "#94a3b8" }}
          >
            {code_changed ? "있음" : "없음"}
          </span>
        </span>
        <span>
          변경 파일 ·{" "}
          <span className="font-bold text-slate-100">
            {changed_files_count ?? 0}개
          </span>
        </span>
        <span className="text-slate-500">
          claude_apply · {last_claude_apply_status || "skipped"}
        </span>
      </div>

      {/* commit + push */}
      <CommitHashLine
        hash={commit_hash}
        hashShort={commit_hash_short}
        pushStatus={push_status}
        pushAt={push_at}
      />

      {/* changed file list */}
      {changed_files_count > 0 && (
        <div className="grid gap-1">
          <span className="text-[10px] font-bold uppercase tracking-widest text-[#d4a843]">
            변경 파일 목록
          </span>
          <FileChips
            files={changed_files || []}
            total={changed_files_count}
          />
        </div>
      )}

      {/* no-op reason */}
      {!code_changed && (no_code_change_reason || last_claude_apply_message) && (
        <div
          className="grid gap-1 rounded p-2 text-[11px]"
          style={{
            backgroundColor: "rgba(148,163,184,0.06)",
            border: "1px solid #1e293b",
          }}
        >
          <span className="text-[10px] font-bold uppercase tracking-widest text-slate-400">
            no-op 사유
          </span>
          {no_code_change_reason && (
            <p className="font-mono text-[10.5px] text-slate-300">
              {no_code_change_reason}
            </p>
          )}
          {last_claude_apply_message && (
            <p className="leading-snug text-slate-400">
              {last_claude_apply_message}
            </p>
          )}
        </div>
      )}

      {/* failure box */}
      <FailureBox
        stage={failed_stage}
        reason={failed_reason}
        suggested={suggested_action}
      />
    </section>
  );
}
