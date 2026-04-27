// Event → ping-pong category classifier.
//
// The Stampport factory loop emits two flavors of events the dashboard
// has to keep distinct:
//
//   1. Demo workflow / orchestrator events fired through the FastAPI
//      event_bus (type=artifact_created with payload.artifact_type).
//   2. Local runner cycle.py events that ride along in heartbeat
//      metadata; when the runner mirrors stage status into agent_message
//      events, the message body carries Korean stage labels rather than
//      a structured payload.
//
// A future SystemLog component (or a filtered view of EventFeed) can
// use this classifier to bucket events into ping-pong vs everything
// else without re-implementing the keyword table at every call site.
//
// Returns one of:
//   { kind: "ping_pong_step",   step: <key> }      — artifact landed
//   { kind: "ping_pong_status", step: <key>, phase } — stage status update
//   { kind: "ping_pong_gate",   phase: "score" | "decision" } — score/ship update
//   null                                            — not ping-pong
//
// `step` keys mirror the heartbeat metadata schema in
// runner.py:_build_pingpong_meta and the artifact_type strings in
// agent_runner.py:run_agent.

const PING_PONG_STEP_KEYS = [
  "planner_proposal",
  "designer_critique",
  "planner_revision",
  "designer_final_review",
  "pm_decision",
];

const PING_PONG_ARTIFACT_TYPES = new Set([
  ...PING_PONG_STEP_KEYS,
  "desire_scorecard",
]);

// Korean message keywords → step key. Order matters: the longer
// strings first so "디자이너 최종 평가" doesn't get swallowed by
// "디자이너 반박".
const STEP_KEYWORDS = [
  ["기획자 원안",        "planner_proposal"],
  ["기획자 수정안",      "planner_revision"],
  ["디자이너 반박",      "designer_critique"],
  ["디자이너 최종 평가", "designer_final_review"],
  ["디자이너 재평가",    "designer_final_review"],
  ["PM 결정",            "pm_decision"],
  ["PM 최종 결정",       "pm_decision"],
  ["출하 결정",          "pm_decision"],
];

const GATE_KEYWORDS = [
  ["욕구 점수표",   "score"],
  ["스코어카드",    "score"],
  ["출하 가능",     "decision"],
  ["재작업 필요",   "decision"],
  ["ship_ready",    "decision"],
];

function _phaseFromMessage(msg) {
  // Map runner-side action verbs onto a small phase enum so a
  // SystemLog can color started / completed / failed differently.
  if (!msg) return "info";
  if (msg.includes("실패") || msg.includes("Failed") || msg.includes("rejected")) {
    return "failed";
  }
  if (msg.includes("스킵") || msg.includes("skipped")) return "skipped";
  if (
    msg.includes("완료") ||
    msg.includes("생성") ||
    msg.includes("passed") ||
    msg.includes("통과")
  ) return "completed";
  if (msg.includes("시작") || msg.includes("started") || msg.includes("진행 중")) {
    return "started";
  }
  return "info";
}

export function classifyEvent(ev) {
  if (!ev) return null;
  // 1. Structured artifact_created events ride payload.artifact_type.
  const artifactType = ev.payload?.artifact_type;
  if (artifactType && PING_PONG_ARTIFACT_TYPES.has(artifactType)) {
    if (artifactType === "desire_scorecard") {
      return { kind: "ping_pong_gate", phase: "score" };
    }
    return { kind: "ping_pong_step", step: artifactType };
  }

  // 2. Free-text agent_message / log events. Keyword-match the body.
  const msg = ev.message || "";
  for (const [kw, step] of STEP_KEYWORDS) {
    if (msg.includes(kw)) {
      return {
        kind: "ping_pong_status",
        step,
        phase: _phaseFromMessage(msg),
      };
    }
  }
  for (const [kw, phase] of GATE_KEYWORDS) {
    if (msg.includes(kw)) {
      return { kind: "ping_pong_gate", phase };
    }
  }
  return null;
}

export function isPingPongEvent(ev) {
  return classifyEvent(ev) !== null;
}

// Convenience for SystemLog filtering — given a list of events,
// return only those that classify as ping-pong activity, sorted by
// id descending so the newest is at the top.
export function filterPingPongEvents(events = []) {
  return events
    .filter(isPingPongEvent)
    .sort((a, b) => (b.id || 0) - (a.id || 0));
}

export const PING_PONG_STEP_ORDER = PING_PONG_STEP_KEYS;
