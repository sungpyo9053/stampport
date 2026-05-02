# Factory Smoke Runner

Self-verifying acceptance entrypoint for the Stampport automation
factory. Replaces the "operator stares at `tail -f` for 5 minutes and
guesses" workflow with a single command that returns a verdict.

```bash
python3 -m control_tower.local_runner.factory_smoke \
    --mode local-cycle --timeout 1800
```

The smoke runner performs preflight cleanup, drives a cycle (or
simulates one), polls `.runtime/factory_state.json`, applies per-stage
timeouts, invokes the Observer, classifies the run into a final
verdict, and writes both a Markdown report and a Claude repair prompt
when the run fails. The operator no longer needs to watch logs in real
time — every signal lands in `.runtime/`.

## Modes

| Flag                          | What it does                                                                           |
| ----------------------------- | -------------------------------------------------------------------------------------- |
| `--mode local-cycle`          | Spawns `cycle.py` as a subprocess and watches it end-to-end.                           |
| `--mode bridge`               | Verifies the runner's bridge pause policy (no pause when `desired=running`).           |
| `--mode observer-only`        | Ticks the Observer once and surfaces its classification — no subprocess, fast.         |
| `--self-test`                 | Runs the built-in acceptance fixtures (no real cycle, no `claude` calls).              |
| `--timeout SEC`               | Overall wall-clock cap (default 1800).                                                 |
| `--json`                      | Emits the verdict JSON to stdout for scripted consumption.                             |

## Verdicts

| Verdict             | Meaning                                                                                    |
| ------------------- | ------------------------------------------------------------------------------------------ |
| `PASS`              | Cycle finished cleanly — `succeeded` / `planning_only` / `no_code_change` / `docs_only`.   |
| `READY_TO_REVIEW`   | Code changed + QA passed, but auto-publish is disabled — operator must approve.            |
| `READY_TO_PUBLISH`  | Same as above, but publish IS enabled — `publish_changes` / `deploy_to_server` will commit.|
| `HOLD`              | PM decision was HOLD (재작업). Development stages were intentionally skipped.              |
| `FAIL`              | Anything else. See `factory_failure_report.md` and `claude_repair_prompt.md`.              |

## Per-stage timeout policy

| Stage                       | Budget (s) |
| --------------------------- | ---------- |
| `git_check`                 | 60         |
| `publish_blocker_check`     | 60         |
| `publish_blocker_resolve`   | 120        |
| `product_planning`          | 600        |
| `designer_critique`         | 360        |
| `planner_revision`          | 360        |
| `designer_final_review`     | 360        |
| `pm_decision`               | 180        |
| `build_app` / `build_control` / `syntax_check` | 180 |
| `claude_propose`            | 420        |
| `implementation_ticket`     | 120        |
| `claude_apply`              | 600        |
| `qa_gate` / `qa_recheck`    | 180        |
| `qa_fix_propose` / `qa_fix_apply` | 420 / 600 |

The overall `--timeout` (default 1800s) is the wall-clock kill switch;
per-stage budgets are advisory — a stage that exceeds its budget gets
a `timeout` row in the report but the smoke runner keeps polling
until the cycle exits or the wall-clock cap is hit.

## Output files

All paths are relative to the repository's `.runtime/` directory.

| File                              | When                       | Purpose                                                                |
| --------------------------------- | -------------------------- | ---------------------------------------------------------------------- |
| `factory_smoke_state.json`        | Always                     | Machine-readable verdict, stage table, supplemental metadata.          |
| `factory_smoke_report.md`         | Always                     | Human-readable summary — verdict, stage table, operator next action.   |
| `factory_smoke.log`               | Always                     | Chronological log of the smoke run + the cycle subprocess's stdout.    |
| `factory_failure_report.md`       | When `verdict == FAIL`     | Failure report with Observer evidence and stage table.                 |
| `claude_repair_prompt.md`         | When `verdict == FAIL`     | Claude-direct repair prompt with target files + verification steps.    |
| `claude_rework_prompt.md`         | When `verdict == HOLD`     | Next-cycle planner input — PM HOLD 약점 / 다음 단계 / 미달 점수 정리. |

## Default-safe environment

Preflight sets safe defaults if the operator hasn't set them
explicitly. We never overwrite existing values.

| Variable                            | Default       | Why                                                          |
| ----------------------------------- | ------------- | ------------------------------------------------------------ |
| `LOCAL_RUNNER_ALLOW_PUBLISH`        | `false`       | No automatic `git commit` / `git push`.                      |
| `LOCAL_RUNNER_PUBLISH_DRY_RUN`      | `true`        | If publish is somehow triggered, log instead of executing.   |
| `LOCAL_RUNNER_RESTART_DRY_RUN`      | `true`        | Don't restart the runner from inside a smoke run.            |
| `FACTORY_RUN_CLAUDE`                | `true`        | Real cycles need Claude — set `false` for dry runs.          |
| `FACTORY_APPLY_CLAUDE`              | `true`        | Same.                                                        |
| `FACTORY_PRODUCT_PLANNER_MODE`      | `true`        | Always on — Stampport's pipeline depends on it.              |
| `FACTORY_PLANNER_DESIGNER_PINGPONG` | `true`        | Same.                                                        |
| `FACTORY_WATCHDOG_ENABLED`          | mode-dependent | `true` for `bridge`, `false` otherwise (avoids reentry).    |

## Bridge pause policy

The runner's `_reconcile_continuous_mode` translates Control Tower
desired-state into `factory.paused` markers. The corrected policy is:

| `desired_status` | `continuous_mode` | What the runner does                          |
| ---------------- | ----------------- | --------------------------------------------- |
| `running`        | `true`            | Loop continuously. NO pause marker.           |
| `running`        | `false`           | Single-shot run. **NO pause marker.**         |
| `paused` / `idle`| any               | Write pause marker. `pause applied` log emitted. |

`continuous=false` means "do not loop", **not** "halt". The pause
marker is reserved for the explicit halt signal (`desired in {paused,
idle}`). The smoke runner's `--mode bridge` verifies this by checking
both the log tail (`pause applied (... desired=running)` ⇒ FAIL) and
the on-disk marker presence.

## PM HOLD policy

When `pm_decision_ship_ready=false` (PM verdict is HOLD), the
following stages are skipped:

- `claude_propose` → status `skipped`, reason `PM HOLD`
- `implementation_ticket` → status `skipped_hold`
- `claude_apply` → status `skipped` (cascade)

The cycle's terminal status becomes `hold_for_rework` and the smoke
verdict is `HOLD` — a non-failure outcome. The operator can opt out
with `FACTORY_ALLOW_PM_HOLD_TO_IMPLEMENT=true` for cases where the
planner-rework loop is broken and forward progress is required anyway.

When verdict is `HOLD`, the smoke runner writes
`.runtime/claude_rework_prompt.md` (not `claude_repair_prompt.md` —
nothing failed). The next cycle's `stage_product_planning` reads
`pm_decision.md` + `designer_final_review.md` and prepends a
`Previous PM HOLD` section to the planner prompt so the next planner
treats the prior weakness as the bottleneck instead of proposing a
fresh, unrelated set of candidates.

## implementation_ticket statuses

| Status                              | Meaning                                                                  |
| ----------------------------------- | ------------------------------------------------------------------------ |
| `generated`                         | Ticket written with `target_files >= 1`.                                 |
| `skipped_hold`                      | PM verdict was HOLD — ticket intentionally not generated.                |
| `pm_scope_missing_target_files`     | PM SHIP, but PM/planner artifacts had no `app/*` or `control_tower/*` paths. |
| `failed`                            | Disk write failed (rare).                                                |
| `missing`                           | Legacy alias for `pm_scope_missing_target_files` (kept for compatibility). |
| `skipped`                           | Other skip reason (publish blocker, etc.).                               |

## Observer diagnostic codes added 2026-05-02

| Code                            | Category | Failure? | When                                                          |
| ------------------------------- | -------- | -------- | ------------------------------------------------------------- |
| `fresh_idle`                    | healthy  | no       | factory_state empty / `cycle == 0` / no log activity.         |
| `bridge_pause_mismatch`         | failure  | yes      | Log shows `pause applied (... desired=running)`.              |
| `pm_hold_for_rework`            | hold     | no       | factory_state.status == `hold_for_rework`.                    |
| `pm_scope_missing_target_files` | failure  | yes      | PM SHIP but `target_files == []`.                             |
| `ready_to_review`               | review   | no       | changed_files + QA passed + publish disabled.                 |
| `smoke_timeout` / `smoke_passed` / `smoke_failed` | varies | varies | Smoke runner verdict promotion (used inside the report).      |

## Self-test fixtures

`python3 -m control_tower.local_runner.factory_smoke --self-test`
runs fifteen acceptance fixtures, all stdlib-only, no `claude` calls
required:

1. **fresh runtime** → `fresh_idle` (info, healthy).
2. **desired=running + continuous=false** → no `bridge_pause_mismatch`.
3. **PM HOLD** → `HOLD` verdict + `pm_hold_for_rework` reason.
4. **PM SHIP + target_files** → ticket `generated`, verdict `READY_TO_REVIEW`.
5. **changed_files=3 + qa=passed + publish disabled** → `READY_TO_REVIEW`.
6. **`__pycache__` / `.pyc` / `.runtime/` / `node_modules/`** → filtered from `git add`.
7. **stale deploy failure** → does NOT contaminate the latest `ready_to_review`.
8. **`pause applied (continuous=False, desired=running)` log** → `bridge_pause_mismatch`.
9. **smoke timeout** → repair prompt mentions `smoke_timeout` + the suspect stage.
10. **multi-state mock** → verdict resolves to `PASS` / `READY_TO_REVIEW` / `HOLD`.
11. **output writers** → `factory_smoke_state.json` + report always emitted.
12. **planner heading contract** — both `## 신규 기능 아이디어 후보` and the legacy `## 신규 장치 아이디어 후보` alias pass the gate; selected feature extracts under both `이번 사이클 선정 기능` and `이번 사이클 선정 장치`.
13. **PM HOLD rework prompt** — `verdict == HOLD` writes `.runtime/claude_rework_prompt.md`, leaves `claude_repair_prompt.md` absent, and surfaces the rework path in `factory_smoke_report.md`.
14. **PM HOLD planner injection** — when `pm_decision.md` says hold, the next planner prompt prepends a `Previous PM HOLD` section with 약점 / 다음 단계 / 미달 점수.
15. **HOLD ≠ FAIL contract** — observer + smoke + factory_state all classify HOLD as non-failure; `implementation_ticket_status` stays `skipped_hold`.

The Observer's own self-test
(`python3 -m control_tower.local_runner.factory_observer --self-test`)
covers an additional 25 cases including the new diagnostic codes.

## Recommended verification cadence

After any change to runner / cycle / observer / smoke files:

```bash
# 1. py_compile every Python module
python3 -m py_compile control_tower/local_runner/runner.py \
    control_tower/local_runner/cycle.py \
    control_tower/local_runner/control_state.py \
    control_tower/local_runner/agent_supervisor.py \
    control_tower/local_runner/factory_observer.py \
    control_tower/local_runner/factory_smoke.py

# 2. Observer self-test (25 fixtures)
python3 -m control_tower.local_runner.factory_observer --self-test

# 3. Smoke self-test (11 fixtures)
python3 -m control_tower.local_runner.factory_smoke --self-test

# 4. Web build
cd control_tower/web && npm run build

# 5. Real local smoke (only when the above all pass + claude CLI is available)
python3 -m control_tower.local_runner.factory_smoke \
    --mode local-cycle --timeout 1800
```

The smoke runner always exits 0 for `PASS` / `READY_TO_REVIEW` /
`READY_TO_PUBLISH` / `HOLD` and 1 for `FAIL` — so it can be wired
into CI or a `make` target without any extra adapter logic.
