// Stampport Control Tower switched from the StartMate-style "AgentOffice
// diorama" to a full pixel-office stage. This file is kept as a shim
// that forwards to the new PixelOffice so any straggling import keeps
// resolving.
import PixelOffice from "./PixelOffice.jsx";

export default function AgentOffice({ agentStatuses, bubbles, activeAgentId }) {
  return (
    <PixelOffice
      agentStatuses={agentStatuses}
      bubbles={bubbles}
      activeAgentId={activeAgentId}
    />
  );
}
