import { useEffect, useMemo, useRef, useState } from "react";
import { startAutopilot, stopAutopilot } from "../api/controlTowerApi.js";

// Auto Pilot Publish panel.
//
// Reads from runners[].metadata_json.local_factory.autopilot — written
// by control_tower/local_runner/runner.py:_build_autopilot_meta which
// in turn surfaces .runtime/autopilot_state.json. Posts start/stop
// commands through the runner queue (allowlisted as start_autopilot /
// stop_autopilot in schemas.py).
//
// Two visual modes:
//   - Idle / stopped / failed → form is editable, defaults from a
//     localStorage-saved draft (so the operator's last config sticks).
//   - Running / stopping     → inputs are disabled and bound to the
//     ACTUAL runtime config (autopilot_state.json), not the draft.
//     Mode select must NOT flip back to safe_run while running.

const MODE_LABEL = {
  safe_run: "Safe Run (cycle만 실행)",
  auto_commit: "Auto Commit (commit만)",
  auto_publish: "Auto Publish (commit + push + health)",
};

const STATUS_TONE = {
  idle:     { color: "#64748b", label: "대기" },
  running:  { color: "#34d399", label: "실행 중" },
  stopping: { color: "#fbbf24", label: "정지 중" },
  stopped:  { color: "#94a3b8", label: "정지" },
  failed:   { color: "#f87171", label: "실패" },
};

const DRAFT_KEY = "stampport.autopilot.draft.v1";

function pickAutopilot(runners = []) {
  for (const r of runners) {
    const ap = r?.metadata_json?.local_factory?.autopilot;
    if (ap) return { runnerId: r.id, state: ap };
  }
  return { runnerId: runners?.[0]?.id || null, state: null };
}

function loadDraft() {
  if (typeof window === "undefined") return {};
  try {
    return JSON.parse(window.localStorage.getItem(DRAFT_KEY) || "{}");
  } catch {
    return {};
  }
}

function saveDraft(draft) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
  } catch {
    /* quota / private-mode — ignore */
  }
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

  const status = state?.status || "idle";
  const isRunning = status === "running";
  const isStopping = status === "stopping";
  const isLocked = isRunning || isStopping;

  // Draft (idle defaults) — only used when not locked. The form values
  // below READ from running state when locked; the draft is what the
  // operator was last typing in.
  const [draft, setDraft] = useState(() => {
    const d = loadDraft();
    return {
      mode: d.mode || "safe_run",
      maxCycles: d.maxCycles || 5,
      maxHours: d.maxHours || 6,
      stopOnHold: d.stopOnHold !== undefined ? !!d.stopOnHold : true,
      requireRender: d.requireRender !== undefined ? !!d.requireRender : true,
      requireHealth: d.requireHealth !== undefined ? !!d.requireHealth : true,
    };
  });

  useEffect(() => {
    if (!isLocked) saveDraft(draft);
  }, [draft, isLocked]);

  // Local "stop request in flight" state — flips on click and is
  // cleared once the runner reports status=stopping or stopped.
  const [stopRequested, setStopRequested] = useState(false);
  const prevStatusRef = useRef(status);
  useEffect(() => {
    const prev = prevStatusRef.current;
    if (prev !== status) {
      // Once the runner has acknowledged (status moved off "running"
      // toward stopping/stopped), clear the local "requested" flag.
      if (prev === "running" && (status === "stopping" || status === "stopped")) {
        setStopRequested(false);
      }
      // If the runner reports running fresh (e.g. user started again),
      // clear stale stop request state too.
      if (prev !== "running" && status === "running") {
        setStopRequested(false);
      }
      prevStatusRef.current = status;
    }
  }, [status]);

  const tone = STATUS_TONE[status] || STATUS_TONE.idle;
  const cycleCount = state?.cycle_count ?? 0;
  const totalCycles = state?.max_cycles ?? draft.maxCycles;

  // Effective values: when locked, mirror runtime state so the UI shows
  // what the autopilot is *actually* using — not whatever the operator
  // happened to type before they hit start.
  const effectiveMode = isLocked
    ? (state?.mode || draft.mode)
    : draft.mode;
  const effectiveMaxCycles = isLocked
    ? (state?.max_cycles ?? draft.maxCycles)
    : draft.maxCycles;
  const effectiveMaxHours = isLocked
    ? (state?.max_hours ?? draft.maxHours)
    : draft.maxHours;
  const effectiveStopOnHold = isLocked
    ? (state?.stop_on_hold !== undefined ? !!state.stop_on_hold : draft.stopOnHold)
    : draft.stopOnHold;
  const effectiveRequireRender = isLocked
    ? (state?.require_render_check !== undefined
        ? !!state.require_render_check
        : draft.requireRender)
    : draft.requireRender;
  const effectiveRequireHealth = isLocked
    ? (state?.require_api_health !== undefined
        ? !!state.require_api_health
        : draft.requireHealth)
    : draft.requireHealth;

  const lastVerdict = state?.last_verdict || "—";
  const lastFailure = state?.last_failure_code || "";
  const lastCommit = (state?.last_commit_hash || "").slice(0, 8) || "—";
  const lastPush = state?.last_push_status || "—";
  const lastHealth = state?.last_health_status || "—";
  const lastRender = state?.last_render_status || "—";
  const stopReason = state?.stop_reason || "—";
  const reportPath = state?.report_path || ".runtime/autopilot_report.md";

  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState("");

  // Build the start payload from the operator's draft. Single source
  // of truth — both the live "what will be sent" preview pill AND the
  // actual handleStart() POST read from this. That way the bug where
  // the select said auto_publish but the POST sent safe_run can never
  // come back: if the preview shows safe_run, you know the click will
  // send safe_run, period.
  const startPayload = useMemo(() => ({
    autopilot_enabled: true,
    // Send under both keys for defence-in-depth: the runner accepts
    // either `autopilot_mode` (canonical) or `mode` (back-compat).
    autopilot_mode: draft.mode,
    mode: draft.mode,
    max_cycles: Number(draft.maxCycles) || 5,
    max_hours: Number(draft.maxHours) || 6,
    stop_on_hold: !!draft.stopOnHold,
    require_scope_consistency: true,
    require_render_check: !!draft.requireRender,
    require_api_health: !!draft.requireHealth,
  }), [draft]);

  // The runner has an active cycle process iff cycle_count > 0 AND
  // status==running. If status==running but cycle_count is still 0
  // (start command claimed but no smoke spawned yet), the "현재 cycle
  // 종료 후 정지" label is misleading — there's nothing in flight to
  // wait for. The "stopped acknowledged" check, plus a quick fallback
  // that auto-clears stopRequested after ~10s if the runner refuses to
  // confirm, prevents the UI from sticking on RUNNING/STOP REQUESTED
  // forever.
  const cycleInFlight = isRunning && (state?.cycle_count || 0) > 0;
  useEffect(() => {
    if (!stopRequested) return undefined;
    // If the runner has already moved off `running`, the other effect
    // above will clear the flag; this one only handles the silent-
    // refusal case where the heartbeat keeps lying about running.
    const id = setTimeout(() => {
      // Only auto-clear if we're STILL in the requested state — the
      // user shouldn't see the flag bounce if a real status update
      // arrived in the meantime.
      setStopRequested(false);
    }, 12000);
    return () => clearTimeout(id);
  }, [stopRequested]);

  const canStart = !!runnerId && !busy && !isLocked && !stopRequested;
  const canStop = !!runnerId && !busy && (isRunning || isStopping) && !stopRequested;
  // Restart is always offered when a runner is registered — it stops
  // first if running, then starts. If the runner is already stopped
  // it's effectively a Start with the current draft.
  const canRestart = !!runnerId && !busy && !stopRequested;

  async function handleStart() {
    if (!runnerId) {
      setFeedback("러너가 등록되어 있지 않아요 — 먼저 runner heartbeat 확인");
      return;
    }
    if (!startPayload.autopilot_mode) {
      setFeedback("모드가 선택되지 않았습니다 — Safe Run / Auto Commit / Auto Publish 중 하나를 선택해 주세요.");
      return;
    }
    setBusy(true);
    setFeedback("");
    try {
      // Persist the payload to console + a runtime debug surface so an
      // operator can verify "what did the dashboard actually send?"
      // without sniffing the network tab.
      // eslint-disable-next-line no-console
      console.info("[autopilot] start payload →", startPayload);
      if (typeof window !== "undefined") {
        window.__last_autopilot_start_payload = startPayload;
      }
      await startAutopilot(runnerId, startPayload);
      setFeedback(
        `Auto Pilot 시작 요청 완료 (mode=${startPayload.autopilot_mode}, ` +
        `cycles=${startPayload.max_cycles}, hours=${startPayload.max_hours}, ` +
        `stop_on_hold=${startPayload.stop_on_hold}). 다음 heartbeat 에서 갱신됩니다.`,
      );
      onSent && onSent();
    } catch (e) {
      setFeedback(`시작 실패: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleStop() {
    if (!runnerId) return;
    // Optimistic: flip the UI immediately so the operator sees "정지
    // 요청 중" without waiting for the next heartbeat.
    setStopRequested(true);
    setBusy(true);
    setFeedback("");
    try {
      await stopAutopilot(runnerId, "operator stop from dashboard");
      setFeedback(
        cycleInFlight
          ? "Auto Pilot 정지 요청 완료 — 현재 cycle 종료를 기다립니다."
          : "Auto Pilot 정지 요청 완료.",
      );
      onSent && onSent();
    } catch (e) {
      setStopRequested(false);
      setFeedback(`정지 실패: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleRestart() {
    if (!runnerId) return;
    setBusy(true);
    setFeedback("Auto Pilot 재시작 요청 — 정지 후 새 설정으로 다시 시작합니다.");
    try {
      if (isRunning || isStopping) {
        await stopAutopilot(runnerId, "operator restart");
        // Wait briefly for the runner to acknowledge the stop. The
        // autopilot module checks if the loop is alive before it
        // accepts a new start, so we give it a beat.
        await new Promise((resolve) => setTimeout(resolve, 800));
      }
      // eslint-disable-next-line no-console
      console.info("[autopilot] restart payload →", startPayload);
      if (typeof window !== "undefined") {
        window.__last_autopilot_start_payload = startPayload;
      }
      await startAutopilot(runnerId, startPayload);
      setFeedback(
        `Auto Pilot 재시작 완료 (mode=${startPayload.autopilot_mode}).`,
      );
      onSent && onSent();
    } catch (e) {
      setFeedback(`재시작 실패: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }

  // Compose stop button label so the operator always sees the right
  // phase. Three states the runner can actually be in (running with
  // active cycle / running with no cycle yet / stopped) → three
  // distinct labels. Don't say "현재 cycle 종료 후 정지" when there's
  // no cycle in flight to wait for.
  let stopLabel = "정지";
  if (stopRequested && cycleInFlight) {
    stopLabel = "정지 요청 중 — 현재 cycle 종료 후 정지";
  } else if (stopRequested) {
    stopLabel = "정지 요청 중...";
  } else if (isStopping) {
    stopLabel = "현재 cycle 종료 후 정지";
  } else if (status === "stopped") {
    stopLabel = "정지됨";
  }

  const startLabel = (() => {
    if (busy) return "처리 중...";
    if (isRunning) return "실행 중";
    if (isStopping) return "정지 중";
    return "Auto Pilot 시작";
  })();

  // Pretty-print the payload for the operator-facing preview pill.
  const payloadPreviewLines = [
    `mode=${startPayload.autopilot_mode}`,
    `max_cycles=${startPayload.max_cycles}`,
    `max_hours=${startPayload.max_hours}`,
    `stop_on_hold=${startPayload.stop_on_hold}`,
    `render=${startPayload.require_render_check}`,
    `health=${startPayload.require_api_health}`,
  ].join(" · ");

  return (
    <section
      className="autopilot-panel flex flex-col gap-2 p-3"
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
          {stopRequested && (
            <span
              className="rounded px-1.5 py-0.5 text-[9px] font-bold tracking-widest"
              style={{
                color: "#fbbf24",
                border: "1px solid #fbbf2466",
                backgroundColor: "#0a1228",
              }}
            >
              STOP REQUESTED
            </span>
          )}
        </div>
        <span className="text-[10px] text-slate-500">
          runner: {runnerId || "(none)"}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-2 text-[11px]">
        <label className="col-span-2 flex flex-col gap-1">
          <span className="text-slate-400">
            모드{isLocked && (
              <span className="ml-2 text-[9px] tracking-widest text-emerald-400">
                · runtime config
              </span>
            )}
          </span>
          <select
            value={effectiveMode}
            disabled={isLocked}
            onChange={(e) => setDraft((d) => ({ ...d, mode: e.target.value }))}
            className="rounded bg-slate-800 px-2 py-1 text-slate-100 disabled:opacity-50"
            data-testid="autopilot-mode-select"
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
            value={effectiveMaxCycles}
            disabled={isLocked}
            onChange={(e) =>
              setDraft((d) => ({ ...d, maxCycles: e.target.value }))
            }
            className="rounded bg-slate-800 px-2 py-1 text-slate-100 disabled:opacity-50"
            data-testid="autopilot-max-cycles"
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-slate-400">Max hours</span>
          <input
            type="number"
            min={0.1}
            max={48}
            step={0.5}
            value={effectiveMaxHours}
            disabled={isLocked}
            onChange={(e) =>
              setDraft((d) => ({ ...d, maxHours: e.target.value }))
            }
            className="rounded bg-slate-800 px-2 py-1 text-slate-100 disabled:opacity-50"
            data-testid="autopilot-max-hours"
          />
        </label>

        <label className="col-span-2 flex items-center gap-2">
          <input
            type="checkbox"
            checked={effectiveStopOnHold}
            disabled={isLocked}
            onChange={(e) =>
              setDraft((d) => ({ ...d, stopOnHold: e.target.checked }))
            }
          />
          <span className="text-slate-300">Stop on HOLD</span>
        </label>

        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={effectiveRequireRender}
            disabled={isLocked}
            onChange={(e) =>
              setDraft((d) => ({ ...d, requireRender: e.target.checked }))
            }
          />
          <span className="text-slate-300">Render smoke</span>
        </label>

        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={effectiveRequireHealth}
            disabled={isLocked}
            onChange={(e) =>
              setDraft((d) => ({ ...d, requireHealth: e.target.checked }))
            }
          />
          <span className="text-slate-300">Production health</span>
        </label>

        <div className="col-span-2 text-[10px] text-slate-500">
          Stop on FAIL · Stop on scope_mismatch · Require scope consistency —
          항상 활성화 (편집 불가)
        </div>
      </div>

      {/* Live "what will be sent" preview — eliminates the silent
          safe_run regression by making the payload visible BEFORE the
          operator clicks Start. */}
      <div
        className="rounded px-2 py-1.5 text-[10px] tracking-widest"
        data-testid="autopilot-payload-preview"
        style={{
          backgroundColor: "#0a1228",
          border: "1px dashed #d4a84355",
          color: "#fde68a",
          fontFamily: "ui-monospace, monospace",
        }}
      >
        <span className="mr-2 font-bold text-amber-300">START PAYLOAD</span>
        <span style={{ wordBreak: "break-word" }}>{payloadPreviewLines}</span>
      </div>

      <div className="mt-1 flex flex-wrap gap-2">
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
          data-testid="autopilot-start"
        >
          {startLabel}
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
          data-testid="autopilot-stop"
        >
          {stopLabel}
        </button>
        <button
          type="button"
          onClick={handleRestart}
          disabled={!canRestart}
          className={
            "flex-1 rounded-lg px-3 py-1.5 text-[12px] font-semibold " +
            (canRestart
              ? "bg-sky-500 text-slate-950 hover:bg-sky-400"
              : "cursor-not-allowed bg-slate-800/60 text-slate-500")
          }
          data-testid="autopilot-restart"
        >
          재시작
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
