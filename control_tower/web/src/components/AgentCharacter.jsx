// Backwards-compatible thin wrapper around the new SD human character.
// The office now renders <Workstation /> directly, but external code that
// still imports AgentCharacter will get the same little person in a card.
import HumanAgentCharacter from "./HumanAgentCharacter.jsx";
import { STATUS_META } from "../constants/agents.js";

export default function AgentCharacter({ agent, status, isActive }) {
  const meta = STATUS_META[status] || STATUS_META.idle;
  return (
    <div
      className="absolute"
      style={{
        left: agent.x,
        top: agent.y,
        transform: "translate(-50%, -50%)",
        width: 80,
        height: 130,
      }}
    >
      <HumanAgentCharacter
        look={agent.look}
        status={status}
        pose="sitting"
        isActive={isActive}
      />
      <div className="mt-1 flex items-center justify-center gap-1.5 rounded-full bg-slate-950/90 px-2.5 py-0.5 text-[11px] uppercase tracking-wider text-slate-100 ring-1 ring-slate-700/70 backdrop-blur">
        <span
          className="h-1.5 w-1.5 rounded-full"
          style={{ backgroundColor: agent.color }}
        />
        <span>{agent.name}</span>
        <span className={`text-[10px] ${meta.text}`}>{meta.label}</span>
      </div>
    </div>
  );
}
