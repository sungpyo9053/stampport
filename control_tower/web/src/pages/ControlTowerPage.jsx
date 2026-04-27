import { useCallback, useEffect, useRef, useState } from "react";
import HeaderStatusBar from "../components/HeaderStatusBar.jsx";
import AgentOffice from "../components/AgentOffice.jsx";
import EventFeed from "../components/EventFeed.jsx";
import TaskBoard from "../components/TaskBoard.jsx";
import ArtifactPanel from "../components/ArtifactPanel.jsx";
import FactoryControlPanel from "../components/FactoryControlPanel.jsx";
import PipelineTimeline from "../components/PipelineTimeline.jsx";
import RunnerPanel from "../components/RunnerPanel.jsx";
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

const POLL_MS = 1500;
const FAST_POLL_MS = 800;        // when factory is actively running
const BUBBLE_TTL_MS = 3200;

export default function ControlTowerPage() {
  const [agents, setAgents] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [events, setEvents] = useState([]);
  const [factory, setFactory] = useState(null);
  const [factoryEvents, setFactoryEvents] = useState([]);
  const [runners, setRunners] = useState([]);
  const [bubbles, setBubbles] = useState({});
  const [handoffs, setHandoffs] = useState([]);
  const [isRunningDemo, setIsRunningDemo] = useState(false);
  const [apiError, setApiError] = useState(null);
  const [showOffice, setShowOffice] = useState(false); // mobile default: collapsed

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
    } else if (
      ev.type === "handoff" &&
      ev.payload?.from_agent &&
      ev.payload?.to_agent
    ) {
      setHandoffs((h) => [
        ...h,
        { id: ev.id, from: ev.payload.from_agent, to: ev.payload.to_agent },
      ]);
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

  // Adaptive polling: fast while running, slow otherwise.
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
      setTasks([]);
      setEvents([]);
      setBubbles({});
      setHandoffs([]);
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

  const onHandoffDone = (id) => {
    setHandoffs((h) => h.filter((x) => x.id !== id));
  };

  const agentStatuses = Object.fromEntries(
    agents.map((a) => [a.id, a.status]),
  );
  const activeAgentId = agents.find((a) => a.status === "working")?.id || null;

  return (
    <div className="flex min-h-screen flex-col">
      <HeaderStatusBar
        agents={agents}
        tasks={tasks}
        events={events}
        onRunDemo={handleRunDemo}
        isRunningDemo={isRunningDemo}
      />

      {apiError && (
        <div className="border-b border-rose-500/40 bg-rose-500/10 px-4 py-1.5 text-[12px] text-rose-200">
          ⚠ 컨트롤타워 API 연결 오류: {apiError}.
        </div>
      )}

      {/* Mobile-first stacked layout. lg+ keeps the office visible. */}
      <main className="flex flex-1 flex-col gap-3 p-3">
        {/* Factory control + pipeline + runner — primary on iPhone */}
        <div className="grid gap-3 lg:grid-cols-3">
          <div className="lg:col-span-2 space-y-3">
            <FactoryControlPanel factory={factory} onChanged={tick} />
            <PipelineTimeline
              factory={factory}
              agentStatuses={agentStatuses}
              factoryEvents={factoryEvents}
            />
          </div>
          <div className="space-y-3">
            <RunnerPanel runners={runners} onChanged={tick} />
          </div>
        </div>

        {/* Office simulation. Always visible on desktop; collapsible on mobile. */}
        <section className="rounded-2xl border border-slate-800 bg-slate-950/30">
          <button
            type="button"
            onClick={() => setShowOffice((s) => !s)}
            className="flex w-full items-center justify-between px-4 py-2 text-[12.5px] tracking-wide text-slate-300 lg:hidden"
          >
            <span>오피스 시뮬레이션</span>
            <span className="text-slate-500">{showOffice ? "접기 ▲" : "펼치기 ▼"}</span>
          </button>
          <div
            className={`${
              showOffice ? "block" : "hidden"
            } min-h-[650px] p-3 lg:block`}
          >
            <AgentOffice
              agentStatuses={agentStatuses}
              bubbles={bubbles}
              handoffs={handoffs}
              onHandoffDone={onHandoffDone}
              activeAgentId={activeAgentId}
            />
          </div>
        </section>

        {/* Secondary panels */}
        <div className="grid gap-3 md:grid-cols-2">
          <TaskBoard tasks={tasks} />
          <ArtifactPanel events={events} />
        </div>

        {/* Event feed — bottom on mobile, but full width regardless */}
        <div className="h-[260px] sm:h-[300px]">
          <EventFeed events={events} />
        </div>
      </main>
    </div>
  );
}
