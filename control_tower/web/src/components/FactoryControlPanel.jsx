import { useState } from "react";
import {
  startFactory,
  pauseFactory,
  resumeFactory,
  stopFactory,
  resetFactory,
  setFactoryContinuous,
} from "../api/controlTowerApi.js";

// Mobile-first factory control panel. Lives at the top of the page so
// the iPhone user can see status + buttons without scrolling.

const STATUS_META = {
  idle:       { label: "대기 중",       chip: "bg-slate-700 text-slate-200" },
  running:    { label: "실행 중",       chip: "bg-amber-500 text-slate-950" },
  paused:     { label: "일시정지",       chip: "bg-violet-500 text-slate-950" },
  stopping:   { label: "중지 중...",    chip: "bg-orange-500 text-slate-950" },
  stopped:    { label: "중지됨",        chip: "bg-rose-500 text-slate-50" },
  completed:  { label: "배포 완료",     chip: "bg-emerald-500 text-slate-950" },
  failed:     { label: "실패",          chip: "bg-rose-600 text-slate-50" },
};

function Btn({ children, onClick, disabled, tone = "default" }) {
  const toneClass = {
    default:  "bg-slate-800 text-slate-200 hover:bg-slate-700",
    primary:  "bg-sky-500 text-slate-950 hover:bg-sky-400",
    warn:     "bg-amber-500 text-slate-950 hover:bg-amber-400",
    danger:   "bg-rose-500 text-slate-50 hover:bg-rose-400",
    ghost:    "bg-transparent text-slate-300 ring-1 ring-slate-700 hover:ring-slate-500",
  }[tone];
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`flex-1 rounded-lg px-3 py-2 text-[12.5px] font-semibold transition ${
        disabled ? "cursor-not-allowed bg-slate-800/60 text-slate-600" : toneClass
      }`}
    >
      {children}
    </button>
  );
}

export default function FactoryControlPanel({ factory, onChanged }) {
  const [busy, setBusy] = useState(null);

  const status = factory?.status || "idle";
  const meta = STATUS_META[status] || STATUS_META.idle;

  const wrap = (label, fn) => async () => {
    if (busy) return;
    setBusy(label);
    try {
      await fn();
      await onChanged?.();
    } catch (e) {
      console.error(e);
    } finally {
      setBusy(null);
    }
  };

  const canStart = ["idle", "stopped", "completed", "failed"].includes(status);
  const canPause = status === "running";
  const canResume = status === "paused";
  const canStop = ["running", "paused"].includes(status);
  const canReset = status !== "running"; // only reset when not actively running

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-950/70 p-3 sm:p-4">
      <div className="flex items-center justify-between gap-2">
        <div>
          <div className="text-[11px] tracking-wide text-slate-500">자동 공장</div>
          <div className="mt-0.5 text-[15px] font-semibold text-slate-50">
            Stampport 제작소 · AI Agent Studio
          </div>
        </div>
        <span
          className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11.5px] font-semibold ${meta.chip}`}
        >
          {status === "running" && (
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-slate-950/50" />
          )}
          {meta.label}
        </span>
      </div>

      {factory?.current_stage && (
        <div className="mt-2 text-[12px] text-slate-400">
          현재 단계 ·{" "}
          <span className="text-slate-200">{factory.current_stage}</span>
        </div>
      )}
      {factory?.last_message && (
        <div className="mt-1 text-[12px] text-slate-500">
          {factory.last_message}
        </div>
      )}
      {factory?.desired_status && factory.desired_status !== status && (
        <div className="mt-1 text-[11.5px] text-amber-300">
          희망 상태 · <span className="font-semibold">{factory.desired_status}</span>{" "}
          <span className="text-amber-300/60">(워치독이 곧 맞춥니다)</span>
        </div>
      )}
      {typeof factory?.run_count === "number" && factory.run_count > 0 && (
        <div className="mt-1 text-[11.5px] text-slate-500">
          누적 실행 · {factory.run_count}회
        </div>
      )}

      <label className="mt-3 flex items-center justify-between gap-2 rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2">
        <div>
          <div className="text-[12px] font-semibold text-slate-200">
            계속 실행 모드
          </div>
          <div className="text-[11px] text-slate-500">
            한 사이클이 끝나면 자동으로 다음 사이클을 시작합니다.
          </div>
        </div>
        <input
          type="checkbox"
          className="h-5 w-5 cursor-pointer accent-emerald-500"
          checked={!!factory?.continuous_mode}
          disabled={!!busy}
          onChange={wrap("continuous", () =>
            setFactoryContinuous(!factory?.continuous_mode),
          )}
        />
      </label>

      <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-5">
        <Btn
          tone="primary"
          onClick={wrap("start", startFactory)}
          disabled={!canStart || !!busy}
        >
          {busy === "start" ? "시작 중..." : "시작"}
        </Btn>
        <Btn
          tone="warn"
          onClick={wrap("pause", pauseFactory)}
          disabled={!canPause || !!busy}
        >
          {busy === "pause" ? "일시정지 중..." : "일시정지"}
        </Btn>
        <Btn
          tone="primary"
          onClick={wrap("resume", resumeFactory)}
          disabled={!canResume || !!busy}
        >
          {busy === "resume" ? "재개 중..." : "재개"}
        </Btn>
        <Btn
          tone="danger"
          onClick={wrap("stop", stopFactory)}
          disabled={!canStop || !!busy}
        >
          {busy === "stop" ? "중지 중..." : "중지"}
        </Btn>
        <Btn
          tone="ghost"
          onClick={wrap("reset", resetFactory)}
          disabled={!canReset || !!busy}
        >
          초기화
        </Btn>
      </div>
    </section>
  );
}
