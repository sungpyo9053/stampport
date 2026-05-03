import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import PixelOffice from "../components/PixelOffice.jsx";
import PingPongBoard from "../components/PingPongBoard.jsx";
import ArtifactBoard from "../components/ArtifactBoard.jsx";
import ControlDock from "../components/ControlDock.jsx";
import PipelineTimeline from "../components/PipelineTimeline.jsx";
import OperatorCommandPanel from "../components/OperatorCommandPanel.jsx";
import SystemLogPanel from "../components/SystemLogPanel.jsx";
import CycleEffectivenessPanel from "../components/CycleEffectivenessPanel.jsx";
import WatchdogPanel from "../components/WatchdogPanel.jsx";
import PipelineRecoveryPanel from "../components/PipelineRecoveryPanel.jsx";
import AgentAccountabilityPanel from "../components/AgentAccountabilityPanel.jsx";
import AutoPilotPanel from "../components/AutoPilotPanel.jsx";
import AutoPilotHero from "../components/AutoPilotHero.jsx";
import AgentOfficeScene from "../components/AgentOfficeScene.jsx";
import AgentDetailDrawer from "../components/AgentDetailDrawer.jsx";
import OverallStatusBar from "../components/OverallStatusBar.jsx";
import {
  fetchAgents,
  fetchEvents,
  fetchFactoryEvents,
  fetchFactoryStatus,
  fetchRunners,
  fetchTasks,
  resetDemo,
  runDemo,
} from "../api/controlTowerApi.js";
import {
  makeHandoffEvent,
  synthesizeCycleEvents,
  synthesizeDeployEvents,
  synthesizeOperatorRequestEvents,
  synthesizeWatchdogEvents,
} from "../utils/cycleEventSynth.js";

const POLL_MS = 1500;
const FAST_POLL_MS = 800;
const BUBBLE_TTL_MS = 4500;
// Cap how many in-page handoff events we keep on screen — they're
// transient notifications, not durable state.
const HANDOFF_LOG_MAX = 40;

// Pull autopilot status off the runner heartbeat so we know whether to
// hide stale demo / sample artifacts behind the "Legacy Diagnostic"
// accordion. When autopilot is RUNNING the operator wants the live
// scene front-and-center — not a Cycle Board sample from last week.
function pickAutopilotStatus(runners = []) {
  for (const r of runners) {
    const ap = r?.metadata_json?.local_factory?.autopilot;
    if (ap?.status) return ap.status;
  }
  return null;
}

// Stampport Control Tower — Auto Pilot agent-office layout.
//
// Layout from top to bottom:
//   1. Header (logo + demo button)
//   2. AutoPilot Hero — story-card status banner
//   3. AgentOfficeScene — 8 agents in a Reels-style scene
//   4. SystemLog + OperatorCommandPanel — live signal panel
//   5. AutoPilotPanel — start/stop knobs
//   6. AgentAccountabilityPanel — per-agent verdict (current cycle)
//   7. Legacy Diagnostic accordion — pixel office, ping-pong board,
//      pipeline recovery, watchdog, cycle effectiveness, artifacts.
//      Collapsed by default while Auto Pilot is RUNNING; expanded when
//      the operator is idle / debugging a specific cycle.
export default function ControlTowerPage() {
  const [agents, setAgents] = useState([]);
  const [, setTasks] = useState([]);
  const [events, setEvents] = useState([]);
  const [factory, setFactory] = useState(null);
  const [factoryEvents, setFactoryEvents] = useState([]);
  const [runners, setRunners] = useState([]);
  const [bubbles, setBubbles] = useState({});
  const [handoffLog, setHandoffLog] = useState([]);
  const [isRunningDemo, setIsRunningDemo] = useState(false);
  const [apiError, setApiError] = useState(null);
  const [selectedAgentId, setSelectedAgentId] = useState(null);
  const [legacyOpen, setLegacyOpen] = useState(false);

  // ?handoffDemo=1 (or ?demo=handoff) forces the courier demo loop on
  // top of whatever the runner is reporting, so an operator can verify
  // the animation regardless of factory state. Computed once per
  // mount — toggling the flag requires a navigation, which is fine for
  // a debug-only switch.
  const forceDemoHandoff = useMemo(() => {
    if (typeof window === "undefined") return false;
    try {
      const params = new URLSearchParams(window.location.search);
      const v = params.get("handoffDemo") || params.get("demo");
      if (!v) return false;
      const lowered = v.toLowerCase();
      return lowered === "1" || lowered === "true" || lowered === "handoff";
    } catch {
      return false;
    }
  }, []);

  // AgentRouteLayer hands us {kind, from, to, label, banner, source}
  // each time a non-demo handoff card starts/ends. Convert to an
  // event-shaped row and prepend so SystemLog renders newest first.
  const handleHandoff = useCallback((info) => {
    if (!info || !info.kind) return;
    setHandoffLog((prev) =>
      [makeHandoffEvent(info), ...prev].slice(0, HANDOFF_LOG_MAX),
    );
  }, []);

  const lastEventIdRef = useRef(0);
  const initializedRef = useRef(false);
  const bubbleTimers = useRef({});

  const processNewEvent = useCallback((ev) => {
    if (ev.type === "agent_message" && ev.agent_id) {
      setBubbles((b) => ({
        ...b,
        [ev.agent_id]: { id: ev.id, message: ev.message },
      }));
      if (bubbleTimers.current[ev.agent_id]) {
        clearTimeout(bubbleTimers.current[ev.agent_id]);
      }
      const agentId = ev.agent_id;
      const evId = ev.id;
      bubbleTimers.current[agentId] = setTimeout(() => {
        setBubbles((b) => {
          if (!b[agentId] || b[agentId].id !== evId) return b;
          const next = { ...b };
          delete next[agentId];
          return next;
        });
      }, BUBBLE_TTL_MS);
    }
  }, []);

  const tick = useCallback(async () => {
    try {
      const [a, t, e, f, fe, r] = await Promise.all([
        fetchAgents(),
        fetchTasks(),
        fetchEvents(),
        fetchFactoryStatus().catch(() => null),
        fetchFactoryEvents().catch(() => []),
        fetchRunners().catch(() => []),
      ]);
      setAgents(a);
      setTasks(t);
      setEvents(e);
      setFactory(f);
      setFactoryEvents(fe);
      setRunners(r);
      setApiError(null);

      if (!initializedRef.current) {
        initializedRef.current = true;
        const maxId = e.length > 0 ? Math.max(...e.map((x) => x.id)) : 0;
        lastEventIdRef.current = maxId;
        return;
      }
      const newOnes = e.filter((ev) => ev.id > lastEventIdRef.current);
      if (newOnes.length === 0) return;
      for (const ev of newOnes) processNewEvent(ev);
      lastEventIdRef.current = Math.max(
        lastEventIdRef.current,
        ...newOnes.map((x) => x.id),
      );
    } catch (err) {
      setApiError(err.message || "API unreachable");
    }
  }, [processNewEvent]);

  useEffect(() => {
    tick();
    const interval = factory?.status === "running" ? FAST_POLL_MS : POLL_MS;
    const id = setInterval(tick, interval);
    return () => clearInterval(id);
  }, [tick, factory?.status]);

  useEffect(() => {
    return () => {
      Object.values(bubbleTimers.current).forEach((t) => clearTimeout(t));
    };
  }, []);

  const handleRunDemo = async () => {
    if (isRunningDemo) return;
    setIsRunningDemo(true);
    try {
      await resetDemo();
      setEvents([]);
      setBubbles({});
      lastEventIdRef.current = 0;
      Object.values(bubbleTimers.current).forEach(clearTimeout);
      bubbleTimers.current = {};
      setAgents((prev) =>
        prev.map((a) => ({ ...a, status: "idle", current_task_id: null })),
      );
      await runDemo();
    } catch (err) {
      setApiError(err.message || "failed to start demo");
    } finally {
      setTimeout(() => setIsRunningDemo(false), 1200);
    }
  };

  const agentStatuses = Object.fromEntries(
    agents.map((a) => [a.id, a.status]),
  );
  const activeAgentId = agents.find((a) => a.status === "working")?.id || null;

  const autopilotStatus = useMemo(
    () => pickAutopilotStatus(runners),
    [runners],
  );
  const autopilotRunning = autopilotStatus === "running";

  const combinedEvents = useMemo(
    () => [
      ...events,
      ...synthesizeCycleEvents(runners),
      ...synthesizeDeployEvents(runners),
      ...synthesizeWatchdogEvents(runners),
      ...synthesizeOperatorRequestEvents(runners),
      ...handoffLog,
    ],
    [events, runners, handoffLog],
  );

  // Esc closes the agent detail drawer.
  useEffect(() => {
    if (!selectedAgentId) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") setSelectedAgentId(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedAgentId]);

  return (
    <div
      className="control-tower-page flex min-h-screen flex-col gap-3 p-3 sm:p-4"
      style={{ backgroundColor: "#050912" }}
    >
      {/* Top bar — pixel sign + demo button. */}
      <header
        className="flex flex-wrap items-center justify-between gap-3 px-3 py-2"
        style={{
          backgroundColor: "#0a1228",
          border: "1.5px solid #0e4a3a",
          borderRadius: 4,
          fontFamily: "ui-monospace, monospace",
        }}
      >
        <div className="flex items-center gap-3">
          <div
            className="grid h-9 w-9 place-items-center text-base font-bold"
            style={{
              backgroundColor: "#d4a843",
              color: "#0a1228",
              border: "2px solid #0e4a3a",
              borderRadius: 3,
            }}
          >
            ST
          </div>
          <div className="leading-tight">
            <div className="text-[12.5px] font-bold tracking-[0.25em] text-[#d4a843]">
              STAMPPORT CONTROL TOWER
            </div>
            <div className="text-[10px] tracking-[0.2em] text-slate-400">
              스탬포트 · AI 에이전트 오피스
            </div>
          </div>
        </div>
        <button
          onClick={handleRunDemo}
          disabled={isRunningDemo || autopilotRunning}
          className="px-3 py-1.5 text-[11px] font-bold tracking-[0.2em] transition disabled:opacity-50"
          style={{
            backgroundColor:
              isRunningDemo || autopilotRunning ? "#1a2540" : "#0e4a3a",
            color:
              isRunningDemo || autopilotRunning ? "#475569" : "#f5e9d3",
            border: `1.5px solid ${
              isRunningDemo || autopilotRunning ? "#1a2540" : "#d4a843"
            }`,
            borderRadius: 3,
            cursor:
              isRunningDemo || autopilotRunning ? "not-allowed" : "pointer",
            boxShadow:
              isRunningDemo || autopilotRunning ? "none" : "0 0 12px #d4a84355",
          }}
          title={autopilotRunning
            ? "Auto Pilot 실행 중에는 데모를 실행할 수 없습니다"
            : ""}
        >
          {isRunningDemo ? "데모 실행 중..." : "▶ 데모 실행"}
        </button>
      </header>

      {apiError && (
        <div
          className="px-3 py-1.5 text-[11px] tracking-wider"
          style={{
            backgroundColor: "#3d0a14",
            border: "1px solid #8b2e3c",
            color: "#fecaca",
            borderRadius: 3,
            fontFamily: "ui-monospace, monospace",
          }}
        >
          ⚠ 컨트롤타워 API 연결 오류 · {apiError}
        </div>
      )}

      {/* AUTO PILOT HERO — top-of-page primary status. Always rendered
          first so a control_state-missing message never shows above it. */}
      <AutoPilotHero runners={runners} />

      {/* AGENT OFFICE SCENE — 3 zones (PLAN / BUILD / SHIP) with 8
          agent slots. Drawer is a fixed overlay so we no longer need
          to split the office side-by-side; just let the scene span
          full width. */}
      <AgentOfficeScene
        runners={runners}
        selectedAgentId={selectedAgentId}
        onAgentClick={(id) => setSelectedAgentId(id)}
        drawerOpen={!!selectedAgentId}
      />

      {/* Operations row: SystemLog (left) + OperatorCommandPanel (right) */}
      <section className="grid flex-none gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(380px,400px)] control-tower-right-rail">
        <SystemLogPanel events={combinedEvents} runners={runners} />
        <OperatorCommandPanel runners={runners} onSent={tick} />
      </section>

      {/* AutoPilot knobs + per-agent accountability — current cycle data only. */}
      <section className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(380px,420px)] control-tower-right-rail">
        <AgentAccountabilityPanel runners={runners} />
        <AutoPilotPanel runners={runners} onSent={tick} />
      </section>

      {/* LEGACY DIAGNOSTIC — collapsed by default while Auto Pilot is
          running so stale demo/cycle artifacts don't masquerade as
          current. The accordion lets the operator dig back in for
          debugging without the noise leaking onto the main scene. */}
      <section
        className="legacy-diagnostic flex flex-col gap-2"
        data-testid="legacy-diagnostic"
      >
        <button
          type="button"
          onClick={() => setLegacyOpen((v) => !v)}
          className="flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-[10px] font-bold tracking-[0.3em]"
          style={{
            backgroundColor: "#0a1228",
            border: "1px dashed #1e293b",
            color: "#94a3b8",
            cursor: "pointer",
            fontFamily: "ui-monospace, monospace",
          }}
        >
          <span className="flex items-center gap-2">
            <span style={{ color: "#d4a843" }}>{legacyOpen ? "▼" : "▶"}</span>
            <span>LEGACY DIAGNOSTIC</span>
            <span className="text-slate-600">
              · pixel office · pipeline · watchdog · artifacts
            </span>
          </span>
          {autopilotRunning && !legacyOpen && (
            <span
              className="rounded-full px-2 py-0.5 text-[9px] tracking-widest"
              style={{
                color: "#fbbf24",
                border: "1px solid #fbbf2466",
                backgroundColor: "#0a1228",
              }}
            >
              Auto Pilot 실행 중에는 숨김
            </span>
          )}
        </button>

        {legacyOpen && (
          <>
            <OverallStatusBar runners={runners} />
            <PipelineTimeline
              factory={factory}
              agentStatuses={agentStatuses}
              factoryEvents={factoryEvents}
            />
            <main className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_360px]">
              <div className="relative" style={{ minHeight: 600 }}>
                <PixelOffice
                  agentStatuses={agentStatuses}
                  bubbles={bubbles}
                  activeAgentId={activeAgentId}
                  factory={factory}
                  runners={runners}
                  onHandoff={handleHandoff}
                  forceDemoHandoff={forceDemoHandoff}
                />
              </div>

              <aside className="flex flex-col gap-3">
                <PipelineRecoveryPanel runners={runners} />
                <WatchdogPanel runners={runners} />
                <CycleEffectivenessPanel runners={runners} />
                <PingPongBoard events={events} runners={runners} />
                <ArtifactBoard events={events} factory={factory} />
              </aside>
            </main>

            <ControlDock factory={factory} runners={runners} onChanged={tick} />
          </>
        )}
      </section>

      <AgentDetailDrawer
        agentId={selectedAgentId}
        runners={runners}
        events={combinedEvents}
        onClose={() => setSelectedAgentId(null)}
      />
    </div>
  );
}
