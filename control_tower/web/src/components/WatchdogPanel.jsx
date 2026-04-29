// Factory Watchdog status panel.
//
// Reads from runners[].metadata_json.local_factory.watchdog (written by
// runner.py:_build_watchdog_meta). Display-only — toggling on/off
// happens via the FACTORY_WATCHDOG_ENABLED environment variable on the
// runner, not from the UI. We intentionally keep the panel small so it
// can sit alongside CycleEffectivenessPanel without crowding the
// pixel-office stage.

const STATUS_COLOR = {
  disabled:  "#64748b",
  watching:  "#38bdf8",
  healthy:   "#34d399",
  repairing: "#fbbf24",
  degraded:  "#fb923c",
  broken:    "#f87171",
};

const SEVERITY_COLOR = {
  info:    "#94a3b8",
  warning: "#fbbf24",
  error:   "#f87171",
};

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString([], { hour12: false });
  } catch {
    return String(iso).slice(0, 19);
  }
}

function pickWatchdog(runners = []) {
  for (const r of runners) {
    const wd = r?.metadata_json?.local_factory?.watchdog;
    if (wd) return wd;
  }
  return null;
}

export default function WatchdogPanel({ runners = [] }) {
  const wd = pickWatchdog(runners);

  if (!wd) {
    return (
      <section
        className="flex flex-col gap-2 p-3"
        data-testid="watchdog-panel"
        style={{
          backgroundColor: "#0e1a35",
          border: "1.5px solid #0e4a3a",
          borderRadius: 6,
          fontFamily: "ui-monospace, monospace",
        }}
      >
        <div className="flex items-center gap-2">
          <span className="inline-block h-2 w-2" style={{ backgroundColor: "#64748b" }} />
          <span className="text-[10px] font-bold uppercase tracking-[0.3em] text-emerald-300">
            FACTORY WATCHDOG
          </span>
        </div>
        <div className="text-[11px] text-slate-500">
          runner heartbeat 대기 중 — runner 가 연결되면 watchdog 상태가 표시됩니다.
        </div>
      </section>
    );
  }

  const enabled = !!wd.enabled;
  const status = wd.status || (enabled ? "watching" : "disabled");
  const statusColor = STATUS_COLOR[status] || "#94a3b8";
  const severity = wd.severity || "info";
  const severityColor = SEVERITY_COLOR[severity] || "#94a3b8";
  const evidence = Array.isArray(wd.evidence) ? wd.evidence : [];
  const safeActions = Array.isArray(wd.safe_actions_taken) ? wd.safe_actions_taken : [];
  const suggested = Array.isArray(wd.suggested_actions) ? wd.suggested_actions : [];

  return (
    <section
      className="flex flex-col gap-2 p-3"
      data-testid="watchdog-panel"
      style={{
        backgroundColor: "#0e1a35",
        border: "1.5px solid #0e4a3a",
        borderRadius: 6,
        fontFamily: "ui-monospace, monospace",
      }}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span
            className="inline-block h-2 w-2"
            style={{ backgroundColor: statusColor }}
          />
          <span className="text-[10px] font-bold uppercase tracking-[0.3em] text-emerald-300">
            FACTORY WATCHDOG
          </span>
          <span
            className="rounded px-1.5 py-0.5 text-[9px] font-bold tracking-widest"
            style={{
              color: statusColor,
              border: `1px solid ${statusColor}66`,
              backgroundColor: "#0a1228",
            }}
          >
            {status.toUpperCase()}
          </span>
          <span
            className="rounded px-1.5 py-0.5 text-[9px] font-bold tracking-widest"
            style={{
              color: enabled ? "#34d399" : "#64748b",
              border: `1px solid ${enabled ? "#34d39966" : "#64748b66"}`,
              backgroundColor: "#0a1228",
            }}
          >
            {enabled ? "ENABLED" : "DISABLED"}
          </span>
        </div>
        <span className="text-[10px] tracking-widest text-slate-500">
          {fmtTime(wd.last_checked_at)}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px]">
        <div className="text-slate-400">
          <span className="text-slate-500">code:</span>{" "}
          <span style={{ color: severityColor }}>
            {wd.last_diagnostic_code || "—"}
          </span>
        </div>
        <div className="text-slate-400">
          <span className="text-slate-500">repeat:</span>{" "}
          <span className="text-slate-200">
            {wd.repeat_count || 0} / {wd.max_repeat || 3}
          </span>
        </div>
        <div className="text-slate-400">
          <span className="text-slate-500">interval:</span>{" "}
          <span className="text-slate-200">{wd.interval_sec || "—"}s</span>
        </div>
        <div className="text-slate-400">
          <span className="text-slate-500">last repair:</span>{" "}
          <span className="text-slate-200">{fmtTime(wd.last_repair_at)}</span>
        </div>
      </div>

      {wd.root_cause && (
        <div
          className="rounded px-2 py-1.5 text-[11px] leading-snug"
          style={{
            backgroundColor: "#0a1228",
            border: `1px solid ${severityColor}33`,
            color: severityColor,
          }}
        >
          {wd.root_cause}
        </div>
      )}

      {wd.auto_repair_blocked_reason && (
        <div className="text-[10px] text-amber-300">
          auto repair blocked · {wd.auto_repair_blocked_reason}
        </div>
      )}

      {evidence.length > 0 && (
        <details className="text-[10px] text-slate-500">
          <summary className="cursor-pointer hover:text-slate-300">
            evidence ({evidence.length})
          </summary>
          <ul className="mt-1 space-y-0.5">
            {evidence.slice(0, 8).map((line, i) => (
              <li key={i} className="text-slate-400">
                · {line}
              </li>
            ))}
          </ul>
        </details>
      )}

      {safeActions.length > 0 && (
        <details className="text-[10px] text-slate-500" open>
          <summary className="cursor-pointer text-slate-400 hover:text-slate-200">
            safe actions taken ({safeActions.length})
          </summary>
          <ul className="mt-1 space-y-0.5">
            {safeActions.slice(0, 8).map((line, i) => (
              <li key={i} className="text-emerald-300">
                ✓ {line}
              </li>
            ))}
          </ul>
        </details>
      )}

      {suggested.length > 0 && (
        <details className="text-[10px] text-slate-500" open>
          <summary className="cursor-pointer text-slate-400 hover:text-slate-200">
            운영자 확인 필요
          </summary>
          <ul className="mt-1 space-y-0.5">
            {suggested.slice(0, 4).map((line, i) => (
              <li key={i} className="text-amber-200">
                · {line}
              </li>
            ))}
          </ul>
        </details>
      )}

      {!enabled && (
        <div className="text-[10px] text-slate-500">
          활성화하려면 runner 환경에서{" "}
          <span className="text-emerald-300">FACTORY_WATCHDOG_ENABLED=true</span>{" "}
          설정 후 재시작하세요.
        </div>
      )}
    </section>
  );
}
