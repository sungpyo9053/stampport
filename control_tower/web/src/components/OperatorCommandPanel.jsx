import { useEffect, useRef, useState } from "react";
import { sendRunnerCommand } from "../api/controlTowerApi.js";

// Operator command panel — the dashboard's "iPhone in your pocket"
// channel. Lets the operator type a free-form Korean instruction
// ("스탬프 결과 화면을 더 세련되게 바꿔줘") and ship it to the
// local runner as an operator_request command. The runner hands the
// prompt off to Claude Code (via LOCAL_RUNNER_CLAUDE_COMMAND) which
// can edit / validate / commit / push autonomously.
//
// Disabled when:
//   - no runner is reachable (offline)
//   - the runner already has a current_command in flight
//   - the panel just sent a request and is waiting for the API
//     response (in-flight guard, prevents double-fire from a phone
//     tap that fires twice).

const PLACEHOLDER =
  "예: 스탬프 생성 화면을 더 세련되게 바꾸고, 가게 이름만으로는 도장을 못 찍게 해줘.";

const MAX_CHARS = 6000;

// Module-level lock so a StrictMode double-mount or a re-render
// mid-click can't enqueue the same prompt twice.
let _operatorRequestInflight = null;

export default function OperatorCommandPanel({ runners = [], onSent }) {
  const [text, setText] = useState("");
  const [status, setStatus] = useState(null); // {kind:"ok"|"err", message}
  const [busy, setBusy] = useState(false);
  const textRef = useRef(null);

  // Pick the first runner whose heartbeat is still alive — including
  // a `busy` one mid-publish. The send button itself disables when a
  // current_command is set, so we never send to a busy runner; but
  // the metadata we surface ("러너 사용 중") still needs to read off
  // that runner.
  const reachableRunner =
    runners.find((r) => r?.status === "online") ||
    runners.find((r) => r?.status !== "offline") ||
    null;
  const runnerOnline = reachableRunner?.status === "online";
  const runnerBusy =
    !!reachableRunner?.current_command || reachableRunner?.status === "busy";

  // Disable rules — first match wins for both the visual label and
  // the click handler.
  const reason = !reachableRunner
    ? "러너 오프라인 — 맥북 runner를 켜야 작업 지시를 받을 수 있어요"
    : !runnerOnline && runnerBusy
    ? `러너 사용 중 (현재 명령: ${reachableRunner?.current_command || "busy"})`
    : runnerBusy
    ? "러너가 다른 명령을 실행 중입니다"
    : !text.trim()
    ? null   // not an error, just disables the button quietly
    : busy
    ? "전송 중..."
    : null;

  const trimmedLen = text.trim().length;
  const canSend =
    !!reachableRunner && runnerOnline && !runnerBusy && trimmedLen > 0 && !busy;

  // Auto-grow textarea so a long prompt doesn't get clipped on mobile.
  useEffect(() => {
    if (!textRef.current) return;
    textRef.current.style.height = "auto";
    textRef.current.style.height =
      Math.min(280, textRef.current.scrollHeight) + "px";
  }, [text]);

  const handleSend = async () => {
    if (!canSend) return;
    if (_operatorRequestInflight) {
      setStatus({
        kind: "err",
        message: "이미 작업 지시 요청이 진행 중입니다.",
      });
      return;
    }
    const prompt = text.trim().slice(0, MAX_CHARS);
    setBusy(true);
    setStatus(null);
    _operatorRequestInflight = (async () => {
      try {
        await sendRunnerCommand(reachableRunner.id, "operator_request", {
          prompt,
          auto_commit_push: true,
        });
        setText("");
        setStatus({
          kind: "ok",
          message: "작업 지시 전송 완료 — runner가 받아 처리합니다.",
        });
        await onSent?.();
      } catch (err) {
        setStatus({
          kind: "err",
          message: `전송 실패 — ${err?.message || "알 수 없는 오류"}`,
        });
      } finally {
        setBusy(false);
        _operatorRequestInflight = null;
      }
    })();
    await _operatorRequestInflight;
  };

  return (
    <section
      className="flex flex-col gap-2 p-3"
      data-testid="operator-command-panel"
      style={{
        backgroundColor: "#0e1a35",
        border: "1.5px solid #d4a84355",
        borderRadius: 6,
        boxShadow: "0 0 16px #d4a84322",
        fontFamily: "ui-monospace, monospace",
      }}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span
            className="inline-block h-2 w-2"
            style={{ backgroundColor: "#d4a843" }}
          />
          <span className="text-[10px] font-bold uppercase tracking-[0.3em] text-[#d4a843]">
            CLAUDE에게 작업 지시
          </span>
        </div>
        <span
          className="rounded px-1.5 py-0.5 text-[9px] font-bold tracking-widest"
          style={{
            color: runnerOnline ? "#34d399" : reachableRunner ? "#fbbf24" : "#f87171",
            border: `1px solid ${
              runnerOnline ? "#34d39966" : reachableRunner ? "#fbbf2466" : "#f8717166"
            }`,
            backgroundColor: "#0a1228",
          }}
        >
          {runnerOnline
            ? "RUNNER ONLINE"
            : reachableRunner
            ? "RUNNER BUSY"
            : "RUNNER OFFLINE"}
        </span>
      </div>

      <p className="text-[10.5px] leading-snug text-slate-400">
        맥북 runner가 이 요청을 받아 Claude Code로 수정 → 검증 → commit → push
        까지 수행합니다. main 푸시 후 GitHub Actions가 서버 자동 배포합니다.
      </p>

      <textarea
        ref={textRef}
        value={text}
        onChange={(e) => setText(e.target.value.slice(0, MAX_CHARS))}
        placeholder={PLACEHOLDER}
        spellCheck={false}
        autoCorrect="off"
        autoCapitalize="off"
        rows={4}
        className="w-full resize-none rounded p-2 text-[12px] leading-snug outline-none focus:ring-1"
        style={{
          backgroundColor: "#0a1228",
          color: "#f5e9d3",
          border: "1px solid #0e4a3a",
          minHeight: 92,
          maxHeight: 280,
          fontFamily: "ui-monospace, monospace",
        }}
      />

      <div className="flex flex-wrap items-center justify-between gap-2 text-[10px] tracking-wider">
        <span className="text-slate-500">
          {trimmedLen} / {MAX_CHARS}자
        </span>
        <div className="flex items-center gap-2">
          {reason && (
            <span className="text-[10px] tracking-wider text-amber-300">
              {reason}
            </span>
          )}
          <button
            type="button"
            onClick={handleSend}
            disabled={!canSend}
            className="px-3 py-1.5 text-[11px] font-bold tracking-[0.2em] transition disabled:opacity-50"
            style={{
              backgroundColor: canSend ? "#d4a843" : "#1a2540",
              color: canSend ? "#0a1228" : "#475569",
              border: `1.5px solid ${canSend ? "#d4a843" : "#1a2540"}`,
              borderRadius: 3,
              cursor: canSend ? "pointer" : "not-allowed",
              boxShadow: canSend ? "0 0 12px #d4a84355" : "none",
            }}
          >
            {busy ? "전송 중..." : "▶ 작업 지시 보내기"}
          </button>
        </div>
      </div>

      {status && (
        <div
          className="rounded px-2 py-1.5 text-[10.5px] tracking-wider"
          style={{
            backgroundColor: status.kind === "ok" ? "#0a2a1f" : "#3d0a14",
            color: status.kind === "ok" ? "#86efac" : "#fecaca",
            border: `1px solid ${
              status.kind === "ok" ? "#34d39966" : "#f8717166"
            }`,
          }}
        >
          {status.kind === "ok" ? "✅ " : "⚠ "}
          {status.message}
        </div>
      )}
    </section>
  );
}
