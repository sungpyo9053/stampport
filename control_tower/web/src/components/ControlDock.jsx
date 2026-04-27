import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import {
  pauseFactory,
  resumeFactory,
  resetFactory,
  sendRunnerCommand,
  setFactoryContinuous,
  startFactory,
  stopFactory,
} from "../api/controlTowerApi.js";
import ReleasePreviewPanel from "./ReleasePreviewPanel.jsx";
import CycleChangePanel from "./CycleChangePanel.jsx";

// In-flight guard for the deploy button. Module-scoped so it survives
// re-renders and double-mounts in StrictMode without sliding into a
// duplicate enqueue. The Promise is set when the click handler fires
// and cleared when the request resolves (or after DEPLOY_GUARD_MS as a
// belt-and-braces timeout for a hung enqueue).
const DEPLOY_GUARD_MS = 90_000;
let _deployInflight = null;
let _deployInflightSince = 0;

function _deployGuardActive() {
  if (!_deployInflight) return false;
  if (Date.now() - _deployInflightSince > DEPLOY_GUARD_MS) {
    _deployInflight = null;
    _deployInflightSince = 0;
    return false;
  }
  return true;
}

// Local "queued" flag — tracks the brief gap between "user clicked
// 배포" and "runner heartbeat reports command_received". Module-scoped
// so it survives StrictMode's dev double-mount. Capped at 30s so we
// don't get stuck if the runner went offline between the click and
// the next poll.
const DEPLOY_QUEUED_MS = 30_000;
let _deployQueuedSince = 0;
function _deployQueuedActive() {
  if (!_deployQueuedSince) return false;
  if (Date.now() - _deployQueuedSince > DEPLOY_QUEUED_MS) {
    _deployQueuedSince = 0;
    return false;
  }
  return true;
}

// Statuses considered "deploy is currently moving" — when any of
// these are present, the button must stay disabled regardless of
// runner.current_command (which clears the moment the handler returns
// even though GitHub Actions is still building).
const DEPLOY_INFLIGHT_STATUSES = new Set([
  "queued",
  "command_received",
  "validating",
  "committing",
  "pushing",
]);

// Stepper definition. Order matters — the panel walks left to right
// and marks each step pending / running / success / failed based on
// the current deploy_progress.status (mapped through STATUS_TO_INDEX).
const DEPLOY_STEPS = [
  { key: "queued",            label: "배포 요청 전송" },
  { key: "command_received",  label: "Runner 명령 수신" },
  { key: "validating",        label: "검증 (Safety Gate / QA)" },
  { key: "committing",        label: "git commit" },
  { key: "pushing",           label: "git push origin main" },
  { key: "actions_triggered", label: "GitHub Actions 트리거" },
  { key: "deploying",         label: "서버 배포" },
  { key: "completed",         label: "배포 완료" },
];
const STATUS_TO_INDEX = Object.fromEntries(
  DEPLOY_STEPS.map((s, i) => [s.key, i]),
);

function _stepIndex(status) {
  if (status == null) return -1;
  if (status === "idle") return -1;
  if (status === "failed") return -1;
  return STATUS_TO_INDEX[status] ?? -1;
}

// Resolve the effective deploy status the stepper should render,
// folding in two FE-only refinements:
//   1. The user has just clicked but no heartbeat has confirmed
//      command_received yet → show "queued".
//   2. The runner reports actions_triggered but the 5-min Actions
//      window has expired → optimistically display "completed".
function computeDeployDisplay({ progress, queuedLocally, actionsInFlight }) {
  const raw = progress?.status || "idle";
  if (raw === "failed") {
    return { effective: "failed", failedAtIdx: _stepIndex(progress?.failed_at_status) };
  }
  if (raw === "actions_triggered") {
    return {
      effective: actionsInFlight ? "deploying" : "completed",
      failedAtIdx: -1,
    };
  }
  if (
    queuedLocally &&
    (raw === "idle" || raw === "completed")
  ) {
    return { effective: "queued", failedAtIdx: -1 };
  }
  return { effective: raw, failedAtIdx: -1 };
}

// Window during which we treat a successful local push as "GitHub
// Actions deploy is still running" — long enough to cover the SSH
// build + healthcheck pass on the server, short enough to clear out
// once the workflow has actually finished.
const ACTIONS_INFLIGHT_MS = 5 * 60_000;

// Window after which a *terminal* failure is no longer "the current
// problem" — the panel should fall back to a small "마지막 배포: 실패,
// HH:MM:SS" chip rather than a big red banner forever. Stays
// conservative; the operator can still scroll the previous_attempts
// list to see older failures.
const STALE_FAILURE_MS = 5 * 60_000;

// Compute the deploy button's visible state from runner heartbeat
// metadata. Returns the enabled flag, button label, and a short reason
// string for the disabled case so both the button and the explanation
// strip render off the same source of truth.
function computeDeployState({ runner, busy, guardActive, queuedLocally }) {
  const lf = runner?.metadata_json?.local_factory || {};
  const publish = lf.publish || {};
  const blocker = lf.publish_blocker || {};
  const qa = lf.qa_gate || {};
  const progress = publish.deploy_progress || null;
  const progressStatus = progress?.status || "idle";
  // The new schema flips is_active=true for any in-flight stage and
  // back to false the instant we hit a terminal status (completed /
  // failed). That's the canonical button gate — it lets a failed
  // attempt re-arm the button without waiting for a heartbeat tick.
  const isActive = progress?.is_active === true;
  const changedCount = publish.changed_count ?? 0;
  const dryRun = publish.dry_run !== false;
  const blocked = blocker.blocked === true;
  const currentCommand = runner?.current_command || null;
  const runnerBusy = runner?.status === "busy";
  const lastPushStatus = publish.last_push_status;
  const lastPushAt = publish.last_push_at;

  // Was the most recent terminal failure within STALE_FAILURE_MS?
  // We use this to keep showing a big "실패" banner ONLY when the
  // failure is fresh; older terminal failures collapse to a small
  // "마지막 배포: 실패, HH:MM:SS" chip in the panel.
  let recentFailure = false;
  if (
    progressStatus === "failed" &&
    !isActive &&
    progress?.failed_at
  ) {
    const t = Date.parse(progress.failed_at);
    if (!Number.isNaN(t) && Date.now() - t < STALE_FAILURE_MS) {
      recentFailure = true;
    }
  }

  // GitHub Actions in-flight heuristic: a real (non-dry-run) push
  // succeeded recently and the working tree is clean → the workflow
  // dispatched on the GitHub side is most likely still running.
  let actionsInFlight = false;
  if (lastPushStatus === "succeeded" && lastPushAt && changedCount === 0) {
    const t = Date.parse(lastPushAt);
    if (!Number.isNaN(t) && Date.now() - t < ACTIONS_INFLIGHT_MS) {
      actionsInFlight = true;
    }
  }

  // Whether the runner is currently chewing on a deploy/publish
  // command. Anything else (factory restart, build_check, etc.)
  // doesn't conflict with a deploy click — the queue serializes,
  // but the user sees them as parallel concerns.
  const cmdBlocksDeploy =
    currentCommand === "deploy_to_server" ||
    currentCommand === "publish_changes";

  // First-match wins. Order matches the user-facing priority — the
  // local in-flight click guard outranks server state because it
  // reflects "we just sent the command, server hasn't echoed yet".
  let enabled = true;
  let reason = "";
  let label = dryRun ? "배포 예행연습" : "배포";

  if (busy === "deploy" || guardActive || queuedLocally) {
    enabled = false;
    label = "배포 중";
    reason = "배포 명령 진행 중";
  } else if (isActive) {
    enabled = false;
    label = "배포 중";
    reason = "배포 진행 중 — 단계: " + (progress?.current_step || progressStatus);
  } else if (!runner) {
    enabled = false;
    reason = "러너 오프라인";
  } else if (cmdBlocksDeploy || runnerBusy) {
    enabled = false;
    reason = "명령 실행 중";
  } else if (blocked) {
    enabled = false;
    reason = "Release Safety Gate 차단";
  } else if (actionsInFlight || progressStatus === "actions_triggered") {
    enabled = false;
    reason = "GitHub Actions 배포 진행 중";
  } else if (changedCount === 0) {
    // status === completed AND no changes → disabled. Same gate
    // covers the "post-success" idle case where the working tree
    // is clean.
    enabled = false;
    reason = "배포할 변경 없음";
  }
  // NOTE: a stale qa.status === "failed" no longer blocks the
  // button. The on-demand QA Gate runs at deploy click time, so
  // gating on a leftover qa_status from an earlier cycle just keeps
  // the operator stuck. publish_blocked still blocks; risky/secret
  // checks still block at handler time.

  return {
    enabled,
    label,
    reason,
    dryRun,
    actionsInFlight,
    changedCount,
    currentCommand,
    publish,
    blocker,
    qa,
    diagnostics: lf.command_diagnostics || null,
    progress,
    progressStatus,
    isActive,
    recentFailure,
    queuedLocally,
  };
}

const SAFETY_TAG = {
  clean:   { text: "통과",                color: "#34d399" },
  warning: { text: "경고 있음, 배포 가능", color: "#fbbf24" },
  blocked: { text: "차단",                color: "#f87171" },
};

const QA_TAG = {
  passed:  { text: "통과", color: "#34d399" },
  warned:  { text: "경고", color: "#fbbf24" },
  failed:  { text: "실패", color: "#f87171" },
  skipped: { text: "스킵", color: "#94a3b8" },
};

// "배포 진행" stepper. Renders the 8 progressive steps from idle →
// completed (or failed) so the user can see exactly where the deploy
// is mid-flight without watching server logs. The panel is hidden
// while progress is plain "idle" with no history — there's nothing
// to show until the first deploy click.
const STEP_STATE_COLOR = {
  pending: "#475569",
  running: "#fbbf24",
  success: "#34d399",
  failed:  "#f87171",
};

function _stepState({ index, currentIdx, effective, failedAtIdx }) {
  if (effective === "failed") {
    if (index < failedAtIdx) return "success";
    if (index === failedAtIdx) return "failed";
    return "pending";
  }
  if (effective === "completed") {
    return index <= STATUS_TO_INDEX.completed ? "success" : "pending";
  }
  if (currentIdx < 0) return "pending";
  if (index < currentIdx) return "success";
  if (index === currentIdx) return "running";
  return "pending";
}

function _formatLogTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

function _attemptShortLabel(progress) {
  // Prefer a numeric "Deploy #<cid>" because it lines up with the
  // System Log markers; fall back to the stamped attempt id when the
  // command_id is missing (e.g. operator-fix path).
  if (progress?.command_id) return `Deploy #${progress.command_id}`;
  if (progress?.attempt_id) return progress.attempt_id;
  return "Deploy (현재 시도)";
}

function PreviousAttemptsBlock({ attempts = [] }) {
  if (!attempts || attempts.length === 0) return null;
  // Newest-last in the array as runner appends; render newest-first
  // so the operator sees the most recent prior attempt first.
  const ordered = [...attempts].reverse().slice(0, 3);
  return (
    <details className="grid gap-1">
      <summary className="cursor-pointer text-[10px] font-bold uppercase tracking-[0.3em] text-slate-400 hover:text-slate-200">
        이전 배포 시도 ({attempts.length})
      </summary>
      <ul className="grid gap-1 pt-1">
        {ordered.map((a) => {
          const label = _attemptShortLabel(a);
          const tone =
            a.status === "failed"
              ? STEP_STATE_COLOR.failed
              : a.status === "completed"
                ? STEP_STATE_COLOR.success
                : "#94a3b8";
          return (
            <li
              key={`${a.attempt_id || label}-${a.ended_at || a.updated_at}`}
              className="rounded px-2 py-1 text-[10px] tracking-wider"
              style={{
                backgroundColor: "#0a1228",
                border: `1px solid ${tone}55`,
              }}
            >
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="font-bold text-slate-200">{label}</span>
                <span style={{ color: tone }}>· {a.status}</span>
                {(a.failed_at || a.ended_at) && (
                  <span className="text-slate-500">
                    · {_formatLogTime(a.failed_at || a.ended_at)}
                  </span>
                )}
              </div>
              {a.failed_stage && (
                <div className="mt-0.5 text-slate-400">
                  실패 단계 · <span className="text-slate-200">{a.failed_stage}</span>
                </div>
              )}
              {a.failed_reason && (
                <div className="mt-0.5 line-clamp-2 text-slate-300">
                  {a.failed_reason}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </details>
  );
}

function DeployProgressPanel({ deployState, queuedLocally }) {
  const { progress, actionsInFlight, isActive, recentFailure, qa, diagnostics } = deployState;
  const { effective, failedAtIdx } = computeDeployDisplay({
    progress,
    queuedLocally,
    actionsInFlight,
  });

  // Hide the panel entirely when there's no signal to show — nothing
  // ever queued, runner reports a fresh idle progress block, no
  // history. Once the first deploy fires, the panel stays visible so
  // the last-completed/last-failed state is always there for context.
  const hasSignal =
    queuedLocally ||
    (progress && progress.status && progress.status !== "idle") ||
    (progress?.history && progress.history.length > 0) ||
    (progress?.previous_attempts && progress.previous_attempts.length > 0);
  if (!hasSignal) return null;

  // Drive the stepper from `effective` so "actions_triggered" can be
  // collapsed into "deploying" or "completed" depending on the 5-min
  // Actions window (computeDeployDisplay handles that).
  let currentIdx;
  if (effective === "deploying") {
    currentIdx = STATUS_TO_INDEX.deploying;
  } else if (effective === "completed") {
    currentIdx = STATUS_TO_INDEX.completed;
  } else if (effective === "failed") {
    currentIdx = -1;
  } else {
    currentIdx = STATUS_TO_INDEX[effective] ?? -1;
  }

  // Stale failure (>5min terminal) → collapse the panel into a small
  // "마지막 배포: 실패, HH:MM:SS" chip + the previous-attempts list.
  // Anything fresher gets the full stepper + failure card so the
  // operator can act on it.
  const isFailedTerminal =
    progress?.status === "failed" && progress?.is_active === false;
  const showStaleSummary = isFailedTerminal && !recentFailure;

  if (showStaleSummary) {
    const failedAt = progress?.failed_at || progress?.ended_at;
    return (
      <div
        className="grid gap-2 px-1 pt-1"
        style={{ borderTop: "1px solid #1a2540" }}
      >
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10.5px] tracking-wider">
          <span className="font-bold tracking-[0.25em] text-[#d4a843]">
            ▶ 배포 진행
          </span>
          <span className="text-slate-300">현재 상태: 대기 중</span>
          {failedAt && (
            <span className="text-slate-400">
              마지막 배포 ·{" "}
              <span style={{ color: STEP_STATE_COLOR.failed }}>실패</span>
              {", "}
              {_formatLogTime(failedAt)}
            </span>
          )}
          <span className="rounded border px-1.5 py-0.5 text-[9px] font-bold tracking-widest"
            style={{ borderColor: "#34d39955", color: "#86efac" }}
          >
            다시 시도 가능
          </span>
        </div>
        <PreviousAttemptsBlock attempts={progress?.previous_attempts} />
      </div>
    );
  }

  const history = (progress?.history || []).slice(-8).reverse();
  // Prefer the structured command_diagnostics blob written by the
  // runner (richer than progress.failed_reason — has diagnostic_code,
  // suggested_action, and the QA stderr_tail when applicable).
  // Fall back to the qa_gate metadata when command_diagnostics is
  // empty (older runner heartbeat). Final fallback is the legacy
  // progress.failed_reason string.
  const cmdDiag = diagnostics || {};
  const diagnosticCode =
    cmdDiag.diagnostic_code || qa?.diagnostic_code || null;
  const failedReason =
    cmdDiag.failed_reason ||
    progress?.failed_reason ||
    qa?.failed_reason ||
    null;
  const failedStage =
    cmdDiag.failed_stage || progress?.failed_stage || null;
  const failedAt = progress?.failed_at;
  const suggestedAction =
    cmdDiag.suggested_action ||
    progress?.suggested_action ||
    qa?.suggested_action ||
    null;
  const startedAt = progress?.started_at;
  const updatedAt = progress?.updated_at;
  const completedAt = progress?.completed_at;
  const attemptLabel = _attemptShortLabel(progress);

  return (
    <div
      className="grid gap-2 px-1 pt-1"
      style={{
        borderTop: "1px solid #1a2540",
      }}
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] tracking-[0.25em] text-[#d4a843]">
        <span className="font-bold">▶ 배포 진행</span>
        <span className="text-slate-500">·</span>
        <span className="text-[10px] tracking-wider text-slate-300">
          {attemptLabel}
        </span>
        <span className="text-slate-500">·</span>
        <span
          className="text-[10px] font-bold"
          style={{
            color:
              effective === "failed"
                ? STEP_STATE_COLOR.failed
                : effective === "completed"
                  ? STEP_STATE_COLOR.success
                  : "#fbbf24",
          }}
        >
          {effective === "failed"
            ? "실패"
            : effective === "completed"
              ? "완료"
              : effective === "queued"
                ? "요청 전송됨"
                : effective === "deploying"
                  ? "GitHub Actions 진행 중"
                  : "진행 중"}
        </span>
        {!isActive && effective === "failed" && (
          <span
            className="rounded border px-1.5 py-0.5 text-[9px] font-bold tracking-widest"
            style={{ borderColor: "#34d39955", color: "#86efac" }}
          >
            다시 시도 가능
          </span>
        )}
        {progress?.current_step && effective !== "completed" && (
          <span className="text-[10px] text-slate-400">
            · {progress.current_step}
          </span>
        )}
      </div>

      {/* Stepper row */}
      <div className="flex flex-wrap items-stretch gap-1">
        {DEPLOY_STEPS.map((step, idx) => {
          const state = _stepState({
            index: idx,
            currentIdx,
            effective,
            failedAtIdx,
          });
          const color = STEP_STATE_COLOR[state];
          return (
            <div
              key={step.key}
              className="flex min-w-[88px] flex-1 flex-col items-start gap-0.5 px-1.5 py-1"
              style={{
                backgroundColor:
                  state === "running" ? "rgba(251, 191, 36, 0.08)" : "#0a1228",
                border: `1px solid ${color}`,
                borderRadius: 3,
                fontFamily: "ui-monospace, monospace",
              }}
              title={`${step.label} · ${state}`}
            >
              <div
                className="flex items-center gap-1 text-[9px] font-bold uppercase tracking-wider"
                style={{ color }}
              >
                <span
                  className="inline-block h-1.5 w-1.5"
                  style={{
                    backgroundColor: color,
                    borderRadius: state === "running" ? "50%" : 0,
                    animation:
                      state === "running" ? "pulse 1s infinite" : "none",
                  }}
                />
                {String(idx + 1).padStart(2, "0")}
              </div>
              <div className="text-[10px] tracking-wider text-slate-200">
                {step.label}
              </div>
              <div
                className="text-[9px] tracking-wider"
                style={{ color }}
              >
                {state === "pending"
                  ? "대기"
                  : state === "running"
                    ? "진행 중"
                    : state === "success"
                      ? "완료"
                      : "실패"}
              </div>
            </div>
          );
        })}
      </div>

      {/* Timestamps + failure detail */}
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-slate-500">
        {progress?.command_id && <span>cmd · #{progress.command_id}</span>}
        {startedAt && <span>시작 · {_formatLogTime(startedAt)}</span>}
        {updatedAt && <span>업데이트 · {_formatLogTime(updatedAt)}</span>}
        {completedAt && <span>완료 · {_formatLogTime(completedAt)}</span>}
        {failedAt && effective === "failed" && (
          <span>실패 · {_formatLogTime(failedAt)}</span>
        )}
        {effective !== "completed" &&
          effective !== "failed" &&
          progress?.actions_url && (
            <a
              href={progress.actions_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sky-400 hover:text-sky-300"
            >
              ▶ Actions에서 워크플로 보기
            </a>
          )}
      </div>

      {effective === "failed" && (failedReason || failedStage || suggestedAction || diagnosticCode) && (
        <div
          className="grid gap-1 rounded px-2 py-1.5 text-[10.5px] tracking-wider"
          style={{
            backgroundColor: "#3d0a14",
            border: "1px solid #8b2e3c",
            color: "#fecaca",
          }}
        >
          <div className="flex flex-wrap items-baseline gap-x-3">
            <span className="font-bold">⚠ 배포 실패</span>
            {failedAt && (
              <span className="text-[10px] text-rose-300">
                실패 시각 · {_formatLogTime(failedAt)}
              </span>
            )}
            {failedStage && (
              <span className="text-[10px] text-rose-300">
                실패 단계 · {failedStage}
              </span>
            )}
            {diagnosticCode && (
              <span className="text-[10px] text-amber-200">
                진단 ·{" "}
                <code
                  className="rounded px-1 text-amber-300"
                  style={{ backgroundColor: "#0a1228" }}
                >
                  {diagnosticCode}
                </code>
              </span>
            )}
          </div>
          {failedReason && (
            <pre className="whitespace-pre-wrap break-words text-[10.5px] leading-snug text-rose-100">
              {failedReason}
            </pre>
          )}
          {suggestedAction && (
            <div className="text-[10px] text-amber-200">
              권장 조치 · {suggestedAction}
            </div>
          )}
        </div>
      )}

      {/* Recent transitions, newest first. Acts as a lightweight
          system-log without needing a separate panel. */}
      {history.length > 0 && (
        <div
          className="grid gap-0.5 rounded px-2 py-1 text-[10px] text-slate-300"
          style={{ backgroundColor: "#0a1228", border: "1px solid #1a2540" }}
        >
          {history.map((h, i) => (
            <div
              key={`${h.at}-${i}`}
              className="flex flex-wrap items-baseline gap-2"
            >
              <span className="text-slate-500">[{_formatLogTime(h.at)}]</span>
              <span className="text-slate-400">{h.status}</span>
              <span className="text-slate-200">· {h.message}</span>
            </div>
          ))}
        </div>
      )}

      <PreviousAttemptsBlock attempts={progress?.previous_attempts} />
    </div>
  );
}

function DeployInfoStrip({ deployState }) {
  const { publish, blocker, qa, dryRun, changedCount, enabled, reason } = deployState;
  const blockerStatus = blocker.status || "clean";
  const qaStatus = qa.status || "skipped";
  const safety = SAFETY_TAG[blockerStatus] || { text: blockerStatus, color: "#94a3b8" };
  const qaTag = QA_TAG[qaStatus] || { text: qaStatus, color: "#94a3b8" };
  const warningReasons = (blocker.warning_reasons || []).slice(0, 2);
  const changedFiles = publish.changed_files || [];
  const lastMessage = publish.last_publish_message;
  const actionsUrl = publish.actions_url;

  const hasAnything =
    changedCount > 0 ||
    publish.last_push_status ||
    blocker.status ||
    qa.status ||
    actionsUrl ||
    lastMessage;
  if (!hasAnything) return null;

  return (
    <div className="grid gap-1 px-1 text-[10.5px] tracking-wider">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-slate-400">
        <span>
          변경 <span className="text-slate-200">{changedCount}개</span>
        </span>
        <span>
          안전게이트 ·{" "}
          <span style={{ color: safety.color }}>{safety.text}</span>
        </span>
        <span>
          QA · <span style={{ color: qaTag.color }}>{qaTag.text}</span>
        </span>
        {dryRun && (
          <span
            className="rounded px-1.5 py-0.5 text-[9.5px] uppercase"
            style={{
              backgroundColor: "rgba(56, 189, 248, 0.15)",
              color: "#7dd3fc",
              border: "1px solid #38bdf855",
            }}
          >
            DRY-RUN
          </span>
        )}
        {actionsUrl && (
          <a
            href={actionsUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="text-sky-400 hover:text-sky-300"
          >
            ▶ Actions에서 배포 보기
          </a>
        )}
      </div>

      {warningReasons.length > 0 && (
        <div className="text-[10px] text-amber-300/90">
          ⚠ {warningReasons.join(" · ")}
        </div>
      )}

      {changedFiles.length > 0 && (
        <div className="line-clamp-2 text-[10px] text-slate-300">
          {changedFiles.slice(0, 8).map((p, i) => (
            <span key={p}>
              {i > 0 && ", "}
              <code
                className="rounded px-1 text-slate-200"
                style={{ backgroundColor: "#0a1228" }}
              >
                {p}
              </code>
            </span>
          ))}
          {changedFiles.length > 8 && (
            <span className="text-slate-500">
              {" "}
              · 외 {changedFiles.length - 8}개
            </span>
          )}
        </div>
      )}

      {lastMessage && (
        <div className="line-clamp-2 text-[10px] text-slate-500">
          마지막 메시지 · {lastMessage}
        </div>
      )}

      {/* Manual redeploy slot — placeholder only. workflow_dispatch
          API call lands in a follow-up PR. */}
      {changedCount === 0 && (
        <div className="text-[10px] text-slate-500">
          수동 재배포 (예정) · 재배포는 Actions 수동 실행으로 가능
        </div>
      )}

      {!enabled && reason && (
        <div className="text-[10px] tracking-wider text-amber-300">
          ▸ 배포 비활성: {reason}
        </div>
      )}
    </div>
  );
}

// Game-style control dock. Floats at the bottom of the office. Each
// command is a square pixel button with a pictogram and a label that
// fades in on hover. Big gray button rows are intentionally avoided.
//
// Runner-bound actions (build / test / deploy) target the first online
// runner. When no runner is online, those buttons gray out with a
// tooltip explaining why.

const STATUS_LABEL = {
  idle:      { label: "대기 중",   color: "#94a3b8" },
  running:   { label: "실행 중",   color: "#fbbf24" },
  paused:    { label: "일시정지",  color: "#a78bfa" },
  stopping:  { label: "중지 중",   color: "#fb923c" },
  stopped:   { label: "중지됨",    color: "#fb7185" },
  completed: { label: "배포 완료", color: "#34d399" },
  failed:    { label: "실패",     color: "#f87171" },
};

function PixelIcon({ kind, color }) {
  // Each glyph is hand-drawn in a 24x24 grid using crisp rectangles.
  const c = color;
  switch (kind) {
    case "start":
      return (
        <svg viewBox="0 0 24 24" width="22" height="22" shapeRendering="crispEdges">
          <rect x="6"  y="4"  width="3" height="16" fill={c} />
          <rect x="9"  y="6"  width="3" height="12" fill={c} />
          <rect x="12" y="8"  width="3" height="8"  fill={c} />
          <rect x="15" y="10" width="3" height="4"  fill={c} />
        </svg>
      );
    case "pause":
      return (
        <svg viewBox="0 0 24 24" width="22" height="22" shapeRendering="crispEdges">
          <rect x="7"  y="5" width="3" height="14" fill={c} />
          <rect x="14" y="5" width="3" height="14" fill={c} />
        </svg>
      );
    case "resume":
      return (
        <svg viewBox="0 0 24 24" width="22" height="22" shapeRendering="crispEdges">
          <rect x="6"  y="4"  width="2" height="16" fill={c} />
          <rect x="11" y="4"  width="3" height="16" fill={c} />
          <rect x="14" y="6"  width="3" height="12" fill={c} />
          <rect x="17" y="9"  width="2" height="6"  fill={c} />
        </svg>
      );
    case "stop":
      return (
        <svg viewBox="0 0 24 24" width="22" height="22" shapeRendering="crispEdges">
          <rect x="5" y="5" width="14" height="14" fill={c} />
          <rect x="7" y="7" width="10" height="10" fill="#0a1228" />
          <rect x="9" y="9" width="6"  height="6"  fill={c} />
        </svg>
      );
    case "build":
      // hammer
      return (
        <svg viewBox="0 0 24 24" width="22" height="22" shapeRendering="crispEdges">
          <rect x="3"  y="3"  width="9" height="6" fill={c} />
          <rect x="5"  y="5"  width="9" height="2" fill="#0a1228" opacity="0.5" />
          <rect x="9"  y="9"  width="3" height="12" fill={c} />
        </svg>
      );
    case "test":
      // checklist
      return (
        <svg viewBox="0 0 24 24" width="22" height="22" shapeRendering="crispEdges">
          <rect x="4" y="3"  width="14" height="18" fill={c} />
          <rect x="6" y="5"  width="10" height="2"  fill="#0a1228" />
          <rect x="6" y="9"  width="2"  height="2"  fill="#0a1228" />
          <rect x="9" y="9"  width="7"  height="2"  fill="#0a1228" />
          <rect x="6" y="13" width="2"  height="2"  fill="#0a1228" />
          <rect x="9" y="13" width="7"  height="2"  fill="#0a1228" />
          <rect x="6" y="17" width="2"  height="2"  fill="#0a1228" />
          <rect x="9" y="17" width="7"  height="2"  fill="#0a1228" />
        </svg>
      );
    case "deploy":
      // rocket
      return (
        <svg viewBox="0 0 24 24" width="22" height="22" shapeRendering="crispEdges">
          <rect x="10" y="3"  width="4" height="2"  fill={c} />
          <rect x="9"  y="5"  width="6" height="9"  fill={c} />
          <rect x="11" y="8"  width="2" height="3"  fill="#0a1228" />
          <rect x="7"  y="10" width="2" height="4"  fill={c} />
          <rect x="15" y="10" width="2" height="4"  fill={c} />
          <rect x="9"  y="14" width="6" height="2"  fill={c} />
          <rect x="10" y="16" width="4" height="2"  fill="#fb923c" />
          <rect x="11" y="18" width="2" height="3"  fill="#fbbf24" />
        </svg>
      );
    case "reset":
      return (
        <svg viewBox="0 0 24 24" width="22" height="22" shapeRendering="crispEdges">
          <rect x="5"  y="5"  width="2" height="14" fill={c} />
          <rect x="7"  y="3"  width="2" height="2"  fill={c} />
          <rect x="9"  y="3"  width="6" height="2"  fill={c} />
          <rect x="15" y="5"  width="2" height="2"  fill={c} />
          <rect x="17" y="7"  width="2" height="10" fill={c} />
          <rect x="15" y="17" width="2" height="2"  fill={c} />
          <rect x="9"  y="19" width="6" height="2"  fill={c} />
          <rect x="7"  y="17" width="2" height="2"  fill={c} />
          <rect x="11" y="9"  width="2" height="6"  fill={c} />
        </svg>
      );
    default:
      return null;
  }
}

function DockButton({
  icon,
  label,
  onClick,
  disabled,
  tone = "default",
  busy,
  title,
}) {
  const toneColors = {
    default: { fg: "#f5e9d3", border: "#0e4a3a", glow: "#0e4a3a" },
    primary: { fg: "#0a1228", border: "#d4a843", glow: "#d4a843", bg: "#d4a843" },
    danger:  { fg: "#fef3c7", border: "#8b2e3c", glow: "#8b2e3c" },
    warn:    { fg: "#fde68a", border: "#a16207", glow: "#a16207" },
    success: { fg: "#d1fae5", border: "#0e4a3a", glow: "#10b981" },
  };
  const t = toneColors[tone] || toneColors.default;

  return (
    <motion.button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title || label}
      whileTap={disabled ? {} : { scale: 0.92 }}
      whileHover={disabled ? {} : { y: -2 }}
      className="group relative flex flex-col items-center justify-center"
      style={{
        width: 60,
        height: 64,
        backgroundColor: tone === "primary" ? t.bg : "#0a1228",
        border: `2px solid ${disabled ? "#1a2540" : t.border}`,
        boxShadow: disabled
          ? "none"
          : `0 0 0 2px #0a1228, 0 0 14px ${t.glow}55, inset 0 -3px 0 rgba(0,0,0,0.4)`,
        borderRadius: 4,
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.4 : 1,
        fontFamily: "ui-monospace, monospace",
        transition: "border-color 120ms",
      }}
    >
      <PixelIcon kind={icon} color={t.fg} />
      <span
        className="mt-0.5 text-[9px] font-bold uppercase tracking-wider"
        style={{ color: t.fg }}
      >
        {busy ? "..." : label}
      </span>
    </motion.button>
  );
}

export default function ControlDock({ factory, runners = [], onChanged }) {
  const [busy, setBusy] = useState(null);
  // Re-render trigger when the module-scoped deploy guard flips. We
  // can't just read _deployInflight in render — React doesn't know to
  // refresh — so this state is touched in the deploy click handler.
  const [, setDeployTick] = useState(0);
  const deployTimerRef = useRef(null);
  const status = factory?.status || "idle";
  const meta = STATUS_LABEL[status] || STATUS_LABEL.idle;

  const onlineRunner = runners.find((r) => r.status === "online");
  const runnerId = onlineRunner?.id;
  // For the deploy info strip we want to read metadata from any
  // runner whose heartbeat is still alive — including a `busy` one
  // mid-publish — so the dashboard keeps showing publish/qa state
  // while a command is in flight. The deploy *click* still requires
  // an idle online runner.
  const heartbeatRunner =
    onlineRunner || runners.find((r) => r.status !== "offline") || null;

  // Clear any outstanding deploy guard timer when this dock unmounts
  // so we don't tick a stale setState.
  useEffect(() => {
    return () => {
      if (deployTimerRef.current) {
        clearInterval(deployTimerRef.current);
        deployTimerRef.current = null;
      }
    };
  }, []);

  const wrap = (label, fn) => async () => {
    if (busy) return;
    setBusy(label);
    try {
      await fn();
      await onChanged?.();
    } catch (e) {
      console.error(e);
      alert(`명령 실패: ${e.message}`);
    } finally {
      setBusy(null);
    }
  };

  const deployGuardActive = _deployGuardActive();
  // Clear the FE-only "queued" flag once the runner heartbeat reports
  // any post-click status — command_received or deeper. After that,
  // the stepper drives off real runner state.
  const _hbStatus =
    heartbeatRunner?.metadata_json?.local_factory?.publish?.deploy_progress
      ?.status || "idle";
  if (_deployQueuedActive() && _hbStatus !== "idle" && _hbStatus !== "queued") {
    _deployQueuedSince = 0;
  }
  const queuedLocally = _deployQueuedActive();
  const deployState = computeDeployState({
    runner: heartbeatRunner,
    busy,
    guardActive: deployGuardActive,
    queuedLocally,
  });

  // Deploy is special — it has a *module-level* guard so rapid
  // remounts (StrictMode dev double-mount, or a panel re-render mid
  // click) cannot enqueue a second deploy_to_server command. The
  // local component `busy` is also set so the button greys out
  // visually.
  const onDeployClick = async () => {
    if (busy) {
      alert("배포가 이미 진행 중입니다. 완료 후 다시 시도하세요.");
      return;
    }
    if (_deployGuardActive() || _deployQueuedActive()) {
      alert("배포가 이미 진행 중입니다. 완료 후 다시 시도하세요.");
      return;
    }
    if (DEPLOY_INFLIGHT_STATUSES.has(deployState.progressStatus)) {
      alert("배포가 이미 진행 중입니다. 완료 후 다시 시도하세요.");
      return;
    }
    if (!runnerId) return;
    // Belt-and-braces: even if the button somehow stays clickable
    // (race between heartbeat polls), refuse to enqueue when the
    // computed gate says we shouldn't.
    if (!deployState.enabled) return;
    setBusy("deploy");
    _deployInflightSince = Date.now();
    // Flip "queued" immediately so the stepper lights up step 01
    // before the next heartbeat lands. Cleared above once the runner
    // confirms command_received or deeper.
    _deployQueuedSince = Date.now();
    // Refresh the disabled state every 2s so the lockout reflects the
    // module-level timeout in the UI even if no other state changes.
    if (deployTimerRef.current) clearInterval(deployTimerRef.current);
    deployTimerRef.current = setInterval(() => setDeployTick((n) => n + 1), 2000);
    const p = (async () => {
      try {
        await sendRunnerCommand(runnerId, "deploy_to_server");
        await onChanged?.();
      } catch (e) {
        console.error(e);
        alert(`배포 명령 실패: ${e.message}`);
      }
    })();
    _deployInflight = p;
    try {
      await p;
    } finally {
      _deployInflight = null;
      _deployInflightSince = 0;
      if (deployTimerRef.current) {
        clearInterval(deployTimerRef.current);
        deployTimerRef.current = null;
      }
      setBusy(null);
      setDeployTick((n) => n + 1);
    }
  };
  const canStart = ["idle", "stopped", "completed", "failed"].includes(status);
  const canPause = status === "running";
  const canResume = status === "paused";
  const canStop = ["running", "paused"].includes(status);
  const canReset = status !== "running";
  const noRunnerNote = runnerId ? "" : "온라인 러너가 필요합니다";

  return (
    <section
      className="relative flex flex-col gap-2 p-3"
      style={{
        backgroundColor: "#0e1a35",
        border: "1.5px solid #d4a84355",
        borderRadius: 6,
        boxShadow: "0 -2px 0 rgba(0,0,0,0.4) inset",
        fontFamily: "ui-monospace, monospace",
      }}
    >
      {/* status bar */}
      <div className="flex flex-wrap items-center justify-between gap-3 px-1">
        <div className="flex items-center gap-2">
          <motion.span
            className="inline-block h-2 w-2"
            style={{ backgroundColor: meta.color }}
            animate={
              status === "running"
                ? { opacity: [0.3, 1, 0.3] }
                : { opacity: 1 }
            }
            transition={{ duration: 1, repeat: Infinity }}
          />
          <span className="text-[10px] font-bold uppercase tracking-[0.3em] text-[#d4a843]">
            CONTROL DOCK
          </span>
          <span
            className="text-[10px] font-bold tracking-wider"
            style={{ color: meta.color }}
          >
            · {meta.label}
          </span>
          {factory?.current_stage && (
            <span className="text-[10px] tracking-wider text-slate-400">
              · STAGE {factory.current_stage}
            </span>
          )}
          {typeof factory?.run_count === "number" && factory.run_count > 0 && (
            <span className="text-[10px] tracking-wider text-slate-500">
              · 누적 {factory.run_count}
            </span>
          )}
        </div>

        {/* continuous toggle as a pixel switch */}
        <label
          className="flex cursor-pointer items-center gap-2 px-2 py-1 text-[10px] font-bold tracking-wider"
          style={{
            backgroundColor: "#0a1228",
            border: "1px solid #0e4a3a",
            borderRadius: 3,
            color: "#f5e9d3",
          }}
        >
          <span>CONTINUOUS</span>
          <input
            type="checkbox"
            className="h-3 w-3 cursor-pointer accent-[#d4a843]"
            checked={!!factory?.continuous_mode}
            disabled={!!busy}
            onChange={wrap("continuous", () =>
              setFactoryContinuous(!factory?.continuous_mode),
            )}
          />
          <span style={{ color: factory?.continuous_mode ? "#34d399" : "#475569" }}>
            {factory?.continuous_mode ? "ON" : "OFF"}
          </span>
        </label>
      </div>

      {/* button row — 7 actions + reset */}
      <div className="flex flex-wrap items-end justify-between gap-2 px-1">
        <div className="flex flex-wrap items-end gap-2">
          <DockButton
            icon="start"
            label="시작"
            tone="primary"
            disabled={!canStart || !!busy}
            busy={busy === "start"}
            onClick={wrap("start", startFactory)}
          />
          <DockButton
            icon="pause"
            label="일시정지"
            tone="warn"
            disabled={!canPause || !!busy}
            busy={busy === "pause"}
            onClick={wrap("pause", pauseFactory)}
          />
          <DockButton
            icon="resume"
            label="재개"
            tone="primary"
            disabled={!canResume || !!busy}
            busy={busy === "resume"}
            onClick={wrap("resume", resumeFactory)}
          />
          <DockButton
            icon="stop"
            label="중지"
            tone="danger"
            disabled={!canStop || !!busy}
            busy={busy === "stop"}
            onClick={wrap("stop", stopFactory)}
          />
          <DockButton
            icon="reset"
            label="초기화"
            tone="default"
            disabled={!canReset || !!busy}
            busy={busy === "reset"}
            onClick={wrap("reset", resetFactory)}
          />
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <DockButton
            icon="build"
            label="빌드"
            tone="default"
            disabled={!runnerId || !!busy}
            busy={busy === "build"}
            title={noRunnerNote || "빌드 확인"}
            onClick={wrap("build", () =>
              sendRunnerCommand(runnerId, "build_check"),
            )}
          />
          <DockButton
            icon="test"
            label="테스트"
            tone="default"
            disabled={!runnerId || !!busy}
            busy={busy === "test"}
            title={noRunnerNote || "테스트 확인"}
            onClick={wrap("test", () =>
              sendRunnerCommand(runnerId, "test_check"),
            )}
          />
          <DockButton
            icon="deploy"
            label={deployState.label}
            tone="success"
            disabled={!runnerId || !!busy || !deployState.enabled}
            busy={busy === "deploy" || deployGuardActive}
            title={
              deployState.enabled
                ? deployState.dryRun
                  ? "배포 예행연습 (dry-run · 실제 push 없이 검증)"
                  : "서버 배포 (publish + SSH 빌드 + 헬스체크)"
                : `배포 비활성: ${deployState.reason || "조건 미충족"}`
            }
            onClick={onDeployClick}
          />
        </div>
      </div>

      <CycleChangePanel runner={heartbeatRunner} />

      <ReleasePreviewPanel deployState={deployState} />

      <DeployInfoStrip deployState={deployState} />

      <DeployProgressPanel
        deployState={deployState}
        queuedLocally={queuedLocally}
      />

      {factory?.last_message && (
        <div className="px-1 text-[10.5px] tracking-wider text-slate-400">
          ▸ {factory.last_message}
        </div>
      )}
    </section>
  );
}
