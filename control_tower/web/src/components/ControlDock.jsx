import { useState } from "react";
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
  const status = factory?.status || "idle";
  const meta = STATUS_LABEL[status] || STATUS_LABEL.idle;

  const onlineRunner = runners.find((r) => r.status === "online");
  const runnerId = onlineRunner?.id;

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
            label="배포"
            tone="success"
            disabled={!runnerId || !!busy}
            busy={busy === "deploy"}
            title={noRunnerNote || "배포하기"}
            onClick={wrap("deploy", () =>
              sendRunnerCommand(runnerId, "publish_changes"),
            )}
          />
        </div>
      </div>

      {factory?.last_message && (
        <div className="px-1 text-[10.5px] tracking-wider text-slate-400">
          ▸ {factory.last_message}
        </div>
      )}
    </section>
  );
}
