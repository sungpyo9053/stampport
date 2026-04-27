import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import PixelOffice from "../components/PixelOffice.jsx";
import PingPongBoard from "../components/PingPongBoard.jsx";
import ArtifactBoard from "../components/ArtifactBoard.jsx";
import ControlDock from "../components/ControlDock.jsx";
import PipelineTimeline from "../components/PipelineTimeline.jsx";
import OperatorCommandPanel from "../components/OperatorCommandPanel.jsx";
import SystemLogPanel from "../components/SystemLogPanel.jsx";
import CycleEffectivenessPanel from "../components/CycleEffectivenessPanel.jsx";
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
} from "../utils/cycleEventSynth.js";

const POLL_MS = 1500;
const FAST_POLL_MS = 800;
const BUBBLE_TTL_MS = 4500;
// Cap how many in-page handoff events we keep on screen — they're
// transient notifications, not durable state.
const HANDOFF_LOG_MAX = 40;

// Stampport Control Tower — pixel-office layout.
//
// The office is the main stage. Pipeline is a thin chip up top,
// ping-pong and cycle artifacts live as side panels, and a game-style
// dock floats at the bottom. There is no factory/task/event table on
// this page — those live inside the office (speech bubbles + artifact
// board).
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

  return (
    <div
      className="flex min-h-screen flex-col gap-3 p-3 sm:p-4"
      style={{ backgroundColor: "#050912" }}
    >
      {/* Top bar — pixel sign + demo button. Replaces HeaderStatusBar. */}
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
          disabled={isRunningDemo}
          className="px-3 py-1.5 text-[11px] font-bold tracking-[0.2em] transition disabled:opacity-50"
          style={{
            backgroundColor: isRunningDemo ? "#1a2540" : "#0e4a3a",
            color: isRunningDemo ? "#475569" : "#f5e9d3",
            border: `1.5px solid ${isRunningDemo ? "#1a2540" : "#d4a843"}`,
            borderRadius: 3,
            cursor: isRunningDemo ? "not-allowed" : "pointer",
            boxShadow: isRunningDemo ? "none" : "0 0 12px #d4a84355",
          }}
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

      {/* Pipeline chip — secondary status only */}
      <PipelineTimeline
        factory={factory}
        agentStatuses={agentStatuses}
        factoryEvents={factoryEvents}
      />

      {/* Operations row: OperatorCommandPanel on the right, SystemLog
          on the left taking the wide column. On mobile both stack
          (Operator first so a phone tap on a small screen lands on
          the input field). */}
      <section className="grid flex-none gap-3 lg:grid-cols-[minmax(0,1fr)_360px]">
        <SystemLogPanel
          events={[
            ...events,
            ...synthesizeCycleEvents(runners),
            ...synthesizeDeployEvents(runners),
            ...handoffLog,
          ]}
        />
        <OperatorCommandPanel runners={runners} onSent={tick} />
      </section>

      {/* Main: pixel office on the left, ping-pong + artifact stack on the right.
          On mobile/tablet the side rail collapses below. */}
      <main className="grid flex-1 gap-3 lg:grid-cols-[minmax(0,1fr)_360px]">
        <div
          className="relative"
          style={{ minHeight: 600 }}
        >
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
          <CycleEffectivenessPanel runners={runners} />
          <PingPongBoard events={events} runners={runners} />
          <ArtifactBoard events={events} factory={factory} />
        </aside>
      </main>

      {/* Dock — bottom, full-width, game-style */}
      <ControlDock factory={factory} runners={runners} onChanged={tick} />
    </div>
  );
}
