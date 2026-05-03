import { useState } from "react";
import { sendRunnerCommand } from "../api/controlTowerApi.js";

const STATUS_TONE = {
  online:  { chip: "bg-emerald-500 text-slate-950", label: "온라인" },
  busy:    { chip: "bg-amber-500 text-slate-950",   label: "실행 중" },
  offline: { chip: "bg-slate-700 text-slate-300",   label: "오프라인" },
  error:   { chip: "bg-rose-500 text-slate-50",     label: "오류" },
};

const FACTORY_TONE = {
  succeeded: "bg-emerald-500 text-slate-950",
  running:   "bg-amber-500 text-slate-950",
  failed:    "bg-rose-500 text-slate-50",
  paused:    "bg-violet-500 text-slate-950",
};

// Buttons we briefly disable after a click to dedupe rapid taps. The
// debounce window is short enough not to feel unresponsive, but long
// enough that two taps at 200ms intervals only enqueue one command.
const COMMAND_DEDUPE_MS = 1500;

// Server returns datetimes from datetime.utcnow() with no timezone — JS
// Date() then mis-parses them as local time and you get phantom 9-hour
// skews under KST. parseUtcIso forces UTC interpretation when the
// string carries no offset.
function parseUtcIso(iso) {
  if (!iso) return null;
  const s = String(iso);
  const hasTz = /Z$|[+-]\d{2}:?\d{2}$/.test(s);
  const d = new Date(hasTz ? s : s + "Z");
  return isNaN(d.getTime()) ? null : d;
}

function timeAgo(iso) {
  const d = parseUtcIso(iso);
  if (!d) return "—";
  const sec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (sec < 0) {
    // Server clock ahead by a tick — show as "방금" rather than a wrong
    // negative count, since it's always within a few seconds.
    return "방금";
  }
  if (sec < 60) return `${sec}초 전`;
  if (sec < 3600) return `${Math.floor(sec / 60)}분 전`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}시간 전`;
  return d.toLocaleString("ko-KR", { hour12: false });
}

function localTimeStr(iso) {
  const d = parseUtcIso(iso);
  if (!d) return "—";
  return d.toLocaleString("ko-KR", { hour12: false });
}

function Btn({ children, onClick, disabled, tone = "default" }) {
  const cls = {
    default: "bg-slate-800 text-slate-200 hover:bg-slate-700",
    primary: "bg-sky-500 text-slate-950 hover:bg-sky-400",
    warn:    "bg-amber-500 text-slate-950 hover:bg-amber-400",
    danger:  "bg-rose-500 text-slate-50 hover:bg-rose-400",
  }[tone];
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`rounded-lg px-2.5 py-1.5 text-[12px] font-semibold transition ${
        disabled ? "cursor-not-allowed bg-slate-800/60 text-slate-600" : cls
      }`}
    >
      {children}
    </button>
  );
}

// Korean copy keyed by diagnostic_code so the operator immediately
// sees *why* QA blocked the deploy. Keep aligned with _QA_SUGGESTION
// in control_tower/local_runner/runner.py.
const QA_DIAG_LABEL = {
  qa_passed_cached: "이전 사이클 QA 통과 (캐시 사용)",
  qa_passed_after_run: "on-demand QA 통과",
  qa_skipped_bypass: "QA 우회 (비상)",
  qa_not_run: "QA 단계가 실행되지 않음",
  qa_report_missing_before_run: "qa_report.md 없음 → on-demand QA 실행 중",
  qa_report_missing_after_run: "QA 실행 후에도 qa_report.md 없음",
  qa_report_path_mismatch: "qa_report.md 경로 불일치",
  qa_command_failed: "검증 명령 실패",
  qa_exception_before_report: "QA 실행 중 예외 발생",
  stale_runner: "runner.py 가 부팅 이후 수정됨",
  stale_command: "이전 deploy 명령이 중복 실행됨",
  stale_metadata: "qa 상태와 파일 상태 불일치",
  unknown: "원인 분류 실패",
};

const QA_DIAG_ERROR_CODES = new Set([
  "qa_not_run",
  "qa_report_missing_after_run",
  "qa_report_path_mismatch",
  "qa_command_failed",
  "qa_exception_before_report",
  "stale_runner",
  "stale_command",
  "stale_metadata",
  "unknown",
]);

function QaGateRow({ factory }) {
  const qa = factory?.qa_gate;
  if (!qa) return null;
  const status = qa.status || "skipped";
  const diagCode = qa.diagnostic_code || null;
  const hasDiag = !!diagCode && QA_DIAG_ERROR_CODES.has(diagCode);
  if (
    status === "skipped" &&
    !qa.report_exists &&
    !qa.feedback_exists &&
    !hasDiag
  ) {
    return null;
  }

  const subtoneFor = (s) =>
    s === "passed"
      ? "text-emerald-400"
      : s === "failed"
      ? "text-rose-400"
      : "text-slate-500";

  const headerLabel =
    status === "passed"
      ? "passed"
      : status === "failed"
      ? "failed"
      : "skipped";
  const headerColor = subtoneFor(status);

  const subRows = [
    ["API Smoke", qa.api_smoke],
    ["Contract", qa.contract_guard],
    ["Frontend", qa.frontend_runtime],
    ["Data", qa.data_consistency],
    ["Build", qa.build_artifact],
  ];

  const hasFixLoop =
    typeof qa.fix_max_attempts === "number" && qa.fix_max_attempts > 0;

  return (
    <div className="text-slate-500">
      <div>
        QA Gate · <span className={headerColor}>{headerLabel}</span>
        {qa.publish_allowed ? (
          <>
            {" · "}
            <span className="text-emerald-400">배포 허용</span>
          </>
        ) : (
          <>
            {" · "}
            <span className="text-rose-300">배포 차단됨</span>
          </>
        )}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-0.5">
        {subRows.map(([k, v]) => (
          <span key={k}>
            {k} · <span className={subtoneFor(v)}>{v || "skipped"}</span>
          </span>
        ))}
      </div>
      {qa.failed_reason && status === "failed" && (
        <div className="line-clamp-2 text-rose-200/90">
          원인 · {qa.failed_reason}
        </div>
      )}
      {hasDiag && (
        <div className="mt-1 grid gap-0.5 rounded border border-rose-500/40 bg-rose-950/40 px-2 py-1 text-[10.5px] text-rose-100">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="font-bold tracking-wider">진단 코드</span>
            <code className="rounded bg-slate-900 px-1 text-amber-300">
              {diagCode}
            </code>
            <span className="text-rose-200/80">
              · {QA_DIAG_LABEL[diagCode] || diagCode}
            </span>
          </div>
          {qa.qa_required_reason && (
            <div className="text-rose-200/70">
              실행 사유 · {qa.qa_required_reason}
            </div>
          )}
          {qa.failed_command && (
            <div>
              실패 명령 ·{" "}
              <code className="rounded bg-slate-900 px-1 text-amber-200">
                {qa.failed_command}
              </code>
              {typeof qa.exit_code === "number" && (
                <span className="ml-2 text-rose-200/80">
                  exit_code · {qa.exit_code}
                </span>
              )}
            </div>
          )}
          {(qa.report_exists_before !== undefined ||
            qa.report_exists_after !== undefined) && (
            <div className="text-rose-200/70">
              report 존재 · before {String(qa.report_exists_before)} · after{" "}
              {String(qa.report_exists_after)}
            </div>
          )}
          {qa.cycle_report_path &&
            qa.report_path &&
            qa.cycle_report_path !== qa.report_path && (
              <div className="text-amber-300">
                ⚠ 경로 mismatch · runner={qa.report_path} · cycle=
                {qa.cycle_report_path}
              </div>
            )}
          {qa.stale_runner && (
            <div className="text-amber-300">
              ⚠ runner.py 가 부팅 이후 수정됨 — restart 권장
            </div>
          )}
          {qa.stderr_tail && (
            <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-950 px-1.5 py-1 text-[10px] leading-snug text-rose-100">
              {qa.stderr_tail}
            </pre>
          )}
          {qa.exception_message && !qa.stderr_tail && (
            <div className="text-rose-200">
              예외 · {qa.exception_message}
            </div>
          )}
          {qa.suggested_action && (
            <div className="text-amber-200">
              권장 조치 · {qa.suggested_action}
            </div>
          )}
        </div>
      )}
      {(qa.console_errors > 0 || qa.page_errors > 0) && (
        <div>
          Console error · {qa.console_errors} · Page error · {qa.page_errors}
        </div>
      )}
      {qa.blank_screen && (
        <div className="text-rose-300">⚠ 빈 화면 위험 — ErrorBoundary/렌더 가드 점검</div>
      )}
      {qa.object_render_error && (
        <div className="text-rose-300">⚠ object render 위험 — BulletList 방어 필요</div>
      )}
      {hasFixLoop && (qa.fix_attempt > 0 || status === "failed") && (
        <div>
          수정 루프 · {qa.fix_attempt}/{qa.fix_max_attempts}
          {qa.fix_propose_status && qa.fix_propose_status !== "skipped" && (
            <>
              {" · "}
              <span>제안 {qa.fix_propose_status}</span>
            </>
          )}
          {qa.fix_apply_status && qa.fix_apply_status !== "skipped" && (
            <>
              {" · "}
              <span>적용 {qa.fix_apply_status}</span>
            </>
          )}
        </div>
      )}
      {qa.report_path && qa.report_exists && (
        <div>
          QA 리포트 ·{" "}
          <code className="rounded bg-slate-800 px-1 text-slate-300">
            {qa.report_path}
          </code>
        </div>
      )}
      {qa.feedback_path && qa.feedback_exists && (
        <div>
          QA Feedback ·{" "}
          <code className="rounded bg-slate-800 px-1 text-slate-300">
            {qa.feedback_path}
          </code>
        </div>
      )}
      {qa.screenshot_path && qa.screenshot_exists && (
        <div>
          스크린샷 ·{" "}
          <code className="rounded bg-slate-800 px-1 text-slate-300">
            {qa.screenshot_path}
          </code>
        </div>
      )}
    </div>
  );
}

function PublishBlockerRow({ factory }) {
  const blocker = factory?.publish_blocker;
  if (!blocker) return null;
  const status = blocker.status || "clean";
  const blocked = !!blocker.blocked;

  // 5-bucket counts. Fall back to the legacy auto_resolved_* fields
  // for older heartbeats so the UI doesn't go blank during deploys.
  const autoRestored = blocker.auto_restored_count ?? 0;
  const autoDeleted = blocker.auto_deleted_count ?? 0;
  const allowedCode = blocker.allowed_code_count ?? 0;
  // manual_required is now the *warning* bucket — counts as "주의
  // 깊게 보면 좋은" change, not a blocker.
  const warningCount = blocker.manual_required_count ?? 0;
  const hardRisky = blocker.hard_risky_count ?? 0;
  const conflictMarkers = blocker.conflict_markers || [];
  const conflictCount = conflictMarkers.length;
  const warningReasons = blocker.warning_reasons || [];
  const warningFiles = blocker.manual_required_files || [];
  const hardRiskyBn = blocker.hard_risky_basenames || [];
  const autoRestoredFiles = blocker.auto_restored_files || [];
  const autoDeletedFiles = blocker.auto_deleted_files || [];
  const message = blocker.message;
  const reportPath = blocker.report_path;
  const reportExists = !!blocker.report_exists;
  const recurring = blocker.recurring || {};
  const recurringTopCount = Object.values(recurring).reduce(
    (max, v) => Math.max(max, typeof v === "number" ? v : 0),
    0,
  );

  // Hide the row entirely when there's nothing to say — keeps the
  // factory card uncluttered on a clean cycle.
  if (
    !blocked &&
    autoRestored === 0 &&
    autoDeleted === 0 &&
    allowedCode === 0 &&
    warningCount === 0 &&
    warningReasons.length === 0 &&
    conflictCount === 0 &&
    hardRisky === 0 &&
    !message
  ) {
    return null;
  }

  // Status chip — 4 states map to color tones. Warning is the new
  // "passed with warnings" state; blocked covers true blockers only
  // (secret leak / conflict marker).
  const isRealBlocker = hardRisky > 0 || conflictCount > 0;
  let label;
  let chipColor;
  if (status === "blocked" || isRealBlocker) {
    label = "blocked";
    chipColor = "text-rose-400";
  } else if (status === "warning" || warningCount > 0 || warningReasons.length > 0) {
    label = "passed with warnings";
    chipColor = "text-amber-300";
  } else if (status === "resolved" || autoRestored > 0 || autoDeleted > 0) {
    label = "resolved";
    chipColor = "text-emerald-400";
  } else {
    label = "clean";
    chipColor = "text-slate-400";
  }

  return (
    <div className="text-slate-500">
      <div>
        Release Safety Gate · <span className={chipColor}>{label}</span>
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-0.5">
        {autoRestored > 0 && (
          <span>
            자동 복구 ·{" "}
            <span className="text-emerald-300">{autoRestored}개</span>
          </span>
        )}
        {autoDeleted > 0 && (
          <span>
            자동 삭제 ·{" "}
            <span className="text-emerald-300">{autoDeleted}개</span>
          </span>
        )}
        {allowedCode > 0 && (
          <span>
            정상 코드 ·{" "}
            <span className="text-slate-300">{allowedCode}개</span>
          </span>
        )}
        {warningCount > 0 && (
          <span>
            warning ·{" "}
            <span className="text-amber-300">{warningCount}개</span>
          </span>
        )}
        {conflictCount > 0 && (
          <span>
            conflict ·{" "}
            <span className="text-rose-400">{conflictCount}개</span>
          </span>
        )}
        {hardRisky > 0 && (
          <span>
            secret ·{" "}
            <span className="text-rose-400">{hardRisky}개</span>
          </span>
        )}
      </div>

      {/* Warning reasons — surface the human-readable category list */}
      {warningReasons.slice(0, 3).map((r, i) => (
        <div key={`wr-${i}`} className="text-amber-200/90">
          사유 · {r}
        </div>
      ))}
      {warningReasons.length > 3 && (
        <div className="text-slate-500">
          외 {warningReasons.length - 3}건 — 리포트에서 전체 확인
        </div>
      )}

      {/* Warning files — show first 3 with full path for the human */}
      {warningFiles.slice(0, 3).map((p) => (
        <div key={`m-${p}`} className="text-amber-200/80">
          warning ·{" "}
          <code className="rounded bg-slate-800 px-1 text-amber-200">{p}</code>
        </div>
      ))}
      {warningFiles.length > 3 && (
        <div className="text-slate-500">
          외 {warningFiles.length - 3}건 — 리포트에서 전체 확인
        </div>
      )}

      {/* Conflict markers — absolutely a blocker */}
      {conflictMarkers.slice(0, 3).map((p) => (
        <div key={`c-${p}`} className="text-rose-300">
          conflict ·{" "}
          <code className="rounded bg-slate-800 px-1 text-rose-200">{p}</code>
        </div>
      ))}

      {/* Hard-risky — basenames ONLY. The runner never ships full paths. */}
      {hardRiskyBn.slice(0, 3).map((bn) => (
        <div key={`r-${bn}`} className="text-rose-300">
          secret ·{" "}
          <code className="rounded bg-slate-800 px-1 text-rose-200">{bn}</code>{" "}
          <span className="text-slate-600">(전체 경로 미노출)</span>
        </div>
      ))}

      {/* Auto-cleaned, first 3 — for transparency */}
      {autoRestoredFiles.slice(0, 2).map((p) => (
        <div key={`ar-${p}`} className="text-slate-400">
          restore ·{" "}
          <code className="rounded bg-slate-800 px-1 text-slate-300">{p}</code>
        </div>
      ))}
      {autoDeletedFiles.slice(0, 2).map((p) => (
        <div key={`ad-${p}`} className="text-slate-400">
          delete ·{" "}
          <code className="rounded bg-slate-800 px-1 text-slate-300">{p}</code>
        </div>
      ))}

      {recurringTopCount >= 3 && (
        <div className="text-amber-300/80">
          반복 발생 · 최다 {recurringTopCount}회
          {" — "}
          <span className="text-slate-500">사이클 시작 시 자동 정리 대상</span>
        </div>
      )}

      {reportPath && reportExists && (
        <div>
          리포트 ·{" "}
          <code className="rounded bg-slate-800 px-1 text-slate-300">
            {reportPath}
          </code>
        </div>
      )}

      {isRealBlocker && (
        <div className="mt-1 rounded border border-rose-900/60 bg-rose-950/40 p-1.5 text-[11px] text-rose-200">
          {hardRisky > 0
            ? "Secret 패턴이 감지되어 배포를 중단했습니다."
            : "Git conflict marker가 남아 있어 배포를 중단했습니다."}
        </div>
      )}
      {!isRealBlocker && (warningCount > 0 || warningReasons.length > 0) && (
        <div className="mt-1 rounded border border-amber-900/60 bg-amber-950/30 p-1.5 text-[11px] text-amber-200">
          Release Safety Gate: passed with warnings
          {warningReasons.length > 0 && (
            <>
              {" — "}사유: {warningReasons.slice(0, 2).join(", ")}
            </>
          )}
          {" — "}결과: build/health 통과로 배포 허용
        </div>
      )}
      {message && !isRealBlocker && warningCount === 0 && warningReasons.length === 0 && (
        <div className="text-slate-400">{message}</div>
      )}
    </div>
  );
}

function ProductPlannerRow({ factory }) {
  const pp = factory?.product_planner;
  if (!pp) return null;
  const status = pp.status;
  const bottleneck = pp.bottleneck;
  const selected = pp.selected_feature;
  const pattern = pp.solution_pattern;
  const value = pp.value_summary;
  const llm = pp.llm_needed;
  const dataStorage = pp.data_storage_needed;
  const external = pp.external_integration_needed;
  const fe = pp.frontend_scope;
  const be = pp.backend_scope;
  const success = pp.success_criteria;
  const candCount = pp.candidate_count;
  const at = pp.generated_at;
  const path = pp.report_path;
  const exists = !!pp.report_exists;
  const reason = pp.skipped_reason;
  const gateFails = pp.gate_failures || [];

  let label;
  let chipColor;
  if (status === "generated") {
    label = "ON · 신규 기능 기획 완료";
    chipColor = "text-emerald-400";
  } else if (status === "failed") {
    label = gateFails.length
      ? `ON · 기획 품질 가드 실패 (${gateFails.length}건)`
      : "ON · 기획 실패";
    chipColor = "text-rose-400";
  } else if (status === "skipped" && exists) {
    label = "OFF · 이전 기획 보존";
    chipColor = "text-slate-400";
  } else {
    label = "OFF";
    chipColor = "text-slate-500";
  }

  const isFresh = status === "generated";
  const timeLabel = isFresh ? "" : "마지막 생성: ";

  return (
    <div className="text-slate-500">
      <div>
        제품 기획 · <span className={chipColor}>{label}</span>
        {typeof candCount === "number" && candCount > 0 && status === "generated" && (
          <>
            {" · "}
            <span className="text-slate-400">후보 {candCount}개</span>
          </>
        )}
      </div>
      {bottleneck && status === "generated" && (
        <div className="line-clamp-2 text-amber-200/90">
          가장 큰 병목 · {bottleneck}
        </div>
      )}
      {selected && (
        <div>
          선정 기능 · <span className="text-slate-200">{selected}</span>
        </div>
      )}
      {pattern && status === "generated" && (
        <div>
          해결 패턴 · <span className="text-slate-300">{pattern}</span>
        </div>
      )}
      {value && status === "generated" && (
        <div className="line-clamp-2">가치 · {value}</div>
      )}
      {status === "generated" && (llm || dataStorage || external) && (
        <div className="flex flex-wrap gap-x-3 gap-y-0.5">
          {llm && (
            <span>
              LLM ·{" "}
              <span
                className={
                  llm === "필요" ? "text-amber-300" : "text-slate-300"
                }
              >
                {llm}
              </span>
            </span>
          )}
          {dataStorage && (
            <span>
              데이터 저장 ·{" "}
              <span
                className={
                  dataStorage === "필요" ? "text-amber-300" : "text-slate-300"
                }
              >
                {dataStorage}
              </span>
            </span>
          )}
          {external && (
            <span>
              외부 연동 ·{" "}
              <span
                className={
                  external === "필요" ? "text-amber-300" : "text-slate-300"
                }
              >
                {external}
              </span>
            </span>
          )}
        </div>
      )}
      {fe && status === "generated" && (
        <div className="line-clamp-2">프론트 · {fe}</div>
      )}
      {be && status === "generated" && (
        <div className="line-clamp-2">백엔드 · {be}</div>
      )}
      {success && status === "generated" && (
        <div className="line-clamp-2">성공 기준 · {success}</div>
      )}
      {path && exists && (
        <div>
          리포트 ·{" "}
          <code className="rounded bg-slate-800 px-1 text-slate-300">
            {path}
          </code>
        </div>
      )}
      {at && exists && (
        <div>
          {timeLabel}
          {timeAgo(at)}{" "}
          <span className="text-slate-600">({localTimeStr(at)})</span>
        </div>
      )}
      {gateFails.length > 0 && (
        <div className="mt-1 rounded border border-rose-900/60 bg-rose-950/40 p-1.5 text-[11px] text-rose-200">
          품질 가드 실패:
          <ul className="ml-3 list-disc">
            {gateFails.slice(0, 4).map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </div>
      )}
      {reason && status !== "generated" && status !== "failed" && (
        <div className="text-slate-400">{reason}</div>
      )}
    </div>
  );
}

function ClaudeExecutorRow({ factory }) {
  // Claude Executor Contract surface — only renders when a status is
  // present so untouched cycles stay visually clean. Failure rows are
  // colored by retryability so the operator can tell at a glance
  // whether autopilot will retry on its own or needs a hand.
  const status = factory?.claude_executor_status;
  if (!status || status === "not_run") return null;
  const code = factory?.claude_executor_failure_code;
  const reason = factory?.claude_executor_failure_reason;
  const retryable = !!factory?.claude_executor_retryable;
  const retryCount = factory?.claude_executor_retry_count ?? 0;
  const stdoutPath = factory?.claude_executor_stdout_path;
  const stderrPath = factory?.claude_executor_stderr_path;

  let label = status;
  let chipColor = "text-slate-500";
  if (status === "passed") {
    label = "✓ 정상";
    chipColor = "text-emerald-400";
  } else if (status === "timeout") {
    label = `타임아웃${code ? ` (${code})` : ""}`;
    chipColor = "text-amber-400";
  } else if (status === "retryable_failed") {
    label = `재시도 가능 실패${code ? ` (${code})` : ""}`;
    chipColor = "text-amber-400";
  } else if (status === "failed") {
    label = `실패${code ? ` (${code})` : ""}`;
    chipColor = "text-rose-400";
  }

  return (
    <div className="text-slate-500">
      Claude Executor · <span className={chipColor}>{label}</span>
      {retryCount > 0 && (
        <>
          {" · "}
          <span className="text-slate-400">retry={retryCount}</span>
        </>
      )}
      {status !== "passed" && (
        <>
          {" · "}
          <span className="text-slate-400">
            {retryable ? "auto-retry 예정" : "수동 조치 필요"}
          </span>
        </>
      )}
      {reason && status !== "passed" && (
        <div className="mt-0.5 text-rose-300/80">{reason}</div>
      )}
      {(stdoutPath || stderrPath) && status !== "passed" && (
        <div className="mt-0.5 text-slate-500">
          {stdoutPath && (
            <code className="mr-1 rounded bg-slate-800 px-1 text-slate-300">
              {stdoutPath}
            </code>
          )}
          {stderrPath && (
            <code className="rounded bg-slate-800 px-1 text-slate-300">
              {stderrPath}
            </code>
          )}
        </div>
      )}
    </div>
  );
}

function ClaudeApplyRow({ factory }) {
  const status = factory?.claude_apply_status;
  if (!status) return null;
  const at = factory?.claude_apply_at;
  const count = factory?.claude_apply_changed_count ?? 0;
  const rollback = !!factory?.claude_apply_rollback;
  const reason = factory?.claude_apply_skipped_reason;
  const message = factory?.claude_apply_message;
  const diffPath = factory?.claude_apply_diff_path;
  const diffExists = !!factory?.claude_apply_diff_exists;
  const executorCode = factory?.claude_executor_failure_code;

  let label = "스킵";
  let chipColor = "text-slate-500";
  if (status === "applied") {
    label = "✓ 적용됨";
    chipColor = "text-emerald-400";
  } else if (status === "rolled_back") {
    label = "롤백";
    chipColor = "text-amber-400";
  } else if (status === "cli_failed") {
    label = `CLI 실패${executorCode ? ` (${executorCode})` : ""}`;
    chipColor = "text-rose-400";
  } else if (status === "failed") {
    label = "실패";
    chipColor = "text-rose-400";
  } else if (status === "noop") {
    label = "변경 없음";
    chipColor = "text-slate-400";
  } else if (status === "skipped" && diffExists) {
    label = "스킵 (이전 적용 보존)";
  }

  const isFresh = status === "applied";
  const timeLabel = isFresh ? "" : "마지막 적용: ";

  return (
    <div className="text-slate-500">
      Claude 적용 · <span className={chipColor}>{label}</span>
      {status === "applied" && count > 0 && (
        <>
          {" · "}
          <span className="text-slate-300">{count}개 파일 변경</span>
        </>
      )}
      {rollback && (
        <>
          {" · "}
          <span className="text-amber-300">자동 rollback 수행됨</span>
        </>
      )}
      {diffPath && diffExists && (
        <>
          {" · "}
          <code className="rounded bg-slate-800 px-1 text-slate-300">
            {diffPath}
          </code>
        </>
      )}
      {at && diffExists && (
        <>
          {" · "}
          <span>
            {timeLabel}
            {timeAgo(at)}
          </span>{" "}
          <span className="text-slate-600">({localTimeStr(at)})</span>
        </>
      )}
      {reason && status !== "applied" && status !== "rolled_back" && (
        <>
          {" · "}
          <span className="text-slate-400">{reason}</span>
        </>
      )}
      {message && (status === "rolled_back" || status === "failed") && (
        <div className="mt-0.5 text-rose-300/80">{message}</div>
      )}
    </div>
  );
}

function ClaudeProposalRow({ factory }) {
  const status = factory?.claude_proposal_status;
  if (!status) return null;
  const at = factory?.claude_proposal_at;
  const path = factory?.claude_proposal_path;
  const exists = !!factory?.claude_proposal_exists;
  const reason = factory?.claude_proposal_skipped_reason;

  let label = "스킵";
  let chipColor = "text-slate-500";
  if (status === "generated") {
    label = exists ? "✓ 생성됨" : "생성됨 (파일 없음)";
    chipColor = "text-emerald-400";
  } else if (status === "failed") {
    label = "실패";
    chipColor = "text-rose-400";
  } else if (status === "skipped" && exists) {
    label = "스킵 (이전 제안 보존)";
  }

  // The `at` timestamp comes from the current cycle when status is
  // "generated", or falls back to the proposal file's mtime in
  // runner.py when this cycle skipped. We show it in both cases —
  // labelled as "마지막 생성" when it's a fallback so the user knows
  // it's not from this tick.
  const isFresh = status === "generated";
  const timeLabel = isFresh ? "" : "마지막 생성: ";

  return (
    <div className="text-slate-500">
      Claude 제안 · <span className={chipColor}>{label}</span>
      {path && exists && (
        <>
          {" · "}
          <code className="rounded bg-slate-800 px-1 text-slate-300">
            {path}
          </code>
        </>
      )}
      {at && exists && (
        <>
          {" · "}
          <span>
            {timeLabel}
            {timeAgo(at)}
          </span>{" "}
          <span className="text-slate-600">({localTimeStr(at)})</span>
        </>
      )}
      {reason && status !== "generated" && (
        <>
          {" · "}
          <span className="text-slate-400">{reason}</span>
        </>
      )}
    </div>
  );
}

function RunnerVersionRow({ runner }) {
  const meta = runner.metadata_json?.runner;
  if (!meta) return null;
  const dirtyCount = meta.dirty_files_count ?? 0;
  const restartDryRun = !!meta.restart_dry_run;
  return (
    <div className="mt-1.5 grid gap-0.5 text-[11px] text-slate-500">
      <div>
        Runner PID <span className="text-slate-300">{meta.pid ?? "—"}</span>
        {meta.started_at && (
          <>
            {" · 시작 "}
            <span>{timeAgo(meta.started_at)}</span>{" "}
            <span className="text-slate-600">({localTimeStr(meta.started_at)})</span>
          </>
        )}
      </div>
      <div>
        브랜치 <span className="text-slate-300">{meta.git_branch || "—"}</span>
        {meta.git_commit && (
          <>
            {" @ "}
            <code className="rounded bg-slate-800 px-1 text-slate-300">
              {meta.git_commit}
            </code>
          </>
        )}
        {dirtyCount > 0 && (
          <span className="text-amber-300"> · 로컬 변경 {dirtyCount}개</span>
        )}
        {restartDryRun && (
          <span className="ml-1 rounded bg-sky-900/60 px-1.5 py-0.5 text-[10px] text-sky-300">
            RESTART DRY-RUN
          </span>
        )}
      </div>
      {meta.code_mtime_at && (
        <div>
          코드 mtime <span>{timeAgo(meta.code_mtime_at)}</span>{" "}
          <span className="text-slate-600">({localTimeStr(meta.code_mtime_at)})</span>
        </div>
      )}
    </div>
  );
}

function PublishPanel({ runner, factory, isPending, onSend, onRefresh }) {
  const publish = factory?.publish;
  if (!publish) return null;

  const offline = runner.status === "offline";
  const ready = !!publish.ready;
  const blockedReason = publish.blocked_reason;
  const dryRun = publish.dry_run !== false;
  const allowPublish = !!publish.allow_publish;
  const changed = publish.changed_files || [];
  const allowed = publish.allowed_files || [];
  const allowedCount = publish.allowed_count ?? allowed.length;
  const changedCount = publish.changed_count ?? changed.length;
  const blockedCount = publish.blocked_count ?? 0;
  const riskyCount = publish.risky_count ?? 0;
  const lastStatus = publish.last_push_status;
  const lastAt = publish.last_push_at;
  const lastCommit = publish.last_commit_hash;
  const lastMessage = publish.last_publish_message;
  const actionsUrl = publish.actions_url;
  const lastStage = publish.last_failed_stage;

  // Disable conditions consolidated here so both styling and click
  // handler use the exact same source of truth.
  const refreshPending = isPending(runner.id, "status");
  const deployPending = isPending(runner.id, "publish_changes");
  const deployDisabled = offline || !ready || deployPending || changedCount === 0;

  const lastTone =
    lastStatus === "succeeded"
      ? "text-emerald-400"
      : lastStatus === "dry_run"
      ? "text-sky-300"
      : lastStatus === "noop"
      ? "text-slate-400"
      : lastStatus === "failed"
      ? "text-rose-400"
      : "text-slate-500";

  return (
    <section className="mt-2 rounded-lg border border-slate-800 bg-slate-900/50 p-2.5">
      <div className="flex items-center justify-between gap-2">
        <div className="text-[12px] font-semibold text-slate-100">배포 관리</div>
        <div className="flex items-center gap-1.5 text-[10.5px] uppercase tracking-wide">
          {dryRun && (
            <span className="rounded bg-sky-900/60 px-1.5 py-0.5 text-sky-300">
              DRY-RUN
            </span>
          )}
          {!allowPublish && (
            <span className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-300">
              실제 push 비활성화
            </span>
          )}
        </div>
      </div>

      <div className="mt-1.5 grid gap-0.5 text-[11.5px] text-slate-400">
        <div>
          변경 파일 ·{" "}
          <span className="text-slate-200">{changedCount}개</span>
          {allowedCount !== changedCount && (
            <span className="text-slate-500">
              {" "}
              (허용 {allowedCount} / 차단 {blockedCount} / 위험 {riskyCount})
            </span>
          )}
        </div>
        {allowed.length > 0 && (
          <div className="line-clamp-3 text-slate-300">
            {allowed.slice(0, 5).map((p, i) => (
              <span key={p}>
                {i > 0 && ", "}
                <code className="rounded bg-slate-800 px-1 text-slate-300">{p}</code>
              </span>
            ))}
            {allowed.length > 5 && (
              <span className="text-slate-500"> · 외 {allowed.length - 5}건</span>
            )}
          </div>
        )}
        {ready ? (
          <div className="text-emerald-400">배포 준비 완료</div>
        ) : (
          <div className="text-amber-300">차단: {blockedReason}</div>
        )}
        {lastStatus && (
          <div className={lastTone}>
            마지막 push · {lastStatus}
            {lastCommit && (
              <>
                {" · "}
                <code className="rounded bg-slate-800 px-1 text-slate-300">
                  {String(lastCommit).slice(0, 8)}
                </code>
              </>
            )}
            {lastAt && (
              <>
                {" · "}
                <span>{timeAgo(lastAt)}</span>{" "}
                <span className="text-slate-600">({localTimeStr(lastAt)})</span>
              </>
            )}
            {lastStage && (
              <span className="text-rose-300/80"> · stage={lastStage}</span>
            )}
          </div>
        )}
        {lastMessage && (
          <div className="line-clamp-2 text-slate-500">메시지 · {lastMessage}</div>
        )}
        {actionsUrl && (
          <div className="text-slate-500">
            GitHub Actions ·{" "}
            <a
              href={actionsUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sky-400 hover:text-sky-300"
            >
              {actionsUrl}
            </a>
          </div>
        )}
      </div>

      <div className="mt-2 grid grid-cols-2 gap-2">
        <Btn
          onClick={onRefresh}
          disabled={offline || refreshPending}
        >
          {refreshPending ? "전송 중..." : "변경사항 확인"}
        </Btn>
        <Btn
          tone="primary"
          onClick={onSend}
          disabled={deployDisabled}
        >
          {deployPending ? "배포 명령 전송 중..." : "배포하기"}
        </Btn>
      </div>
    </section>
  );
}

// Per-runner operator-fix request panel. Lets the admin type a free
// form bug/improvement request and dispatch it as either an
// operator_fix_request command (no auto-deploy) or an
// operator_fix_and_publish command (auto-deploy if QA passes).
function OperatorFixPanel({ runner, factory, isPending, sendWithPayload }) {
  const offline = runner.status === "offline";
  const op = factory?.operator_fix || {};
  const status = op.status || "idle";
  const [text, setText] = useState("");
  const [mode, setMode] = useState("fix_only"); // "fix_only" | "fix_and_publish"

  // Disable while a command is in-flight or while there's nothing typed.
  const fixOnlyPending = isPending(runner.id, "operator_fix_request");
  const fixAndPublishPending = isPending(runner.id, "operator_fix_and_publish");
  const sendDisabled =
    offline ||
    fixOnlyPending ||
    fixAndPublishPending ||
    !text.trim();

  const onSend = async () => {
    const command =
      mode === "fix_and_publish"
        ? "operator_fix_and_publish"
        : "operator_fix_request";
    const payload = {
      request: text.trim().slice(0, 6000),
      priority: "normal",
      allow_publish: mode === "fix_and_publish",
    };
    const ok = await sendWithPayload(runner.id, command, payload);
    if (ok) setText("");
  };

  // Status chip tone. The statuses come straight from the runner-side
  // operator_fix_state.json — keep them in sync with that file's
  // state machine: idle | running | applied | qa_failed | published |
  // failed.
  const STATUS_LABEL = {
    idle:       { label: "대기 중",       color: "text-slate-400" },
    running:    { label: "처리 중",       color: "text-amber-300" },
    applied:    { label: "수정 + QA 통과", color: "text-emerald-400" },
    qa_failed:  { label: "QA 실패",       color: "text-rose-400" },
    published:  { label: "수정 + 배포 완료", color: "text-emerald-300" },
    failed:     { label: "실패",          color: "text-rose-400" },
  };
  const statusChip = STATUS_LABEL[status] || STATUS_LABEL.idle;

  return (
    <section className="mt-2 rounded-lg border border-slate-800 bg-slate-900/50 p-2.5">
      <div className="flex items-center justify-between gap-2">
        <div className="text-[12px] font-semibold text-slate-100">
          운영자 수정 요청
        </div>
        <span className={`text-[11px] ${statusChip.color}`}>{statusChip.label}</span>
      </div>

      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="예: '/plan 결과 화면에서 공유 버튼이 클릭이 안 됩니다. 모바일 사파리에서도 안 됩니다.'"
        rows={3}
        maxLength={6000}
        className="mt-1.5 block w-full resize-y rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-[12px] text-slate-100 placeholder:text-slate-600 focus:border-sky-400 focus:outline-none"
      />
      <div className="mt-1 flex items-center justify-between text-[11px] text-slate-500">
        <span>{text.length} / 6000</span>
        <span>secret/token은 자동 마스킹됩니다.</span>
      </div>

      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11.5px] text-slate-400">
        <label className="inline-flex items-center gap-1.5">
          <input
            type="radio"
            checked={mode === "fix_only"}
            onChange={() => setMode("fix_only")}
            className="accent-sky-400"
          />
          수정만 (자동 배포 X)
        </label>
        <label className="inline-flex items-center gap-1.5">
          <input
            type="radio"
            checked={mode === "fix_and_publish"}
            onChange={() => setMode("fix_and_publish")}
            className="accent-sky-400"
          />
          수정 + QA 통과 시 배포
        </label>
      </div>

      <div className="mt-2 grid grid-cols-1 gap-2">
        <Btn
          tone={mode === "fix_and_publish" ? "primary" : "default"}
          onClick={onSend}
          disabled={sendDisabled}
        >
          {fixOnlyPending || fixAndPublishPending
            ? "요청 전송 중..."
            : mode === "fix_and_publish"
            ? "수정 후 배포 요청 보내기"
            : "수정 요청 보내기"}
        </Btn>
      </div>

      {(op.last_message || op.request_path || op.changed_count > 0) && (
        <div className="mt-2 grid gap-0.5 text-[11px] text-slate-500">
          {op.started_at && (
            <div>
              마지막 요청 · {timeAgo(op.started_at)}{" "}
              <span className="text-slate-600">({localTimeStr(op.started_at)})</span>
            </div>
          )}
          {op.last_message && (
            <div className="line-clamp-3 text-slate-400">
              결과 · {op.last_message}
            </div>
          )}
          {op.changed_count > 0 && (
            <div>
              변경 파일 · <span className="text-slate-300">{op.changed_count}건</span>
              {op.changed_files && op.changed_files.length > 0 && (
                <span className="text-slate-500">
                  {" "}
                  ·{" "}
                  {op.changed_files.slice(0, 3).map((p, i) => (
                    <span key={p}>
                      {i > 0 && ", "}
                      <code className="rounded bg-slate-800 px-1 text-slate-300">
                        {p}
                      </code>
                    </span>
                  ))}
                  {op.changed_files.length > 3 && (
                    <span> 외 {op.changed_files.length - 3}건</span>
                  )}
                </span>
              )}
            </div>
          )}
          {op.publish_status && op.publish_status !== "not_requested" && (
            <div>
              배포 ·{" "}
              <span
                className={
                  op.publish_status === "published"
                    ? "text-emerald-400"
                    : "text-rose-300"
                }
              >
                {op.publish_status}
              </span>
              {op.last_commit_hash && (
                <>
                  {" · "}
                  <code className="rounded bg-slate-800 px-1 text-slate-300">
                    {String(op.last_commit_hash).slice(0, 8)}
                  </code>
                </>
              )}
            </div>
          )}
          {op.qa_failed_reason && status === "qa_failed" && (
            <div className="text-rose-300/90">QA 사유 · {op.qa_failed_reason}</div>
          )}
          {op.request_path && op.request_exists && (
            <div>
              요청 파일 ·{" "}
              <code className="rounded bg-slate-800 px-1 text-slate-300">
                {op.request_path}
              </code>
            </div>
          )}
          {op.qa_report_path && op.qa_report_exists && (
            <div>
              QA 리포트 ·{" "}
              <code className="rounded bg-slate-800 px-1 text-slate-300">
                {op.qa_report_path}
              </code>
            </div>
          )}
          {op.qa_feedback_path && op.qa_feedback_exists && (
            <div>
              QA Feedback ·{" "}
              <code className="rounded bg-slate-800 px-1 text-slate-300">
                {op.qa_feedback_path}
              </code>
            </div>
          )}
          {op.redactions && op.redactions.length > 0 && (
            <div className="text-amber-300/80">
              마스킹된 항목 · {op.redactions.join(", ")}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function FactoryDetail({ factory }) {
  if (!factory) return null;
  const status = factory.status || "—";
  const tone = FACTORY_TONE[status] || "bg-slate-700 text-slate-200";
  const progress = typeof factory.progress === "number" ? factory.progress : null;
  const risky = Array.isArray(factory.risky_files) ? factory.risky_files : [];
  return (
    <div className="mt-2 rounded-lg border border-slate-800 bg-slate-900/50 p-2.5">
      <div className="flex items-center justify-between gap-2">
        <div className="text-[12px] font-semibold text-slate-100">
          로컬 자동 공장 #{factory.cycle ?? "—"}
        </div>
        <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${tone}`}>
          {status}
        </span>
      </div>
      <div className="mt-1 grid gap-0.5 text-[11.5px] text-slate-400">
        <div>
          단계 · <span className="text-slate-200">{factory.current_stage || "—"}</span>
        </div>
        {factory.current_task && (
          <div>
            작업 · <span className="text-slate-200">{factory.current_task}</span>
          </div>
        )}
        {factory.last_message && (
          <div className="line-clamp-2">
            메시지 · {factory.last_message}
          </div>
        )}
        <div className="text-slate-500">
          PID {factory.pid ?? "—"} ·{" "}
          {factory.alive ? (
            <span className="text-emerald-400">살아있음</span>
          ) : (
            <span className="text-rose-400">죽음</span>
          )}
        </div>
        {factory.updated_at && (
          <div className="text-slate-500">
            마지막 갱신 · {timeAgo(factory.updated_at)}{" "}
            <span className="text-slate-600">({localTimeStr(factory.updated_at)})</span>
          </div>
        )}
        {factory.report_exists && (
          <div className="text-slate-500">
            리포트 ·{" "}
            <code className="rounded bg-slate-800 px-1 text-slate-300">
              {factory.report_path}
            </code>
          </div>
        )}
        <PublishBlockerRow factory={factory} />
        <ClaudeExecutorRow factory={factory} />
        <ProductPlannerRow factory={factory} />
        <ClaudeProposalRow factory={factory} />
        <ClaudeApplyRow factory={factory} />
        <QaGateRow factory={factory} />
      </div>
      {progress !== null && (
        <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
          <div
            className="h-full bg-sky-400 transition-all"
            style={{ width: `${Math.max(0, Math.min(100, progress))}%` }}
          />
        </div>
      )}
      {risky.length > 0 && (
        <div className="mt-2 rounded border border-rose-900/60 bg-rose-950/40 p-2 text-[11px] text-rose-200">
          ⚠️ Secret 패턴 파일 {risky.length}개 감지 — 자동 commit 비활성화
          <ul className="ml-4 list-disc">
            {risky.slice(0, 5).map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export default function RunnerPanel({ runners = [], onChanged }) {
  // Per-(runner,command) timestamp of the last successful enqueue.
  // We use this both to render a brief "전송 중..." label and to suppress
  // the click handler entirely if it fires inside the dedupe window.
  const [pending, setPending] = useState({});

  if (runners.length === 0) {
    return (
      <section className="rounded-2xl border border-dashed border-slate-800 bg-slate-950/40 p-4">
        <h2 className="text-[14px] font-semibold text-slate-100">로컬 자동 공장</h2>
        <p className="mt-2 text-[12.5px] text-slate-500">
          아직 등록된 러너가 없습니다. Mac에서{" "}
          <code className="rounded bg-slate-800 px-1 text-slate-300">
            python3 -m control_tower.local_runner.runner
          </code>{" "}
          를 실행하면 여기에 자동으로 표시됩니다.
        </p>
      </section>
    );
  }

  const send = (rid, command) => async () => {
    const key = `${rid}:${command}`;
    const last = pending[key] || 0;
    const now = Date.now();
    if (now - last < COMMAND_DEDUPE_MS) return;
    setPending((p) => ({ ...p, [key]: now }));
    try {
      await sendRunnerCommand(rid, command);
      await onChanged?.();
    } catch (e) {
      console.error(e);
      alert(`명령 전송 실패: ${e.message}`);
    } finally {
      // Allow another click after the dedupe window closes naturally —
      // we just leave the timestamp; isPending() below reads it.
    }
  };

  // Same dedupe semantics as `send`, but accepts a custom payload —
  // operator_fix_request needs to ship the typed text along with the
  // command name. Returns whether the dispatch succeeded so the
  // caller can clear its textarea.
  const sendWithPayload = async (rid, command, payload) => {
    const key = `${rid}:${command}`;
    const last = pending[key] || 0;
    const now = Date.now();
    if (now - last < COMMAND_DEDUPE_MS) return false;
    setPending((p) => ({ ...p, [key]: now }));
    try {
      await sendRunnerCommand(rid, command, payload);
      await onChanged?.();
      return true;
    } catch (e) {
      console.error(e);
      alert(`명령 전송 실패: ${e.message}`);
      return false;
    }
  };

  const isPending = (rid, command) =>
    Date.now() - (pending[`${rid}:${command}`] || 0) < COMMAND_DEDUPE_MS;

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-950/70 p-3 sm:p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-[14px] font-semibold tracking-wide text-slate-100">
          로컬 자동 공장
        </h2>
        <span className="text-[11px] tracking-wide text-slate-500">
          러너 {runners.length}개
        </span>
      </div>

      <ul className="space-y-2">
        {runners.map((r) => {
          const tone = STATUS_TONE[r.status] || STATUS_TONE.offline;
          const offline = r.status === "offline";
          const factory = r.metadata_json?.local_factory;
          return (
            <li
              key={r.id}
              className="rounded-xl border border-slate-800 bg-slate-900/50 p-3"
            >
              <div className="flex items-baseline justify-between gap-2">
                <div>
                  <div className="text-[14px] font-semibold text-slate-100">
                    {r.name}
                  </div>
                  <div className="text-[11px] text-slate-500">
                    {r.id} · 마지막 heartbeat {timeAgo(r.last_heartbeat_at)}
                    {r.last_heartbeat_at && (
                      <span className="text-slate-600">
                        {" "}
                        ({localTimeStr(r.last_heartbeat_at)})
                      </span>
                    )}
                  </div>
                </div>
                <span
                  className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold ${tone.chip}`}
                >
                  {tone.label}
                </span>
              </div>

              {(r.current_command || r.last_result) && (
                <div className="mt-1.5 grid gap-1 text-[11.5px] text-slate-400">
                  {r.current_command && (
                    <div>
                      <span className="text-slate-500">현재 ·</span>{" "}
                      <span className="text-amber-300">{r.current_command}</span>
                    </div>
                  )}
                  {r.last_result && (
                    <div className="line-clamp-2">
                      <span className="text-slate-500">결과 ·</span>{" "}
                      {r.last_result}
                    </div>
                  )}
                </div>
              )}

              <RunnerVersionRow runner={r} />
              <FactoryDetail factory={factory} />

              <PublishPanel
                runner={r}
                factory={factory}
                isPending={isPending}
                onSend={send(r.id, "publish_changes")}
                onRefresh={send(r.id, "status")}
              />

              <OperatorFixPanel
                runner={r}
                factory={factory}
                isPending={isPending}
                sendWithPayload={sendWithPayload}
              />

              <div className="mt-2.5 grid grid-cols-3 gap-2 sm:grid-cols-4">
                <Btn
                  tone="primary"
                  onClick={send(r.id, "start_factory")}
                  disabled={offline || isPending(r.id, "start_factory")}
                >
                  {isPending(r.id, "start_factory") ? "전송 중..." : "시작"}
                </Btn>
                <Btn
                  tone="warn"
                  onClick={send(r.id, "pause_factory")}
                  disabled={offline || isPending(r.id, "pause_factory")}
                >
                  {isPending(r.id, "pause_factory") ? "전송 중..." : "일시정지"}
                </Btn>
                <Btn
                  tone="primary"
                  onClick={send(r.id, "resume_factory")}
                  disabled={offline || isPending(r.id, "resume_factory")}
                >
                  {isPending(r.id, "resume_factory") ? "전송 중..." : "재개"}
                </Btn>
                <Btn
                  tone="danger"
                  onClick={send(r.id, "stop_factory")}
                  disabled={offline || isPending(r.id, "stop_factory")}
                >
                  {isPending(r.id, "stop_factory") ? "전송 중..." : "중지"}
                </Btn>
                <Btn
                  onClick={send(r.id, "restart_factory")}
                  disabled={offline || isPending(r.id, "restart_factory")}
                >
                  {isPending(r.id, "restart_factory") ? "전송 중..." : "재시작"}
                </Btn>
                <Btn
                  onClick={send(r.id, "build_check")}
                  disabled={offline || isPending(r.id, "build_check")}
                >
                  {isPending(r.id, "build_check") ? "전송 중..." : "빌드 확인"}
                </Btn>
                <Btn
                  onClick={send(r.id, "test_check")}
                  disabled={offline || isPending(r.id, "test_check")}
                >
                  {isPending(r.id, "test_check") ? "전송 중..." : "테스트 확인"}
                </Btn>
                <Btn
                  onClick={send(r.id, "status")}
                  disabled={offline || isPending(r.id, "status")}
                >
                  {isPending(r.id, "status") ? "전송 중..." : "상태"}
                </Btn>
              </div>

              <div className="mt-2 grid grid-cols-3 gap-2">
                <Btn
                  onClick={send(r.id, "restart_runner")}
                  disabled={offline || isPending(r.id, "restart_runner")}
                >
                  {isPending(r.id, "restart_runner") ? "전송 중..." : "러너 재시작"}
                </Btn>
                <Btn
                  onClick={send(r.id, "restart_factory")}
                  disabled={offline || isPending(r.id, "restart_factory")}
                >
                  {isPending(r.id, "restart_factory") ? "전송 중..." : "로컬 공장 재시작"}
                </Btn>
                <Btn
                  onClick={send(r.id, "update_runner")}
                  disabled={
                    offline ||
                    isPending(r.id, "update_runner") ||
                    (r.metadata_json?.runner?.dirty_files_count ?? 0) > 0
                  }
                  title={
                    (r.metadata_json?.runner?.dirty_files_count ?? 0) > 0
                      ? "로컬 변경사항이 있어 update_runner 비활성화"
                      : ""
                  }
                >
                  {isPending(r.id, "update_runner") ? "전송 중..." : "러너 업데이트"}
                </Btn>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
