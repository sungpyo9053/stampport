import { useMemo, useState } from "react";
import { startAutopilot, stopAutopilot } from "../api/controlTowerApi.js";

// Auto Pilot Publish panel.
//
// Reads from runners[].metadata_json.local_factory.autopilot — written
// by control_tower/local_runner/runner.py:_build_autopilot_meta which
// in turn surfaces .runtime/autopilot_state.json. Posts start/stop
// commands through the runner queue (allowlisted as start_autopilot /
// stop_autopilot in schemas.py).

const MODE_LABEL = {
  safe_run: "Safe Run (cycle만 실행)",
  auto_commit: "Auto Commit (commit만)",
  auto_publish: "Auto Publish (commit + push + health)",
};

const STATUS_TONE = {
  idle: { color: "#64748b", label: "대기" },
  running: { color: "#34d399", label: "실행 중" },
  stopped: { color: "#94a3b8", label: "정지" },
  failed: { color: "#f87171", label: "실패" },
};

function pickAutopilot(runners = []) {
  for (const r of runners) {
    const ap = r?.metadata_json?.local_factory?.autopilot;
    if (ap) return { runnerId: r.id, state: ap };
  }
  return { runnerId: runners?.[0]?.id || null, state: null };
}

function fmtIso(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString("ko-KR", { hour12: false });
  } catch {
    return iso;
  }
}

function StatRow({ label, value, mono }) {
  return (
    <div className="flex items-center justify-between gap-2 text-[11px]">
      <span className="text-slate-400">{label}</span>
      <span
        className={
          mono
            ? "font-mono text-slate-100"
            : "text-slate-100"
        }
      >
        {value || "—"}
      </span>
    </div>
  );
}

export default function AutoPilotPanel({ runners = [], onSent }) {
  const { runnerId, state } = useMemo(
    () => pickAutopilot(runners),
    [runners],
  );

  const [mode, setMode] = useState(state?.mode || "safe_run");
  const [maxCycles, setMaxCycles] = useState(state?.max_cycles || 5);
  const [maxHours, setMaxHours] = useState(state?.max_hours || 6);
  const [stopOnHold, setStopOnHold] = useState(
    state?.stop_on_hold !== undefined ? !!state.stop_on_hold : true,
  );
  const [requireRender, setRequireRender] = useState(true);
  const [requireHealth, setRequireHealth] = useState(true);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState("");

  const status = state?.status || "idle";
  const tone = STATUS_TONE[status] || STATUS_TONE.idle;
  const cycleCount = state?.cycle_count || 0;
  const totalCycles = state?.max_cycles || maxCycles;
  const lastVerdict = state?.last_verdict || "—";
  const lastFailure = state?.last_failure_code || "";
  const lastCommit = (state?.last_commit_hash || "").slice(0, 8) || "—";
  const lastPush = state?.last_push_status || "—";
  const lastHealth = state?.last_health_status || "—";
  const lastRender = state?.last_render_status || "—";
  const stopReason = state?.stop_reason || "—";
  const reportPath = state?.report_path || ".runtime/autopilot_report.md";

  const isRunning = status === "running";
  const canStart = !!runnerId && !busy && !isRunning;
  const canStop = !!runnerId && !busy && isRunning;

  async function handleStart() {
    if (!runnerId) {
      setFeedback("러너가 등록되어 있지 않아요 — 먼저 runner heartbeat 확인");
      return;
    }
    setBusy(true);
    setFeedback("");
    try {
      await startAutopilot(runnerId, {
        autopilot_enabled: true,
        autopilot_mode: mode,
        max_cycles: Number(maxCycles) || 5,
        max_hours: Number(maxHours) || 6,
        stop_on_hold: !!stopOnHold,
        require_scope_consistency: true,
        require_render_check: !!requireRender,
        require_api_health: !!requireHealth,
      });
      setFeedback("Auto Pilot 시작 요청 완료 — 다음 heartbeat 에서 상태가 갱신됩니다.");
      onSent && onSent();
    } catch (e) {
      setFeedback(`시작 실패: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleStop() {
    if (!runnerId) return;
    setBusy(true);
    setFeedback("");
    try {
      await stopAutopilot(runnerId, "operator stop from dashboard");
      setFeedback("Auto Pilot 정지 요청 완료.");
      onSent && onSent();
    } catch (e) {
      setFeedback(`정지 실패: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      className="flex flex-col gap-2 p-3"
      data-testid="autopilot-panel"
      style={{
        backgroundColor: "#0e1a35",
        border: "1.5px solid #5b3a14",
        borderRadius: 6,
        fontFamily: "ui-monospace, monospace",
      }}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span
            className="inline-block h-2 w-2"
            style={{ backgroundColor: tone.color }}
          />
          <span className="text-[10px] font-bold uppercase tracking-[0.3em] text-amber-300">
            AUTO PILOT PUBLISH
          </span>
          <span
            className="rounded px-1.5 py-0.5 text-[9px] font-bold tracking-widest"
            style={{
              backgroundColor: tone.color,
              color: "#0e1a35",
            }}
          >
            {tone.label}
          </span>
        </div>
        <span className="text-[10px] text-slate-500">
          runner: {runnerId || "(none)"}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-2 text-[11px]">
        <label className="col-span-2 flex flex-col gap-1">
          <span className="text-slate-400">모드</span>
          <select
            value={mode}
            disabled={isRunning}
            onChange={(e) => setMode(e.target.value)}
            className="rounded bg-slate-800 px-2 py-1 text-slate-100 disabled:opacity-50"
          >
            {Object.entries(MODE_LABEL).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-slate-400">Max cycles</span>
          <input
            type="number"
            min={1}
            max={50}
            value={maxCycles}
            disabled={isRunning}
            onChange={(e) => setMaxCycles(e.target.value)}
            className="rounded bg-slate-800 px-2 py-1 text-slate-100 disabled:opacity-50"
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-slate-400">Max hours</span>
          <input
            type="number"
            min={0.1}
            max={48}
            step={0.5}
            value={maxHours}
            disabled={isRunning}
            onChange={(e) => setMaxHours(e.target.value)}
            className="rounded bg-slate-800 px-2 py-1 text-slate-100 disabled:opacity-50"
          />
        </label>

        <label className="col-span-2 flex items-center gap-2">
          <input
            type="checkbox"
            checked={stopOnHold}
            disabled={isRunning}
            onChange={(e) => setStopOnHold(e.target.checked)}
          />
          <span className="text-slate-300">Stop on HOLD</span>
        </label>

        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={requireRender}
            disabled={isRunning}
            onChange={(e) => setRequireRender(e.target.checked)}
          />
          <span className="text-slate-300">Render smoke</span>
        </label>

        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={requireHealth}
            disabled={isRunning}
            onChange={(e) => setRequireHealth(e.target.checked)}
          />
          <span className="text-slate-300">Production health</span>
        </label>

        <div className="col-span-2 text-[10px] text-slate-500">
          Stop on FAIL · Stop on scope_mismatch · Require scope consistency —
          항상 활성화 (편집 불가)
        </div>
      </div>

      <div className="mt-1 flex gap-2">
        <button
          type="button"
          onClick={handleStart}
          disabled={!canStart}
          className={
            "flex-1 rounded-lg px-3 py-1.5 text-[12px] font-semibold " +
            (canStart
              ? "bg-amber-500 text-slate-950 hover:bg-amber-400"
              : "cursor-not-allowed bg-slate-800/60 text-slate-500")
          }
        >
          {isRunning ? "실행 중" : "Auto Pilot 시작"}
        </button>
        <button
          type="button"
          onClick={handleStop}
          disabled={!canStop}
          className={
            "flex-1 rounded-lg px-3 py-1.5 text-[12px] font-semibold " +
            (canStop
              ? "bg-rose-500 text-slate-50 hover:bg-rose-400"
              : "cursor-not-allowed bg-slate-800/60 text-slate-500")
          }
        >
          정지
        </button>
      </div>

      {feedback ? (
        <div className="rounded bg-slate-900/80 px-2 py-1 text-[11px] text-amber-200">
          {feedback}
        </div>
      ) : null}

      <div className="mt-1 flex flex-col gap-1 rounded bg-slate-900/60 p-2">
        <StatRow label="현재 cycle" value={`${cycleCount} / ${totalCycles}`} />
        <StatRow label="시작" value={fmtIso(state?.started_at)} />
        <StatRow label="종료" value={fmtIso(state?.ended_at)} />
        <StatRow
          label="마지막 verdict"
          value={
            lastFailure
              ? `${lastVerdict} (${lastFailure})`
              : lastVerdict
          }
        />
        <StatRow label="마지막 commit" value={lastCommit} mono />
        <StatRow label="마지막 push" value={lastPush} />
        <StatRow label="마지막 render" value={lastRender} />
        <StatRow label="마지막 health" value={lastHealth} />
        <StatRow label="정지 사유" value={stopReason} />
        <StatRow label="report" value={reportPath} mono />
      </div>
    </section>
  );
}
