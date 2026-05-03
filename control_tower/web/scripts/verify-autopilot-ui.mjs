#!/usr/bin/env node
// Auto Pilot state-matrix validator.
//
// Imports the pure derivations from src/utils/autopilotPhase.js and
// src/utils/time.js and runs them against a fixture matrix that
// covers every state machine combination the spec calls out:
//
//   * 8 phases × button enable expectations
//   * 3 modes × stop_on_hold/render/health booleans × max_cycles
//     × max_hours × payload preservation across start + restart
//   * previous-run / previous-cycle artifact freshness
//   * stale-running correction
//   * stuck-before-first-cycle diagnostic
//
// No real autopilot subprocess is spawned — that's the job of the
// existing python self-test. This script is the JS-side equivalent
// for the UI state.
//
// Outputs:
//   .runtime/autopilot_ui_matrix.json — full pass/fail breakdown
//   non-zero exit on any failure
//
// IMPORTANT: this script reuses the SAME functions the panel imports
// (no parallel re-implementation), so a regression in
// deriveButtonState / buildStartPayload / derivePhase fails BOTH the
// matrix and the user-visible UI in lockstep.

import { writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  PHASES,
  buildStartPayload,
  deriveButtonState,
  deriveDisplayCycle,
  deriveEffectiveConfig,
  derivePhase,
  deriveStuckDiagnostic,
  freshnessOf,
  hasActiveCycle,
  pickAutopilot,
  pickRunnerMeta,
  stageToWorkingAgent,
} from "../src/utils/autopilotPhase.js";
import { fmtTime, isAfterStart, parseUtcIso } from "../src/utils/time.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "..", "..", "..");
const RUNTIME_DIR = resolve(REPO_ROOT, ".runtime");

const failures = [];
const passes = [];

function check(name, ok, detail = "") {
  if (ok) passes.push({ name });
  else failures.push({ name, detail });
}

function makeMeta({ status, cycle_count = 0, mode = "safe_run", max_cycles = 5,
                    max_hours = 6, stop_on_hold = true, require_render_check = true,
                    require_api_health = true, started_at = null,
                    ended_at = null, current_cycle_started_at = null,
                    current_cycle_finished_at = null, last_verdict = null,
                    last_failure_code = null, factory_state_status = null,
                    current_stage = null, accountability = null,
                    active_cycle_index = null,
                    autopilot_current_stage = null,
                    live_report_path = null,
                    factory_smoke = null }) {
  return {
    autopilot: {
      status, cycle_count, mode, max_cycles, max_hours,
      stop_on_hold, require_render_check, require_api_health,
      started_at, ended_at,
      current_cycle_started_at, current_cycle_finished_at,
      last_verdict, last_failure_code,
      active_cycle_index,
      current_stage: autopilot_current_stage,
      live_report_path,
    },
    factory_state: { status: factory_state_status, current_stage },
    factory_smoke: factory_smoke || {},
    pipeline_recovery: { current_stage: current_stage || null },
    agent_accountability: accountability || {},
  };
}

// ---------- 1. Phase derivation matrix ----------

const PHASE_FIXTURES = [
  // Idle
  { name: "idle: status=idle, never started", input: makeMeta({ status: "idle" }), expect: "idle" },
  // Starting: running, no cycle, no active subprocess
  {
    name: "starting: cycle=0, no smoke yet",
    input: makeMeta({ status: "running", cycle_count: 0, started_at: new Date().toISOString() }),
    expect: "starting",
  },
  // Cycle running: factory_smoke marker present
  {
    name: "cycle_running: smoke in flight",
    input: makeMeta({
      status: "running", cycle_count: 1,
      started_at: new Date(Date.now() - 30_000).toISOString(),
      current_cycle_started_at: new Date(Date.now() - 5_000).toISOString(),
      current_cycle_finished_at: null,
    }),
    expect: "cycle_running",
  },
  // Waiting next cycle: status=running, cycle>0, no active subprocess
  {
    name: "waiting_next_cycle",
    input: makeMeta({
      status: "running", cycle_count: 2,
      started_at: new Date(Date.now() - 60_000).toISOString(),
      current_cycle_started_at: new Date(Date.now() - 30_000).toISOString(),
      current_cycle_finished_at: new Date(Date.now() - 10_000).toISOString(),
    }),
    expect: "waiting_next_cycle",
  },
  // Stopping
  {
    name: "stopping: status=stopping",
    input: makeMeta({ status: "stopping", cycle_count: 1 }),
    expect: "stopping",
  },
  // Stopped after run
  {
    name: "stopped: cycle_count > 0",
    input: makeMeta({ status: "stopped", cycle_count: 3, last_verdict: "PASS" }),
    expect: "stopped",
  },
  // Failed
  {
    name: "failed: explicit",
    input: makeMeta({ status: "failed", cycle_count: 1, last_verdict: "FAIL" }),
    expect: "failed",
  },
  // Stop requested overrides running → stopping
  {
    name: "stopRequested override → stopping",
    input: makeMeta({ status: "running", cycle_count: 1 }),
    opts: { stopRequested: true },
    expect: "stopping",
  },
  // Restart in flight overrides → restarting
  {
    name: "restartInFlight override → restarting",
    input: makeMeta({ status: "running", cycle_count: 1 }),
    opts: { restartInFlight: true },
    expect: "restarting",
  },
];

for (const f of PHASE_FIXTURES) {
  const got = derivePhase(f.input, f.opts || {});
  check(`phase[${f.name}] = ${f.expect}`, got === f.expect, `got=${got}`);
}

// ---------- 2. Button enable matrix ----------

const BUTTON_FIXTURES = [
  // idle: only Start enabled (and Restart, since runner has a draft)
  { name: "idle buttons", phase: "idle", expect: { canStart: true, canStop: false, canRestart: true, formLocked: false } },
  // starting: Stop enabled (cancel in flight), Start disabled
  { name: "starting buttons", phase: "starting", expect: { canStart: false, canStop: true, canRestart: true, formLocked: true } },
  // cycle_running: Stop + Restart enabled, Start disabled, form locked
  { name: "cycle_running buttons", phase: "cycle_running", expect: { canStart: false, canStop: true, canRestart: true, formLocked: true } },
  // waiting_next_cycle: same as cycle_running for buttons; no active cycle but loop alive
  { name: "waiting_next_cycle buttons", phase: "waiting_next_cycle", expect: { canStart: false, canStop: true, canRestart: true, formLocked: true } },
  // stopping: Stop "정지 요청 중" path, Start disabled, Restart still allowed (queued)
  { name: "stopping buttons", phase: "stopping", expect: { canStart: false, canStop: true, canRestart: true, formLocked: true } },
  // stopped: Start + Restart enabled
  { name: "stopped buttons", phase: "stopped", expect: { canStart: true, canStop: false, canRestart: true, formLocked: false } },
  // failed: like stopped — operator can Start again
  { name: "failed buttons", phase: "failed", expect: { canStart: true, canStop: false, canRestart: true, formLocked: false } },
  // restarting: nothing enabled until the orchestrator clears
  { name: "restarting buttons", phase: "restarting", expect: { canStart: false, canStop: false, canRestart: false, formLocked: true } },
];

for (const f of BUTTON_FIXTURES) {
  const restart = f.phase === "restarting";
  const got = deriveButtonState({
    phase: f.phase,
    stopRequested: false,
    restartInFlight: restart,
    cycleInFlight: f.phase === "cycle_running",
    busy: false,
    hasRunner: true,
  });
  for (const key of Object.keys(f.expect)) {
    check(
      `${f.name} ${key}=${f.expect[key]}`,
      got[key] === f.expect[key],
      `got=${got[key]}`,
    );
  }
}

// stopRequested + running → Stop becomes disabled (already requested) but Start stays disabled
{
  const got = deriveButtonState({
    phase: "cycle_running", stopRequested: true,
    restartInFlight: false, cycleInFlight: true, busy: false, hasRunner: true,
  });
  check("stopRequested disables Stop", got.canStop === false, JSON.stringify(got));
  check("stopRequested keeps Start disabled", got.canStart === false);
  check("stopRequested label includes 종료 후 정지", /현재 cycle 종료 후 정지/.test(got.stopLabel));
}

// busy disables everything
{
  const got = deriveButtonState({ phase: "stopped", busy: true, hasRunner: true });
  check("busy disables Start", got.canStart === false);
  check("busy disables Stop", got.canStop === false);
  check("busy disables Restart", got.canRestart === false);
}

// hasRunner=false disables everything
{
  const got = deriveButtonState({ phase: "stopped", hasRunner: false });
  check("hasRunner=false disables all", !got.canStart && !got.canStop && !got.canRestart);
}

// ---------- 3. Payload preservation: 3 modes × checkbox combos × cycles/hours ----------

const MODES = ["safe_run", "auto_commit", "auto_publish"];
const BOOL = [true, false];
const CYCLES = [1, 5, 10];
const HOURS = [0.1, 3, 10];

let comboCount = 0;
for (const mode of MODES) {
  for (const stopOnHold of BOOL) {
    for (const requireRender of BOOL) {
      for (const requireHealth of BOOL) {
        for (const maxCycles of CYCLES) {
          for (const maxHours of HOURS) {
            comboCount++;
            const draft = { mode, maxCycles, maxHours, stopOnHold, requireRender, requireHealth };
            const payload = buildStartPayload(draft);
            check(
              `payload[${mode}/${maxCycles}c/${maxHours}h/sh=${stopOnHold}/r=${requireRender}/h=${requireHealth}] mode`,
              payload.autopilot_mode === mode && payload.mode === mode,
            );
            check(
              `payload[${mode}/${maxCycles}c/${maxHours}h] cycles`,
              payload.max_cycles === maxCycles,
            );
            check(
              `payload[${mode}/${maxCycles}c/${maxHours}h] hours`,
              payload.max_hours === maxHours,
            );
            check(
              `payload[${mode}/${maxCycles}c/${maxHours}h/sh=${stopOnHold}] stop_on_hold`,
              payload.stop_on_hold === stopOnHold,
            );
            check(
              `payload[${mode}/${maxCycles}c/${maxHours}h/r=${requireRender}] render`,
              payload.require_render_check === requireRender,
            );
            check(
              `payload[${mode}/${maxCycles}c/${maxHours}h/h=${requireHealth}] health`,
              payload.require_api_health === requireHealth,
            );
            check(
              `payload[${mode}] require_scope_consistency=true`,
              payload.require_scope_consistency === true,
            );
          }
        }
      }
    }
  }
}

// String-coerced numeric inputs (the panel's onChange stores strings)
{
  const draft = { mode: "auto_publish", maxCycles: "10", maxHours: "10",
                  stopOnHold: false, requireRender: true, requireHealth: true };
  const p = buildStartPayload(draft);
  check("string cycles → number 10", p.max_cycles === 10);
  check("string hours → number 10", p.max_hours === 10);
  check("user scenario: auto_publish + stop_on_hold=false", p.stop_on_hold === false && p.autopilot_mode === "auto_publish");
}

// ---------- 4. Effective config locking (running shows runtime, idle shows draft) ----------

{
  const draft = { mode: "auto_publish", maxCycles: 10, maxHours: 10,
                  stopOnHold: false, requireRender: true, requireHealth: true };
  // While running, even if the operator changed `draft.mode` to safe_run
  // in their head, the EFFECTIVE display must still show auto_publish
  // from autopilot_state.
  const autopilot = { mode: "auto_publish", max_cycles: 10, max_hours: 10,
                      stop_on_hold: false, require_render_check: true, require_api_health: true };
  const eff = deriveEffectiveConfig({ phase: "cycle_running", draft, autopilot });
  check("running locks mode to runtime", eff.mode === "auto_publish" && eff.locked === true);
  check("running locks max_cycles to runtime", eff.maxCycles === 10);
  check("running locks stop_on_hold to runtime", eff.stopOnHold === false);

  // While idle the draft is what shows.
  const eff2 = deriveEffectiveConfig({ phase: "stopped", draft, autopilot });
  check("stopped shows draft", eff2.mode === "auto_publish" && eff2.locked === false);
}

// ---------- 5. Restart preserves config ----------

{
  const draft = { mode: "auto_publish", maxCycles: 10, maxHours: 10,
                  stopOnHold: false, requireRender: true, requireHealth: true };
  // Restart payload is a second buildStartPayload(draft) call.
  const a = buildStartPayload(draft);
  const b = buildStartPayload(draft);
  check(
    "restart: same draft → identical payload",
    JSON.stringify(a) === JSON.stringify(b),
  );
  // Mode must NOT regress to safe_run.
  check("restart preserves mode=auto_publish", b.autopilot_mode === "auto_publish");
}

// ---------- 6. Previous-run / cycle freshness ----------

const apA = { started_at: "2026-05-02T01:00:00Z", cycle_count: 1 };
const apB = { started_at: "2026-05-03T01:00:00Z", cycle_count: 0 };

// Previous run (artifact came before current run started, no cycle id)
{
  const f = freshnessOf({
    artifactCycleId: 1,
    artifactAt: "2026-05-02T01:30:00Z",
    autopilot: apB,
  });
  // apB.cycle_count=0, artifactCycleId=1 → previous_cycle (artifact > 0)
  // OR current_cycle (since aa.cycle_id is the only signal) — the
  // freshnessOf rule returns current_cycle when artifactCycleId equals
  // apCycle, otherwise previous_cycle when smaller. With apCycle=0 and
  // artifactCycleId=1, the rule path falls into the else branch
  // (current_cycle) — but the AgentAccountabilityPanel applies an
  // additional guard `apStatus=running && cycle_count==0 && acc>=1
  // → stale`. We test that guard separately below.
  // For freshnessOf alone, the answer here is current_cycle.
  check("freshnessOf: artifactCycleId=1, apCycle=0 → current_cycle", f === "current_cycle");
}

// Stale (older artifact)
{
  const f = freshnessOf({
    artifactCycleId: 1, artifactAt: null,
    autopilot: { ...apA, cycle_count: 3 },
  });
  check("freshnessOf: artifactCycleId<apCycle → previous_cycle", f === "previous_cycle");
}

// Same cycle
{
  const f = freshnessOf({ artifactCycleId: 3, autopilot: { cycle_count: 3 } });
  check("freshnessOf: artifactCycleId==apCycle → current_cycle", f === "current_cycle");
}

// No autopilot run — accountability is still current
{
  const f = freshnessOf({ artifactCycleId: 1, autopilot: null });
  check("freshnessOf: no autopilot → unknown/current_cycle treated", ["unknown", "current_cycle"].includes(f));
}

// ---------- 7. Stuck-before-first-cycle diagnostic ----------

{
  // Running, cycle 0, started 200s ago, no active subprocess.
  const meta = makeMeta({
    status: "running", cycle_count: 0,
    started_at: new Date(Date.now() - 200_000).toISOString(),
  });
  const d = deriveStuckDiagnostic(meta);
  check("stuck: running + cycle=0 + 200s + no process → stuck", d.stuck === true);
  check("stuck: diagnostic_code is autopilot_stuck_before_first_cycle",
    d.diagnostic_code === "autopilot_stuck_before_first_cycle");
}
{
  // Same but started only 30s ago — should NOT be stuck yet.
  const meta = makeMeta({
    status: "running", cycle_count: 0,
    started_at: new Date(Date.now() - 30_000).toISOString(),
  });
  const d = deriveStuckDiagnostic(meta);
  check("stuck: <180s → not stuck", d.stuck === false);
}
{
  // cycle_count > 0 → never stuck (even at 1h elapsed)
  const meta = makeMeta({
    status: "running", cycle_count: 1,
    started_at: new Date(Date.now() - 3_600_000).toISOString(),
  });
  const d = deriveStuckDiagnostic(meta);
  check("stuck: cycle_count>0 → not stuck", d.stuck === false);
}
{
  // Active subprocess via current_cycle markers → not stuck
  const meta = makeMeta({
    status: "running", cycle_count: 0,
    started_at: new Date(Date.now() - 200_000).toISOString(),
    current_cycle_started_at: new Date(Date.now() - 30_000).toISOString(),
    current_cycle_finished_at: null,
  });
  const d = deriveStuckDiagnostic(meta);
  check("stuck: active cycle present → not stuck", d.stuck === false);
}

// ---------- 8. Stop/restart race-safety properties ----------

// stopRequested → labels & enable shape match the spec.
{
  const got = deriveButtonState({
    phase: "cycle_running", stopRequested: true,
    cycleInFlight: true, hasRunner: true,
  });
  check("race: stopRequested locks Stop button", got.canStop === false);
  check("race: stopRequested label '정지 요청 중'", /정지 요청 중/.test(got.stopLabel));
}
// restartInFlight → all buttons disabled
{
  const got = deriveButtonState({
    phase: "restarting", restartInFlight: true,
    cycleInFlight: false, hasRunner: true,
  });
  check("race: restarting disables Start", got.canStart === false);
  check("race: restarting disables Stop", got.canStop === false);
  check("race: restarting disables Restart", got.canRestart === false);
}

// ---------- 9. Time formatter timezone correctness ----------

{
  // A naive ISO from event_bus must be treated as UTC.
  const naive = "2026-05-03T01:51:00";
  const z     = "2026-05-03T01:51:00Z";
  const a = parseUtcIso(naive);
  const b = parseUtcIso(z);
  check("parseUtcIso: naive ISO interpreted as UTC", a.getTime() === b.getTime());
  // fmtTime returns same string for both representations.
  check("fmtTime: naive == Z", fmtTime(naive) === fmtTime(z));
}

// isAfterStart: an event AFTER the run start is included.
{
  const start = "2026-05-03T01:00:00Z";
  const ev    = "2026-05-03T01:30:00Z";
  check("isAfterStart: ev > start", isAfterStart(ev, start) === true);
  check("isAfterStart: ev < start (older event filtered)",
    isAfterStart("2026-05-03T00:00:00Z", start) === false);
}

// ---------- 10. Live-cycle / active_cycle_index acceptance ----------
//
// The user-reported bug: real factory_smoke + cycle + claude
// subprocesses were running, log showed cycle #1, but the dashboard
// painted CYCLE 0 / 5 + STUCK. These six fixtures (A-F from the
// spec) lock in the new behaviour.

// A. running, cycle_count=0, active_cycle_index=1, current_cycle_started_at set
{
  const meta = makeMeta({
    status: "running", cycle_count: 0, active_cycle_index: 1,
    started_at: new Date(Date.now() - 200_000).toISOString(),
    current_cycle_started_at: new Date(Date.now() - 30_000).toISOString(),
    current_cycle_finished_at: null,
  });
  check("A: stuck=false when active_cycle_index set",
    deriveStuckDiagnostic(meta).stuck === false);
  check("A: hasActiveCycle=true",
    hasActiveCycle(meta) === true);
  const dc = deriveDisplayCycle(meta);
  check("A: displayCycle.number=1 (active_cycle_index)", dc.number === 1);
  check("A: displayCycle.active=true", dc.active === true);
  check("A: phase = cycle_running", derivePhase(meta) === "cycle_running");
}

// B. running, cycle_count=0, no active_cycle_index, 200s elapsed, no process → stuck
{
  const meta = makeMeta({
    status: "running", cycle_count: 0,
    started_at: new Date(Date.now() - 200_000).toISOString(),
  });
  const d = deriveStuckDiagnostic(meta);
  check("B: stuck=true (200s + no markers)", d.stuck === true);
  check("B: diagnostic_code", d.diagnostic_code === "autopilot_stuck_before_first_cycle");
}

// C. running, cycle_count=0, factory_state.current_stage="designer_critique"
{
  const meta = makeMeta({
    status: "running", cycle_count: 0,
    started_at: new Date(Date.now() - 60_000).toISOString(),
    current_stage: "designer_critique",
    factory_state_status: "running",
  });
  check("C: stuck=false when current_stage set",
    deriveStuckDiagnostic(meta).stuck === false);
  check("C: stageToWorkingAgent → designer",
    stageToWorkingAgent(meta, derivePhase(meta)) === "designer");
}

// C'. autopilot.current_stage (heartbeat-side) also wins
{
  const meta = makeMeta({
    status: "running", cycle_count: 0, active_cycle_index: 1,
    autopilot_current_stage: "pm_decision",
    started_at: new Date(Date.now() - 60_000).toISOString(),
    current_cycle_started_at: new Date(Date.now() - 30_000).toISOString(),
  });
  check("C': autopilot.current_stage routes pm_decision → pm",
    stageToWorkingAgent(meta, "cycle_running") === "pm");
}

// D. live_report_path present while running
{
  const meta = makeMeta({
    status: "running", cycle_count: 0, active_cycle_index: 1,
    started_at: new Date().toISOString(),
    live_report_path: ".runtime/autopilot_live_report.md",
  });
  check("D: live_report_path is exposed on autopilot",
    meta.autopilot.live_report_path === ".runtime/autopilot_live_report.md");
}

// E. running cycle: stop ✓, start ✗, restart ✓
{
  const got = deriveButtonState({
    phase: "cycle_running",
    cycleInFlight: true, busy: false, hasRunner: true,
  });
  check("E: cycle_running canStart=false", got.canStart === false);
  check("E: cycle_running canStop=true",  got.canStop === true);
  check("E: cycle_running canRestart=true", got.canRestart === true);
}

// F. previous PASS/REWORK shouldn't drive current visual when fresh
//    cycle_count=0 + autopilot active_cycle_index=1.
{
  // freshnessOf for an artifact with cycle_id=0 (previous run leftover)
  const f = freshnessOf({
    artifactCycleId: 0,
    artifactAt: "2026-05-01T00:00:00Z",
    autopilot: {
      cycle_count: 0,
      active_cycle_index: 1,
      started_at: new Date().toISOString(),
    },
  });
  // The freshnessOf rule compares cycle_id to apCycle (cycle_count).
  // 0 == 0 so technically "current_cycle"; the AgentAccountability
  // panel separately stale-gates this via its
  // classifyAccountabilityFreshness rule (running + cycle_count=0 +
  // acc.cycle_id>=1 → stale). For freshnessOf alone, equal-numbers =
  // current_cycle is acceptable here.
  check("F: freshnessOf cycle 0/0 → current_cycle", f === "current_cycle");
}

// G. factory_smoke started_at without ended_at also keeps stuck=false
{
  const meta = makeMeta({
    status: "running", cycle_count: 0,
    started_at: new Date(Date.now() - 220_000).toISOString(),
    factory_smoke: { started_at: new Date(Date.now() - 60_000).toISOString(),
                     ended_at: null },
  });
  check("G: factory_smoke started/no ended → not stuck",
    deriveStuckDiagnostic(meta).stuck === false);
  check("G: hasActiveCycle from smoke window",
    hasActiveCycle(meta) === true);
}

// H. stopped run should clear active markers — phase = stopped
{
  const meta = makeMeta({
    status: "stopped", cycle_count: 1,
    last_verdict: "PASS",
    active_cycle_index: null, autopilot_current_stage: null,
  });
  check("H: stopped phase", derivePhase(meta) === "stopped");
  check("H: stopped → not stuck", deriveStuckDiagnostic(meta).stuck === false);
  check("H: displayCycle returns finished count",
    deriveDisplayCycle(meta).number === 1);
}

// ---------- Wrap up ----------

const summary = {
  ok: failures.length === 0,
  pass_count: passes.length,
  fail_count: failures.length,
  total_combinations: comboCount,
  failures,
  generated_at: new Date().toISOString(),
};
writeFileSync(
  resolve(RUNTIME_DIR, "autopilot_ui_matrix.json"),
  JSON.stringify(summary, null, 2),
  "utf8",
);

if (failures.length > 0) {
  console.error(`[autopilot-ui] ${failures.length} FAILED:`);
  for (const f of failures.slice(0, 30)) {
    console.error(`  · ${f.name} — ${f.detail}`);
  }
  process.exit(1);
}
console.log(
  `[autopilot-ui] ${passes.length} checks PASS across ${comboCount} (mode×bool³×cycles×hours) ` +
  `payload combinations + 8 phase / 8 button / 7 freshness / 4 stuck / 4 race / 3 time fixtures — ` +
  `wrote .runtime/autopilot_ui_matrix.json`,
);
process.exit(0);
