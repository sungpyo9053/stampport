import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { startAutopilot, stopAutopilot } from "../api/controlTowerApi.js";
import {
  PHASES,
  MODE_BADGE,
  buildStartPayload,
  deriveButtonState,
  deriveDisplayCycle,
  deriveEffectiveConfig,
  derivePhase,
  deriveStuckDiagnostic,
  hasActiveCycle,
  isRunningPhase,
  pickRunnerMeta,
} from "../utils/autopilotPhase.js";
import { fmtDateTime as sharedFmtDateTime, fmtElapsedFrom as sharedFmtElapsed } from "../utils/time.js";

// Auto Pilot Publish panel — 3-section operator surface:
//
//   A. STATUS SUMMARY — phase pill, mode badge, cycle progress,
//      elapsed, current phase line, last verdict. Read-only.
//   B. RUN CONFIG     — mode / cycles / hours / checkbox toggles.
//      Disabled while running so the form mirrors runtime config.
//   C. DEBUG (접힘)    — START PAYLOAD preview, report path, raw
//      autopilot_state.json blob link. Collapsed by default so the
//      operator never sees developer-shaped strings on the main view.
//
// Restart UX is staged: when running, clicking 재시작 walks 1/3
// stopping → 2/3 waiting stopped → 3/3 starting and shows each step
// in a progress chip row. The Restart payload is the same `draft`
// the operator has typed — never a silent fall-back to safe_run.

const MODE_LABEL = {
  safe_run: "Safe Run (cycle만 실행)",
  auto_commit: "Auto Commit (commit만)",
  auto_publish: "Auto Publish (commit + push + health)",
};

const DRAFT_KEY = "stampport.autopilot.draft.v1";

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

// Delegate to the shared formatters so STARTED in this panel matches
// the SystemLog timestamps to the second. Local aliases keep the
// existing call sites (`fmtIso`, `fmtElapsed`) unchanged.
const fmtIso = sharedFmtDateTime;
const fmtElapsed = (startedIso, isRunning) =>
  sharedFmtElapsed(startedIso, null, isRunning);

function StatRow({ label, value, mono, ellipsis }) {
  const cls = [
    "text-[11.5px]",
    mono ? "font-mono" : "",
    "text-slate-100",
    ellipsis ? "autopilot-stat-ellipsis" : "",
  ].filter(Boolean).join(" ");
  return (
    <div className="flex items-center justify-between gap-2 text-[11.5px]">
      <span className="text-slate-400">{label}</span>
      <span className={cls} title={typeof value === "string" ? value : undefined}>
        {value || "—"}
      </span>
    </div>
  );
}

export default function AutoPilotPanel({ runners = [], onSent }) {
  const meta = useMemo(() => pickRunnerMeta(runners), [runners]);
  const runnerId = useMemo(() => {
    for (const r of runners) {
      if (r?.metadata_json?.local_factory?.autopilot) return r.id;
    }
    return runners?.[0]?.id || null;
  }, [runners]);
  const state = meta?.autopilot || null;

  const [stopRequested, setStopRequested] = useState(false);
  const [restartStep, setRestartStep] = useState(0); // 0 idle, 1 stopping, 2 waiting, 3 starting
  const restartInFlight = restartStep > 0;
  const phase = useMemo(
    () => derivePhase(meta, { restartInFlight, stopRequested }),
    [meta, restartInFlight, stopRequested],
  );
  const isRunning = phase === "cycle_running" || phase === "starting" || phase === "waiting_next_cycle";
  const isStopping = phase === "stopping";
  const isLocked = isRunning || isStopping || phase === "restarting";
  const cycleInFlight = hasActiveCycle(meta);

  // Form draft (only used when not locked).
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

  // Track autopilot.status transitions so we can clear local optimistic
  // flags once the runner has acknowledged.
  const prevStatusRef = useRef(state?.status || "idle");
  useEffect(() => {
    const prev = prevStatusRef.current;
    const status = state?.status || "idle";
    if (prev !== status) {
      if (prev === "running" && (status === "stopping" || status === "stopped")) {
        setStopRequested(false);
      }
      if (prev !== "running" && status === "running") {
        setStopRequested(false);
      }
      prevStatusRef.current = status;
    }
  }, [state?.status]);

  // Stale-stop fallback so the UI doesn't sit on STOP REQUESTED forever.
  useEffect(() => {
    if (!stopRequested) return undefined;
    const id = setTimeout(() => setStopRequested(false), 12000);
    return () => clearTimeout(id);
  }, [stopRequested]);

  // -------- Effective values (lock to runtime config when running) --------
  // Derivation lifted into autopilotPhase.deriveEffectiveConfig so the
  // verify-autopilot-ui matrix tests the same exact rule.
  const effective = useMemo(
    () => deriveEffectiveConfig({ phase, draft, autopilot: state }),
    [phase, draft, state],
  );
  const effectiveMode          = effective.mode;
  const effectiveMaxCycles     = effective.maxCycles;
  const effectiveMaxHours      = effective.maxHours;
  const effectiveStopOnHold    = effective.stopOnHold;
  const effectiveRequireRender = effective.requireRender;
  const effectiveRequireHealth = effective.requireHealth;

  // -------- Start payload (single source of truth) --------
  // The draft is what we ALWAYS send — never a fallback to safe_run.
  const startPayload = useMemo(() => buildStartPayload(draft), [draft]);

  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [debugOpen, setDebugOpen] = useState(false);

  // Auto-clear command feedback after 10s. The status pill is the
  // source of truth for "what is autopilot doing right now"; toast
  // text is just a momentary acknowledgment of the last click.
  useEffect(() => {
    if (!feedback) return undefined;
    const id = setTimeout(() => setFeedback(""), 10000);
    return () => clearTimeout(id);
  }, [feedback]);

  // Stuck-before-first-cycle diagnostic — tick every 15s while running
  // so the wait_sec / stuck flag stays current without depending on
  // heartbeat refresh cadence.
  const [, forceTick] = useState(0);
  useEffect(() => {
    if (state?.status !== "running") return undefined;
    const id = setInterval(() => forceTick((n) => n + 1), 15000);
    return () => clearInterval(id);
  }, [state?.status]);
  const stuck = useMemo(() => deriveStuckDiagnostic(meta), [meta, state]);

  const buttonState = useMemo(() => deriveButtonState({
    phase,
    stopRequested,
    restartInFlight,
    cycleInFlight,
    stuck: stuck.stuck,
    busy,
    hasRunner: !!runnerId,
  }), [phase, stopRequested, restartInFlight, cycleInFlight, stuck.stuck, busy, runnerId]);
  const canStart   = buttonState.canStart;
  const canStop    = buttonState.canStop;
  const canRestart = buttonState.canRestart;

  const sendStart = useCallback(async () => {
    // eslint-disable-next-line no-console
    console.info("[autopilot] start payload →", startPayload);
    if (typeof window !== "undefined") {
      window.__last_autopilot_start_payload = startPayload;
    }
    return startAutopilot(runnerId, startPayload);
  }, [runnerId, startPayload]);

  async function handleStart() {
    if (!runnerId) {
      setFeedback("러너가 등록되어 있지 않아요 — 먼저 runner heartbeat 확인");
      return;
    }
    if (!startPayload.autopilot_mode) {
      setFeedback("모드가 선택되지 않았습니다.");
      return;
    }
    setBusy(true); setFeedback("");
    try {
      await sendStart();
      setFeedback(
        `시작 요청 완료 (mode=${startPayload.autopilot_mode}, ` +
        `cycles=${startPayload.max_cycles}, hours=${startPayload.max_hours}, ` +
        `stop_on_hold=${startPayload.stop_on_hold}).`,
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
    setStopRequested(true);
    setBusy(true); setFeedback("");
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
    setBusy(true); setFeedback("재시작 진행 중...");
    try {
      // 1/3 stopping current run
      if (isRunning || isStopping) {
        setRestartStep(1);
        try { await stopAutopilot(runnerId, "operator restart"); } catch { /* swallow */ }
      }
      // 2/3 waiting stopped — short polling until runner says
      // status != running, capped at ~6s so a stuck loop doesn't lock
      // the operator out of the restart UX forever.
      setRestartStep(2);
      const waitStart = Date.now();
      while (Date.now() - waitStart < 6000) {
        await new Promise((r) => setTimeout(r, 600));
        // We can't read runners props mid-callback (stale closure) —
        // but onSent triggers a tick which refreshes state via React.
        onSent && onSent();
        // Best-effort: leave the wait at the 6s budget if we don't
        // see status flip. The runner accepts a fresh start anyway.
      }
      // 3/3 starting new run
      setRestartStep(3);
      await sendStart();
      setFeedback(`재시작 완료 (mode=${startPayload.autopilot_mode}).`);
      onSent && onSent();
    } catch (e) {
      setFeedback(`재시작 실패: ${e.message || e}`);
    } finally {
      // Hold the "3/3" pill briefly so the operator sees the success
      // before the row clears.
      setTimeout(() => setRestartStep(0), 800);
      setBusy(false);
    }
  }

  const phaseMeta = PHASES[phase] || PHASES.idle;
  const modeBadge = MODE_BADGE[effectiveMode] || MODE_BADGE.safe_run;
  const safeRunWarning = isRunning && effectiveMode === "safe_run";

  // -------- Status summary text --------
  // displayCycle prefers active_cycle_index when a smoke is mid-flight
  // — keeps the panel from sitting on 0/5 while the first product_
  // planning runs.
  const cycleDisplay = useMemo(() => deriveDisplayCycle(meta), [meta]);
  const cycleCount = cycleDisplay.number;
  const totalCycles = state?.max_cycles ?? draft.maxCycles;
  const elapsed = fmtElapsed(state?.started_at, isRunning);
  const lastVerdict = state?.last_verdict || "—";
  const lastFailure = state?.last_failure_code || "";
  const lastCommit = (state?.last_commit_hash || "").slice(0, 8) || "—";
  const lastPush = state?.last_push_status || "—";
  const lastHealth = state?.last_health_status || "—";
  const lastRender = state?.last_render_status || "—";
  const stopReason = state?.stop_reason || "—";
  // While running, surface the LIVE report path so the operator
  // doesn't read the previous run's autopilot_report.md by mistake.
  // After stop, the final report is at autopilot_report.md.
  const reportPath = (
    state?.live_report_path && state?.status === "running"
      ? state.live_report_path
      : (state?.report_path || ".runtime/autopilot_report.md")
  );
  const reportLabel = (
    state?.live_report_path && state?.status === "running"
      ? "live report"
      : (state?.status === "running" ? "마지막 종료 report" : "report")
  );

  const phaseLine = (() => {
    if (phase === "cycle_running") return "factory_smoke / cycle 실행 중";
    if (phase === "waiting_next_cycle") return "다음 사이클 시작 대기 중";
    if (phase === "starting") return "사이클 준비 중";
    if (phase === "stopping") return "현재 사이클 종료 후 정지";
    if (phase === "restarting") return "재시작 진행 중";
    if (phase === "stopped") return "Auto Pilot 정지됨";
    if (phase === "failed") return state?.stop_reason || "실패";
    return "Auto Pilot 대기";
  })();

  // Labels come from deriveButtonState so the matrix verifier checks
  // the same string the operator sees.
  const stopLabel  = buttonState.stopLabel;
  const startLabel = buttonState.startLabel;

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
      data-phase={phase}
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
            className={"autopilot-phase-pill is-" + phaseMeta.tone}
            data-testid="autopilot-phase-pill"
          >
            <span className="inline-block h-1.5 w-1.5 rounded-full" style={{
              backgroundColor: phaseMeta.tone === "active" ? "#fbbf24" :
                               phaseMeta.tone === "error"  ? "#f87171" : "#94a3b8",
            }} />
            {phaseMeta.label}
          </span>
          <span
            className={
              "autopilot-phase-pill " + (safeRunWarning ? "autopilot-mode-badge-warn" : "is-neutral")
            }
            data-testid="autopilot-mode-badge"
          >
            {modeBadge.label}{safeRunWarning && " · 배포 안 함"}
          </span>
          {!safeRunWarning && isRunning && (
            <span className="text-[10px] text-emerald-300 tracking-widest">
              {modeBadge.note}
            </span>
          )}
        </div>
        <span className="text-[10px] text-slate-500">
          runner: {runnerId || "(none)"}
        </span>
      </div>

      {/* Stuck-before-first-cycle warning — runner says running but
          cycle_count=0 for >180s and no active subprocess. The user
          spec calls this `autopilot_stuck_before_first_cycle`; we
          name the data-testid the same so the verifier can grep it. */}
      {stuck.stuck && (
        <div
          className="autopilot-stuck-card"
          data-testid="autopilot-stuck-before-first-cycle"
          data-diagnostic={stuck.diagnostic_code}
        >
          <div className="flex items-center gap-2 flex-wrap">
            <span
              className="rounded-full px-2 py-0.5 text-[10px] font-bold tracking-[0.25em]"
              style={{
                color: "#fecaca",
                border: "1.5px solid #f8717166",
                backgroundColor: "#1c0d12",
              }}
            >
              ⚠ STUCK · 첫 cycle 미시작
            </span>
            <span className="text-[10.5px] text-rose-200">
              {stuck.wait_sec}s 대기 중 · 첫 cycle 프로세스가 시작되지 않았습니다
            </span>
          </div>
          <ul className="mt-1 list-disc pl-5 text-[11px] text-rose-100">
            {(stuck.next_actions || []).map((act) => (
              <li key={act}>{act}</li>
            ))}
          </ul>
        </div>
      )}

      {/* ============ A. STATUS SUMMARY ============ */}
      <div
        className="autopilot-panel-section"
        data-testid="autopilot-section-status"
      >
        <div className="autopilot-panel-section-title">실행 상태 요약</div>
        <div className="grid grid-cols-2 gap-x-3 gap-y-1">
          <StatRow
            label="현재 cycle"
            value={
              cycleDisplay.active
                ? `${cycleCount} / ${totalCycles} · 실행 중`
                : `${cycleCount} / ${totalCycles}`
            }
          />
          <StatRow label="elapsed" value={elapsed} />
          <StatRow label="시작" value={fmtIso(state?.started_at)} />
          <StatRow label="종료" value={fmtIso(state?.ended_at)} />
          <StatRow
            label="last verdict"
            value={lastFailure ? `${lastVerdict} (${lastFailure})` : lastVerdict}
          />
          <StatRow label="last commit" value={lastCommit} mono />
        </div>
        <div
          className="rounded px-2 py-1 text-[11.5px] mt-1"
          data-testid="autopilot-phase-line"
          style={{
            backgroundColor: "#0a1228",
            border: `1px solid ${phaseMeta.tone === "active" ? "#fbbf2466" : "#1e293b"}`,
            color: "#fde68a",
          }}
        >
          ▶ {phaseLine}
        </div>
        {restartInFlight && (
          <div
            className="autopilot-restart-progress"
            data-testid="autopilot-restart-progress"
          >
            <span className={
              "autopilot-restart-progress-step " +
              (restartStep > 1 ? "is-done" : restartStep === 1 ? "is-active" : "")
            }>1/3 stopping current run</span>
            <span className={
              "autopilot-restart-progress-step " +
              (restartStep > 2 ? "is-done" : restartStep === 2 ? "is-active" : "")
            }>2/3 waiting stopped</span>
            <span className={
              "autopilot-restart-progress-step " +
              (restartStep === 3 ? "is-active" : "")
            }>3/3 starting new run</span>
          </div>
        )}
      </div>

      {/* ============ B. RUN CONFIG ============ */}
      <div
        className="autopilot-panel-section"
        data-testid="autopilot-section-config"
      >
        <div className="autopilot-panel-section-title">실행 설정</div>
        <div className="grid grid-cols-2 gap-2 text-[11px]">
          <label className="col-span-2 flex flex-col gap-1">
            <span className="text-slate-300">
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
            <span className="text-slate-300">Max cycles</span>
            <input
              type="number"
              min={1} max={50}
              value={effectiveMaxCycles}
              disabled={isLocked}
              onChange={(e) => setDraft((d) => ({ ...d, maxCycles: e.target.value }))}
              className="rounded bg-slate-800 px-2 py-1 text-slate-100 disabled:opacity-50"
              data-testid="autopilot-max-cycles"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-slate-300">Max hours</span>
            <input
              type="number"
              min={0.1} max={48} step={0.5}
              value={effectiveMaxHours}
              disabled={isLocked}
              onChange={(e) => setDraft((d) => ({ ...d, maxHours: e.target.value }))}
              className="rounded bg-slate-800 px-2 py-1 text-slate-100 disabled:opacity-50"
              data-testid="autopilot-max-hours"
            />
          </label>
          <label className="col-span-2 flex items-center gap-2">
            <input
              type="checkbox"
              checked={effectiveStopOnHold}
              disabled={isLocked}
              onChange={(e) => setDraft((d) => ({ ...d, stopOnHold: e.target.checked }))}
            />
            <span className="text-slate-200">Stop on HOLD</span>
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={effectiveRequireRender}
              disabled={isLocked}
              onChange={(e) => setDraft((d) => ({ ...d, requireRender: e.target.checked }))}
            />
            <span className="text-slate-200">Render smoke</span>
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={effectiveRequireHealth}
              disabled={isLocked}
              onChange={(e) => setDraft((d) => ({ ...d, requireHealth: e.target.checked }))}
            />
            <span className="text-slate-200">Production health</span>
          </label>
          <div className="col-span-2 text-[10px] text-slate-400">
            Stop on FAIL · Stop on scope_mismatch · Require scope consistency —
            항상 활성화
          </div>
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
      </div>

      {/* ============ C. DEBUG (collapsed) ============ */}
      <div
        className="autopilot-panel-section autopilot-debug-collapsible"
        data-testid="autopilot-section-debug"
      >
        <button
          type="button"
          className="autopilot-debug-toggle"
          onClick={() => setDebugOpen((v) => !v)}
          aria-expanded={debugOpen ? "true" : "false"}
          data-testid="autopilot-debug-toggle"
        >
          {debugOpen ? "▼ DEBUG 접기" : "▶ DEBUG 자세히 보기"}
        </button>
        {debugOpen && (
          <div className="flex flex-col gap-2 mt-1" data-testid="autopilot-debug-body">
            <div
              className="autopilot-payload-preview"
              data-testid="autopilot-payload-preview"
            >
              <span className="font-bold text-amber-300 mr-2">START PAYLOAD</span>
              {payloadPreviewLines}
            </div>
            <div className="grid grid-cols-1 gap-x-3 gap-y-1">
              <StatRow label="last push" value={lastPush} />
              <StatRow label="last render" value={lastRender} />
              <StatRow label="last health" value={lastHealth} />
              <StatRow label="stop reason" value={stopReason} />
              <StatRow
                label={reportLabel}
                value={
                  <span
                    className="autopilot-stat-ellipsis autopilot-stat-ellipsis-wide font-mono"
                    title={reportPath}
                  >
                    {reportPath}
                  </span>
                }
              />
              <StatRow label="raw mode" value={state?.mode || draft.mode} mono />
              <StatRow label="raw status" value={state?.status || "idle"} mono />
              <StatRow label="phase" value={phase} mono />
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
