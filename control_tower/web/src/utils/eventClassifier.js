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


// ---------------------------------------------------------------------------
// System log classifier
//
// SystemLogPanel asks: "for THIS event, which operator-facing
// category is it?" The panel splits the firehose into All / Runner /
// Command / Claude / Build / QA / Git / Deploy / Error chips so the
// operator can drill into "what's happening on the runner side".
//
// Agent-activity events (planner/designer chatter, artifact_created
// for ping-pong) are deliberately NOT system events — those live
// in the PingPongBoard / EventFeed already.
//
// classifySystemEvent returns:
//   {
//     category:  "Runner" | "Command" | "Claude" | "Build" | "QA"
//              | "Git"    | "Deploy"  | "Error",
//     severity:  "info" | "success" | "warn" | "error",
//     actor:     "runner" | "claude" | "factory" | "github" | "system",
//     phase:     "started" | "completed" | "failed" | "queued" | "info",
//   }
// or null when the event is purely agent activity.
// ---------------------------------------------------------------------------

export const SYSTEM_LOG_CATEGORIES = [
  "All",
  "Runner",
  "Command",
  "Claude",
  "Build",
  "QA",
  "Git",
  "Deploy",
  "Error",
];

// Hard-coded mapping for known event-bus types. Anything outside this
// table falls through to keyword matching below. We list them
// explicitly because the category isn't always inferable from the
// message text (Korean translations vary by stage).
const SYSTEM_TYPE_TABLE = {
  // Local runner heartbeat / queue.
  local_runner_heartbeat:       { category: "Runner",  actor: "runner",  severity: "info",    phase: "info"      },
  local_runner_command_created: { category: "Command", actor: "runner",  severity: "info",    phase: "queued"    },
  local_runner_command_claimed: { category: "Command", actor: "runner",  severity: "info",    phase: "started"   },
  local_runner_result_reported: { category: "Command", actor: "runner",  severity: "success", phase: "completed" },
  local_runner_stale:           { category: "Runner",  actor: "runner",  severity: "warn",    phase: "info"      },

  // Factory lifecycle (from cycle.py via the API event bus when
  // mirrored). These give the operator a "is the loop running"
  // pulse — bucket them as Runner so they stay visible in the
  // default view.
  factory_started:           { category: "Runner", actor: "factory", severity: "info",    phase: "started"   },
  factory_paused:            { category: "Runner", actor: "factory", severity: "warn",    phase: "info"      },
  factory_resumed:           { category: "Runner", actor: "factory", severity: "info",    phase: "started"   },
  factory_stopping:          { category: "Runner", actor: "factory", severity: "warn",    phase: "info"      },
  factory_stopped:           { category: "Runner", actor: "factory", severity: "warn",    phase: "completed" },
  factory_completed:         { category: "Runner", actor: "factory", severity: "success", phase: "completed" },
  factory_failed:            { category: "Error",  actor: "factory", severity: "error",   phase: "failed"    },
  factory_reset:             { category: "Runner", actor: "factory", severity: "info",    phase: "info"      },
  factory_desired_changed:   { category: "Runner", actor: "factory", severity: "info",    phase: "info"      },
  factory_continuous_toggled:{ category: "Runner", actor: "factory", severity: "info",    phase: "info"      },
  factory_auto_restarted:    { category: "Runner", actor: "factory", severity: "warn",    phase: "started"   },

  // Deploy agent (server-side simulation + GitHub Actions handoff).
  deploy_started:             { category: "Deploy", actor: "github", severity: "info",    phase: "started"   },
  deploy_build_checked:       { category: "Deploy", actor: "github", severity: "info",    phase: "info"      },
  deploy_nginx_checked:       { category: "Deploy", actor: "github", severity: "info",    phase: "info"      },
  deploy_service_restarted:   { category: "Deploy", actor: "github", severity: "info",    phase: "info"      },
  deploy_healthcheck_passed:  { category: "Deploy", actor: "github", severity: "success", phase: "completed" },
  deploy_completed:           { category: "Deploy", actor: "github", severity: "success", phase: "completed" },
  deploy_failed:              { category: "Error",  actor: "github", severity: "error",   phase: "failed"    },

  // Generic error event_bus fanout.
  error: { category: "Error", actor: "system", severity: "error", phase: "failed" },
};

// Keyword table used as a fallback when the event type is generic
// (handoff / agent_message / artifact_created without a known
// payload). Order matters — more specific phrases first.
const KEYWORD_TABLE = [
  // Claude lifecycle.
  [/claude\s*(code|cli)?\s*started/i,                     { category: "Claude", actor: "claude", severity: "info",    phase: "started"   }],
  [/claude\s*(code|cli)?\s*(completed|finished|passed)/i, { category: "Claude", actor: "claude", severity: "success", phase: "completed" }],
  [/claude\s*(code|cli)?\s*(failed|error|timeout)/i,      { category: "Error",  actor: "claude", severity: "error",   phase: "failed"    }],
  [/(operator[_ ]?request|claude에게 작업 지시)/i,        { category: "Claude", actor: "claude", severity: "info",    phase: "info"      }],

  // Build.
  [/build\s+(started|진행|시작)/i,                        { category: "Build", actor: "factory", severity: "info",    phase: "started"   }],
  [/build\s+(passed|completed|통과|완료|success)/i,       { category: "Build", actor: "factory", severity: "success", phase: "completed" }],
  [/build\s+(failed|error|실패)/i,                        { category: "Error", actor: "factory", severity: "error",   phase: "failed"    }],
  [/(npm\s+run\s+build|vite\s+build)/i,                   { category: "Build", actor: "factory", severity: "info",    phase: "info"      }],

  // QA.
  [/qa\s+(gate|pipeline)?\s*(started|진행|시작)/i,        { category: "QA", actor: "factory", severity: "info",    phase: "started"   }],
  [/qa\s+(gate|pipeline)?\s*(passed|completed|통과|완료)/i,{ category: "QA", actor: "factory", severity: "success", phase: "completed" }],
  [/qa\s+(gate|pipeline)?\s*(failed|error|실패)/i,        { category: "Error", actor: "factory", severity: "error", phase: "failed"    }],

  // Git.
  [/(git\s+commit|commit\s+created)/i,                    { category: "Git", actor: "factory", severity: "success", phase: "completed" }],
  [/(git\s+push\s+started|pushing\s+to\s+main)/i,         { category: "Git", actor: "factory", severity: "info",    phase: "started"   }],
  [/(git\s+push\s+(completed|done|success))/i,            { category: "Git", actor: "factory", severity: "success", phase: "completed" }],
  [/(git\s+push\s+failed|conflict\s+marker)/i,            { category: "Error", actor: "factory", severity: "error", phase: "failed"    }],

  // Deploy / GitHub Actions.
  [/(github\s+actions|workflow_dispatch|deploy\s+triggered)/i, { category: "Deploy", actor: "github", severity: "info",    phase: "started"   }],
  [/(deploy.*completed|배포.*완료|healthcheck\s+passed)/i,     { category: "Deploy", actor: "github", severity: "success", phase: "completed" }],
  [/(deploy.*failed|배포.*실패)/i,                             { category: "Error",  actor: "github", severity: "error",   phase: "failed"    }],

  // Error / retry / timeout — last-resort buckets.
  [/(timeout|timed\s+out)/i,                              { category: "Error", actor: "system", severity: "error", phase: "failed" }],
  [/(retry|재시도)/i,                                     { category: "Error", actor: "system", severity: "warn",  phase: "info"   }],
  [/(error|exception|실패|rejected)/i,                    { category: "Error", actor: "system", severity: "error", phase: "failed" }],
];

// Agent activity (planner/designer ping-pong, agent message bubbles)
// is *not* a system event. Skip these so the SystemLog stays focused
// on operator-facing concerns.
const AGENT_ACTIVITY_TYPES = new Set([
  "agent_started",
  "agent_message",
  "task_created",
  "task_completed",
  "handoff",
  "approval_requested",
  "approval_granted",
  "approval_rejected",
]);

const PING_PONG_ARTIFACT_TYPES_FOR_SYSTEM = new Set([
  "planner_proposal",
  "designer_critique",
  "planner_revision",
  "designer_final_review",
  "pm_decision",
  "desire_scorecard",
  "product_brief",
  "wireframe",
  "frontend_code",
  "api_spec",
  "agent_design",
  "test_cases",
  // deploy_log IS a system event — handle it explicitly below.
]);

export function classifySystemEvent(ev) {
  if (!ev) return null;

  // 1. Direct event-type lookup.
  const direct = SYSTEM_TYPE_TABLE[ev.type];
  if (direct) return { ...direct };

  // 2. artifact_created — generally agent activity. Promote
  //    deploy_log to a System "Deploy" entry; drop the rest.
  if (ev.type === "artifact_created") {
    const at = ev.payload?.artifact_type;
    if (at === "deploy_log") {
      return {
        category: "Deploy",
        actor: "github",
        severity: "success",
        phase: "completed",
      };
    }
    if (at && PING_PONG_ARTIFACT_TYPES_FOR_SYSTEM.has(at)) return null;
  }

  // 3. Skip pure agent activity unless the message explicitly mentions
  //    a system concern (build/QA/deploy/git/claude).
  const msg = ev.message || "";
  if (AGENT_ACTIVITY_TYPES.has(ev.type)) {
    // Only allow keyword passthrough when the message names a
    // system concern.
    const passthrough = /(build|qa|deploy|git|push|claude|배포|빌드|커밋|푸시|에러|실패|timeout)/i;
    if (!passthrough.test(msg)) return null;
  }

  // 4. Keyword-fallback.
  for (const [pat, hit] of KEYWORD_TABLE) {
    if (pat.test(msg)) return { ...hit };
  }

  return null;
}

export function isSystemEvent(ev) {
  return classifySystemEvent(ev) !== null;
}

// SystemLogPanel feed — newest first, with the classification
// pre-attached so the panel doesn't re-classify on every render.
export function buildSystemLogEntries(events = []) {
  return events
    .map((ev) => {
      const cls = classifySystemEvent(ev);
      if (!cls) return null;
      return { ev, ...cls };
    })
    .filter(Boolean)
    .sort((a, b) => (b.ev.id || 0) - (a.ev.id || 0));
}
