"""Stampport Local Factory — single check cycle.

Run this from the repo root. The script performs ONE pass of the local
automated-checks loop and writes its result to:

    .runtime/factory_state.json     (machine-readable progress + status)
    .runtime/factory_last_report.md (human-readable report)
    .runtime/local_factory.log      (append-only structured log lines)

It NEVER commits, pushes, or modifies tracked source files. It is also
deliberately stdlib-only: no third-party imports — that way it works
under the system Python on the Mac as well as the project's venv.

The cycle runs these stages in order:
    prepare → git_check → build_app → build_control →
    syntax_check → report → waiting

Stage failures are recorded but do not abort the cycle: we want a full
report every time, even if app/web fails to build. The cycle's overall
status is `failed` if any stage failed, `succeeded` otherwise.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[2]))
RUNTIME = REPO_ROOT / ".runtime"
STATE_FILE = RUNTIME / "factory_state.json"
REPORT_FILE = RUNTIME / "factory_last_report.md"
LOG_FILE = RUNTIME / "local_factory.log"
GOAL_FILE = RUNTIME / "factory_goal.txt"
PAUSE_FILE = RUNTIME / "factory.paused"
PROPOSAL_FILE = RUNTIME / "claude_proposal.md"
APPLY_DIFF_FILE = RUNTIME / "claude_apply.diff"
# Claude Executor Contract artifacts. These persist independently of
# CycleState so the autopilot retry policy + dashboard can reason about
# CLI health without re-deriving the verdict from the (very wide)
# factory_state.json. The contract is: claude_executor_state.json is
# rewritten at every claude_apply / claude_preflight call; the
# stdout/stderr/command logs are forensic-only and never re-applied.
CLAUDE_EXECUTOR_STATE_FILE = RUNTIME / "claude_executor_state.json"
CLAUDE_APPLY_STDOUT_FILE = RUNTIME / "claude_apply_stdout.log"
CLAUDE_APPLY_STDERR_FILE = RUNTIME / "claude_apply_stderr.log"
CLAUDE_APPLY_COMMAND_FILE = RUNTIME / "claude_apply_command.json"
# Forensic artifacts for claude_apply revalidation rollback. When the
# post-apply build/syntax/risky scan fails, we save:
#   - the diff that was about to be rolled back, so the operator (or
#     the next cycle's repair prompt) can see exactly which patch
#     broke the build, and
#   - the build_app stdout/stderr that triggered the rejection.
# Both files are pure forensics — they are never re-applied automatically.
APPLY_ROLLED_BACK_DIFF_FILE = RUNTIME / "claude_apply_rolled_back.diff"
APP_BUILD_AFTER_APPLY_LOG = RUNTIME / "app_build_after_apply.log"
# Product Planner v2 — replaces the older Product Discovery Mode
# (.runtime/product_discovery.md). The new file enforces a richer
# template (LLM need, data storage, MVP scope, success criteria) and
# is the canonical artifact going forward.
PRODUCT_PLANNER_FILE = RUNTIME / "product_planner_report.md"

# Planner ↔ Designer ping-pong artifacts (opt-in via
# FACTORY_PLANNER_DESIGNER_PINGPONG=true). Each step writes its own
# file so the dashboard's PingPongBoard / ArtifactBoard can render
# distinct cards instead of one rolling document. The desire scorecard
# is JSON so the gate logic + dashboard can read scores without
# Markdown parsing.
PLANNER_PROPOSAL_FILE       = RUNTIME / "planner_proposal.md"
DESIGNER_CRITIQUE_FILE      = RUNTIME / "designer_critique.md"
PLANNER_REVISION_FILE       = RUNTIME / "planner_revision.md"
DESIGNER_FINAL_REVIEW_FILE  = RUNTIME / "designer_final_review.md"
PM_DECISION_FILE            = RUNTIME / "pm_decision.md"
DESIRE_SCORECARD_FILE       = RUNTIME / "desire_scorecard.json"
# Design Implementation Spec — written by stage_design_spec when the
# previous cycle's PM HOLD reasons mention concrete implementation
# gaps (SVG path, titleLabel, ShareCard layout, badges.js schema, ...).
# The PM stage accepts this artifact as a SHIP-equivalent signal when
# its acceptance criteria pass (SVG numeric coordinates, ≥13
# titleLabel, ≥3 target files, ShareCard render rule, QA criteria),
# so the rework loop can advance to implementation_ticket / claude_apply
# instead of staying stuck in abstract design discussion.
DESIGN_SPEC_FILE            = RUNTIME / "design_spec.md"
# Implementation Ticket — single source of truth for "what code does
# this cycle actually intend to write?". Lives between PM 결정 and
# claude_apply. claude_apply refuses to run if the ticket is missing
# or has no concrete target files. See stage_implementation_ticket.
IMPLEMENTATION_TICKET_FILE  = RUNTIME / "implementation_ticket.md"
# Active rework feature lock — when PM HOLD is observed at the end of
# a cycle, we persist the canonical selected_feature here so the next
# cycle's planner is forced to keep working on the same feature instead
# of proposing 3 brand-new candidates and starving the rework loop.
# Cleared automatically after claude_apply succeeds (real code change
# shipped) or when the operator deletes the file.
ACTIVE_REWORK_FEATURE_FILE = RUNTIME / "active_rework_feature.json"
# Stale runtime artifacts isolated by stage_runtime_artifact_sweep are
# moved here (one timestamped subdirectory per sweep) so the operator
# can still inspect them but no downstream stage can mistake them for
# the current cycle's output.
STALE_ARTIFACTS_DIR = RUNTIME / "stale_artifacts"
# QA Gatekeeper artifacts. qa_report.md is always (re)written by
# stage_qa_gate; qa_feedback.md is written ONLY when a check fails so
# the next cycle's qa_fix_propose stage has a precise repro/instruction
# document to consume. qa_fix_state.json tracks attempt counts across
# cycles so the loop can't run forever.
QA_REPORT_FILE = RUNTIME / "qa_report.md"
QA_FEEDBACK_FILE = RUNTIME / "qa_feedback.md"
QA_FIX_STATE_FILE = RUNTIME / "qa_fix_state.json"

# Publish blocker artifacts. Three files, each with a different
# audience:
#   blocker_state.json     — machine-readable state for the dashboard
#                            heartbeat. The runner reads this whenever
#                            publish_changes is invoked outside a
#                            cycle, to make a decision without
#                            re-running the full classification.
#   blocker_resolve_report.md — human-readable post-mortem of what the
#                            resolve stage did (or refused to do).
#   blocker_recurring.json — counter of how many times each blocker
#                            file has appeared. After ~3 appearances
#                            we proactively clean it on cycle entry,
#                            so the same .claude/settings.local.json
#                            doesn't burn a stage every hour.
BLOCKER_STATE_FILE = RUNTIME / "blocker_state.json"
BLOCKER_REPORT_FILE = RUNTIME / "blocker_resolve_report.md"
BLOCKER_RECURRING_FILE = RUNTIME / "blocker_recurring.json"

DEFAULT_GOAL = (
    "Stampport의 수집/과시/성장/재방문 루프를 한 단계 더 강하게 만든다. "
    "기획자가 새 보상·뱃지·칭호·퀘스트·공유카드 장치를 제안하고, "
    "디자이너가 ‘진짜 갖고 싶고 자랑하고 싶은가’를 반박해 합의된 가장 작은 한 단계만 출하한다."
)


# Stampport domain profile — sourced once per cycle so prompts can
# embed the canonical product identity (categories, areas, palette,
# emotional QA loop). Keeps prompts in sync with what the FE renders
# without duplicating copy.
STAMPPORT_DOMAIN_PROFILE_PATH = (
    REPO_ROOT / "config" / "domain_profiles" / "stampport.json"
)
STAMPPORT_AGENT_COLLAB_PATH = REPO_ROOT / "docs" / "agent-collaboration.md"


def _load_stampport_profile_text() -> str:
    """Read stampport.json as a UTF-8 string. Returns "" on failure so
    prompt building never raises on a missing/malformed profile."""
    try:
        return STAMPPORT_DOMAIN_PROFILE_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def _load_agent_collab_text() -> str:
    try:
        return STAMPPORT_AGENT_COLLAB_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""

# Working-tree "위험 파일" pattern — used by stage_git_check and the
# post-apply revalidation. Only actual secret-shaped paths count.
# Cache/build artifact patterns (.runtime/, node_modules/, dist/,
# .venv/, __pycache__/) used to be in this list; they're now handled
# by the auto_delete bucket in publish_blocker_resolve and never
# treated as a publish blocker.
RISKY_PATTERNS: tuple[str, ...] = (
    ".env",
    ".pem",
    ".key",
    ".db",
)


# claude_apply sandbox — the only roots Claude is allowed to write to.
# We enforce this *post-hoc* on the actual diff (not just via prompt) so
# a misbehaving Claude can't slip a write past us.
ALLOWED_APPLY_DIRS: tuple[str, ...] = (
    "app/",
    "control_tower/",
    "scripts/",
)

# Anything matching one of these substrings is forbidden even inside an
# allowed dir (e.g., a stray .env or .key dropped under app/). The
# match is plain substring — fast and impossible to circumvent with
# path-normalization tricks since we never normalize.
FORBIDDEN_APPLY_PATTERNS: tuple[str, ...] = (
    ".env",
    ".key",
    ".pem",
    ".db",
    ".runtime/",
    "node_modules/",
    "dist/",
    ".venv/",
    "deploy/nginx-stampport.conf",
    ".github/workflows/",
    "systemd",
)


# ---------------------------------------------------------------------------
# Release Safety Gate policy
#
# Stampport 자동화 공장은 *변경을 만들기 위한* 시스템이다. 변경 파일이
# 있다는 사실 자체는 차단 사유가 아니다. 그래서 게이트는 두 종류의
# 신호를 분리해 추적한다:
#
#   blocker  — 정말 배포를 멈춰야 하는 사고(시크릿 노출, 충돌 마커,
#              빌드/문법/health 실패). 사람이 손대기 전에 push 하면
#              안 되는 사고.
#   warning  — "주의 깊게 보면 좋겠다" 수준의 신호(cycle.py 변경,
#              runner.py 변경, deploy script 변경, nginx template
#              변경, 큰 diff 등). build/health/secret이 통과했다면
#              이 신호는 차단 사유가 아니다.
#
# 결과적으로 publish_blocked는 hard_risky 또는 conflict_marker가
# 검출됐을 때만 True가 된다. manual_required(=warning)는 더 이상
# publish를 막지 않는다.
# ---------------------------------------------------------------------------


# 5-bucket classifier:
#   auto_restore   — known local config drift; `git restore <path>` clears it
#   auto_delete    — generated/cache junk; safe to remove outright
#   allowed_code   — ordinary source/code change; passes through to QA Gate
#                    + publish (subject to QA pass + secret scan)
#   manual_required — deploy/CI/build-config 등 *주의 깊게 보면 좋은*
#                    카테고리. publish 차단 사유가 아니라 warning 으로
#                    리포트에만 표기한다.
#   hard_risky     — secret/credential pattern; NEVER read content, NEVER log
#
# Verdict precedence (first match wins, top → bottom):
#   1. hard_risky pattern in path  → 'hard_risky'
#   2. manual_required pattern     → 'manual_required'  (warning, not blocker)
#   3. exact-match auto_restore    → 'auto_restore'
#   4. auto_delete pattern         → 'auto_delete'
#   5. allowed_code prefix         → 'allowed_code'
#   6. anything else (top-level CHANGELOG.md, docs/, etc.) → 'allowed_code'
#
# Hard-risky has the highest priority so a stray secret never leaks
# into auto_delete or allowed_code. The fallback used to be
# manual_required ("better safe than sorry") — that was the source of
# the false-positive deploy block. The new fallback is allowed_code:
# we trust the secret/conflict/build/health gates downstream and don't
# block on path shape alone.


# Hard-risky path/name patterns. Any substring match → never even open
# the file, never include the path in logs verbatim except the basename.
PUBLISH_HARD_RISKY_PATTERNS: tuple[str, ...] = (
    ".env",
    ".pem",
    ".key",
    ".db",
    "credentials",
    "private_key",
    "private-key",
    "id_rsa",
    "id_ed25519",
    "AWS_SECRET_ACCESS_KEY",
    "TELEGRAM_BOT_TOKEN",
    "KAKAO_ACCESS_TOKEN",
    "KAKAO_REFRESH_TOKEN",
    "SMTP_PASSWORD",
)

# Files we feel safe restoring with `git restore` because they're
# environment-local artifacts that shouldn't ride along on a publish.
PUBLISH_AUTO_RESTORE_FILES: tuple[str, ...] = (
    ".claude/settings.local.json",
    ".vscode/settings.json",
    ".idea/workspace.xml",
)

# Path substrings that, when seen in git status, can be removed
# without manual review — pure generated/cache output. We match
# substrings (not prefixes) so a nested __pycache__/foo.pyc anywhere
# in the tree is auto-deletable.
PUBLISH_AUTO_DELETE_PATTERNS: tuple[str, ...] = (
    "__pycache__/",
    ".pyc",
    "/dist/",
    ".DS_Store",
    "coverage/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "node_modules/",
)

# Source/code roots whose contents are normal "publishable code" —
# ordinary backend/frontend changes. The publish path still runs
# QA Gate / secret-scan / risky-scan on top, so we don't need to
# block here. Order is roughly most-specific → least-specific.
PUBLISH_ALLOWED_CODE_PREFIXES: tuple[str, ...] = (
    "app/api/",
    "app/web/",
    "control_tower/api/",
    "control_tower/web/",
    "control_tower/local_runner/",
    "scripts/",
    "deploy/",
    "config/",
    "docs/",
    ".github/",
)

# These dirs USED to force manual review before push. They are now
# warning-only: the publish path's secret-scan + build/health gates
# decide whether the change actually ships, not the directory name.
PUBLISH_MANUAL_ROOTS: tuple[str, ...] = (
    "deploy/",
    ".github/",
)

# File-name patterns that produce a *warning* (build/CI/infra config —
# not secret-shaped, but a wrong tweak here can break the whole
# deployment). Distinct from hard_risky because we WILL read / log the
# path; the report flags them so a human can eyeball the diff. They
# are NOT publish blockers — build/health/secret gates are.
PUBLISH_MANUAL_PATTERNS: tuple[str, ...] = (
    "package.json",
    "package-lock.json",
    "requirements.txt",
    "Dockerfile",
    "docker-compose",
    "systemd",
    "nginx",
)


# Conflict-marker scan. We treat the presence of git conflict markers
# in any tracked text file as a hard publish blocker — pushing a half-
# resolved merge produces a guaranteed broken main branch. Only the
# three canonical 7-character marker lines count, and we only scan
# files inside ALLOWED_APPLY_DIRS so we never accidentally open a
# `.env` or a binary asset.
CONFLICT_MARKER_TOKENS: tuple[str, ...] = (
    "<<<<<<<",
    "=======",
    ">>>>>>>",
)
# Files larger than this (bytes) are skipped during conflict-marker
# scanning — keeps the gate fast and avoids slurping binaries.
CONFLICT_SCAN_MAX_BYTES = 512 * 1024  # 512 KB

# File-name extensions that are textual enough to scan for conflict
# markers. Anything else is skipped — markers don't appear in PNGs.
CONFLICT_SCAN_TEXT_EXTS: tuple[str, ...] = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".yaml",
    ".yml", ".toml", ".sh", ".css", ".html", ".conf", ".cfg", ".ini",
    ".txt", ".sql", ".env.example",
)

# Each stage name maps to (label_in_korean, weight_for_progress).
# claude_propose has weight 0 because it's opt-in (FACTORY_RUN_CLAUDE)
# and we don't want the progress bar to lurch when it's the only stage
# that ran or didn't run.
STAGES: list[tuple[str, str, int]] = [
    ("prepare",                  "준비",                  5),
    ("git_check",                "Git 상태 점검",          15),
    # Publish-blocker gate sits BEFORE any new development. If
    # publish_blocker_check finds files that the publish step would
    # refuse, publish_blocker_resolve attempts auto-cleanup; anything
    # left after that fails the cycle and forces all downstream
    # development stages (product_planning / claude_propose /
    # claude_apply) into "skipped — blocked".
    ("publish_blocker_check",    "배포 차단 검사",         0),
    ("publish_blocker_resolve",  "배포 차단 정리",         0),
    # Claude Executor preflight — runs BEFORE any planning stage so a
    # missing / broken / unauthenticated `claude` CLI fails the cycle in
    # under 30 seconds instead of after 20+ minutes of planner +
    # designer + design_spec spending. Sets state.claude_executor_*
    # fields and writes claude_executor_state.json. On failure the cycle
    # short-circuits past every Claude-consuming stage so we never burn
    # planner budget on top of a broken executor.
    ("claude_preflight",         "Claude CLI 점검",         0),
    # Runtime artifact sweep — runs AFTER claude_preflight (so a broken
    # CLI doesn't waste time sweeping) and BEFORE product_planning, so
    # any leftover .runtime/design_spec.md / implementation_ticket.md /
    # claude_proposal.md from a previous run is moved aside before any
    # current-cycle stage can consume it as if it were fresh. The
    # sweep is the kernel mechanism behind the Cycle Source-of-Truth
    # Contract; apply_preflight is then free to act as the last
    # defense rather than the stale-file janitor.
    ("runtime_artifact_sweep",   "런타임 아티팩트 정리",     0),
    # Product Planner sits BEFORE the build/syntax gates: a planning
    # tick produces a report file that the later claude_propose stage
    # consumes verbatim. Runs only when FACTORY_PRODUCT_PLANNER_MODE is
    # on, so cost stays bounded.
    ("product_planning",         "제품 기획",              0),
    # Planner ↔ Designer ping-pong (opt-in via
    # FACTORY_PLANNER_DESIGNER_PINGPONG). Runs only after a clean
    # product_planning result. Each stage writes its own .runtime/
    # artifact and the desire scorecard gate decides whether the
    # cycle advances to claude_propose or stalls for rework.
    ("designer_critique",        "디자이너 반박",           0),
    ("planner_revision",         "기획자 수정안",           0),
    ("designer_final_review",    "디자이너 재평가",         0),
    # Design Implementation Spec — runs only when the previous cycle's
    # PM HOLD reasons hit one of the spec-mode keywords (SVG path,
    # titleLabel, badges.js, ShareCard, layout, 좌표, locked,
    # selectedTitle, 구현 명세). When triggered it forces the cycle
    # off the new-ideation track and onto a "lock the implementation
    # spec" track. PM uses .runtime/design_spec.md as a SHIP-equivalent
    # signal if acceptance passes.
    ("design_spec",              "디자인 구현 명세",         0),
    ("pm_decision",              "PM 최종 결정",            0),
    ("build_app",                "app/web 빌드",           25),
    ("build_control",            "control_tower/web 빌드",  25),
    ("syntax_check",             "문법 검사",              25),
    ("claude_propose",           "Claude 패치 제안",        0),
    # Implementation Ticket — bridges PM 결정 + claude_propose into the
    # single-source-of-truth ticket that claude_apply consumes. No
    # ticket, no apply.
    ("implementation_ticket",    "Implementation Ticket",   0),
    ("claude_apply",             "Claude 제안 적용",        0),
    # Stampport QA Gatekeeper — runs AFTER any code change this cycle.
    # Sub-checks: build artifact validation (app/web + control_tower/web
    # dist), API health (app/api/app/main.py py_compile + /health route
    # presence), screen presence (Stampport 8 screens under
    # app/web/src/screens), flow presence (mock login / stamp / passport
    # / badges / quests / share keywords in code), and domain profile
    # presence (config/domain_profiles/stampport.json +
    # docs/agent-collaboration.md). On failure: writes qa_feedback.md
    # and (if attempts left) runs qa_fix_propose / qa_fix_apply /
    # qa_recheck.
    ("qa_gate",                  "QA Gate",                 0),
    ("qa_feedback",              "QA Feedback 생성",        0),
    ("qa_fix_propose",           "QA Fix 제안",             0),
    ("qa_fix_apply",             "QA Fix 적용",             0),
    ("qa_recheck",               "QA 재검사",               0),
    ("report",                   "리포트 작성",             5),
]


# ---------------------------------------------------------------------------
# Time helpers — avoid the UTC/KST confusion that bit the heartbeat UI.
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    """Return an ISO-8601 string in UTC with explicit `Z` suffix.

    The Z suffix makes JS `new Date()` parse it as UTC. Naive ISO strings
    (no suffix) get treated as local time and produce phantom 9-hour
    skews when the browser is in KST.
    """
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# ---------------------------------------------------------------------------
# State / report / log writers
# ---------------------------------------------------------------------------


@dataclass
class StageResult:
    name: str
    label: str
    status: str = "pending"   # pending | running | passed | failed | skipped
    message: str = ""
    detail: str = ""
    duration_sec: float = 0.0


@dataclass
class CycleState:
    cycle: int = 1
    status: str = "running"
    current_stage: str = "prepare"
    current_task: str = ""
    progress: int = 0
    last_message: str = ""
    started_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    finished_at: str | None = None
    goal: str = ""
    risky_files: list[str] = field(default_factory=list)
    stages: list[StageResult] = field(default_factory=list)
    # Claude Executor Contract — written by stage_claude_preflight at
    # cycle start AND refreshed by stage_claude_apply when claude_apply
    # spawns a subprocess. Surfaces CLI health, last failure
    # classification, retryability, and forensic log paths so the
    # autopilot retry policy + dashboard never have to grep stderr
    # themselves. claude_executor_status is one of:
    #   passed | failed | timeout | retryable_failed | not_run
    # claude_executor_failure_code is the kernel classification (one of
    # the codes documented in classify_claude_failure). retryable=True
    # means the autopilot may attempt one immediate retry without
    # rebuilding planner/design_spec; retryable=False means stop.
    claude_executor_status: str = "not_run"
    claude_executor_stage: str | None = None
    claude_executor_command: str | None = None
    claude_executor_exit_code: int | None = None
    claude_executor_timed_out: bool = False
    claude_executor_duration_sec: float | None = None
    claude_executor_failure_code: str | None = None
    claude_executor_failure_reason: str | None = None
    claude_executor_stdout_path: str | None = None
    claude_executor_stderr_path: str | None = None
    claude_executor_retryable: bool = False
    claude_executor_retry_count: int = 0
    claude_executor_last_run_at: str | None = None
    claude_executor_max_cost_usd: str | None = None
    claude_executor_cost_budget_source: str | None = None
    claude_executor_exceeded_budget: bool = False
    # Claude proposal status (so the dashboard knows whether the
    # claude_propose stage actually wrote a file or just skipped).
    claude_proposal_status: str = "skipped"   # generated | skipped | failed
    claude_proposal_path: str | None = None
    claude_proposal_at: str | None = None
    claude_proposal_skipped_reason: str | None = None
    # Product Planner Mode output (opt-in via FACTORY_PRODUCT_PLANNER_MODE).
    # The planner now self-diagnoses ONE bottleneck and self-judges
    # LLM/storage/external integration needs, so the dashboard surfaces
    # those so a human reviewer sees not just "what feature" but
    # "why this one, what kind of tech does it actually need".
    product_planner_status: str = "skipped"   # generated | skipped | failed
    product_planner_path: str | None = None
    product_planner_at: str | None = None
    product_planner_bottleneck: str | None = None
    product_planner_selected_feature: str | None = None
    product_planner_solution_pattern: str | None = None
    product_planner_value_summary: str | None = None
    product_planner_llm_needed: str | None = None
    product_planner_data_storage_needed: str | None = None
    product_planner_external_integration_needed: str | None = None
    product_planner_frontend_scope: str | None = None
    product_planner_backend_scope: str | None = None
    product_planner_success_criteria: str | None = None
    product_planner_candidate_count: int = 0
    product_planner_message: str | None = None
    product_planner_skipped_reason: str | None = None
    product_planner_gate_failures: list[str] = field(default_factory=list)
    # Planner ↔ Designer ping-pong state. Each stage tracks its own
    # status / artifact path / one-line message so the dashboard can
    # render the 5-step ping-pong (planner proposal → designer
    # critique → planner revision → designer final review → PM
    # decision) plus the 6-axis desire scorecard.
    designer_critique_status: str = "skipped"   # generated|skipped|failed
    designer_critique_path: str | None = None
    designer_critique_at: str | None = None
    designer_critique_message: str | None = None
    designer_critique_skipped_reason: str | None = None
    planner_revision_status: str = "skipped"
    planner_revision_path: str | None = None
    planner_revision_at: str | None = None
    planner_revision_message: str | None = None
    planner_revision_skipped_reason: str | None = None
    planner_revision_selected_feature: str | None = None
    designer_final_review_status: str = "skipped"
    designer_final_review_path: str | None = None
    designer_final_review_at: str | None = None
    designer_final_review_message: str | None = None
    designer_final_review_skipped_reason: str | None = None
    designer_final_review_verdict: str | None = None  # pass|revise|reject
    pm_decision_status: str = "skipped"
    pm_decision_path: str | None = None
    pm_decision_at: str | None = None
    pm_decision_message: str | None = None
    pm_decision_skipped_reason: str | None = None
    pm_decision_ship_ready: bool = False
    # Hold classification — set in stage_implementation_ticket when
    # PM HOLD is observed. soft = "selected_feature + target_files exist
    # so the cycle can still build something to fix the HOLD"; hard =
    # "no candidate / no target_files / scope mismatch / domain
    # violation — implementation must not run". Soft HOLD allows
    # design_spec / implementation_ticket / claude_propose to proceed.
    pm_hold_type: str | None = None  # soft | hard | None
    pm_hold_type_reason: str | None = None
    pm_hold_soft_signals: list[str] = field(default_factory=list)
    # Active rework feature lock — populated when the previous cycle
    # ended in PM HOLD and saved a canonical feature name so the
    # current cycle's planner cannot drift to a new candidate. Carries
    # over until claude_apply succeeds or operator clears it.
    active_rework_feature: str | None = None
    active_rework_hold_count: int = 0
    planner_feature_drift_detected: bool = False
    planner_feature_drift_reason: str | None = None
    # design_spec stage: only runs when prior PM HOLD has spec-mode
    # keywords. status moves through skipped|generated|failed|insufficient.
    # pm_hold_spec_mode_active reflects whether *this* cycle was forced
    # into spec mode by the rework context.
    pm_hold_spec_mode_active: bool = False
    pm_hold_spec_keywords: list[str] = field(default_factory=list)
    design_spec_status: str = "skipped"
    design_spec_path: str | None = None
    design_spec_at: str | None = None
    design_spec_message: str | None = None
    design_spec_skipped_reason: str | None = None
    design_spec_target_files: list[str] = field(default_factory=list)
    design_spec_titlelabel_count: int = 0
    design_spec_svg_paths: list[str] = field(default_factory=list)
    design_spec_acceptance_passed: bool = False
    design_spec_acceptance_failures: list[str] = field(default_factory=list)
    # Stale design_spec isolation — set when the on-disk
    # .runtime/design_spec.md belongs to a previous cycle whose feature
    # disagrees with the current cycle's selected feature. The PM stage
    # excludes the spec body from its prompt and refuses spec_bypass
    # whenever this flag is True. See _classify_design_spec_freshness.
    stale_design_spec_detected: bool = False
    stale_design_spec_feature: str | None = None
    stale_design_spec_cycle_id: int | None = None
    stale_design_spec_reason: str | None = None
    current_cycle_feature: str | None = None
    # Desire scorecard (1~5 each, total /30). ship_ready is True only
    # when the threshold gate (≥24 total, visual_desire ≥4, share ≥4,
    # revisit ≥4) passes. rework_required lists the axis ids that
    # tripped a re-work rule so the dashboard can show "디자이너 재작업"
    # / "공유 카드 개선 필요" / "기획자 재작업" badges.
    desire_scorecard: dict[str, int] = field(default_factory=dict)
    desire_scorecard_total: int = 0
    desire_scorecard_path: str | None = None
    desire_scorecard_ship_ready: bool = False
    desire_scorecard_rework: list[str] = field(default_factory=list)
    # Claude apply status — applied / rolled_back / failed / noop / skipped.
    claude_apply_status: str = "skipped"
    claude_apply_at: str | None = None
    claude_apply_changed_files: list[str] = field(default_factory=list)
    claude_apply_diff_path: str | None = None
    claude_apply_rollback: bool = False
    claude_apply_skipped_reason: str | None = None
    claude_apply_message: str | None = None
    # Publish blocker policy — 5-bucket classifier. Populated by
    # stage_publish_blocker_check and stage_publish_blocker_resolve.
    #   auto_restored   — files we ran `git restore` on
    #   auto_deleted    — files we deleted (cache/build junk)
    #   allowed_code    — normal source code change; not a blocker, but
    #                     listed for the human reviewer
    #   manual_required — deploy/build-config; cycle stays blocked
    #   hard_risky      — secret/credential; cycle stays blocked,
    #                     contents NEVER opened, basenames only
    publish_blocked: bool = False
    # publish_blocker_status:
    #   clean    — no blockers, no warnings, no auto-cleanup activity
    #   resolved — auto-cleanup ran and the tree is now clean
    #   warning  — Release Safety Gate passed with warnings (the *new*
    #              non-blocking state for cycle.py/runner.py/deploy
    #              script/nginx/large-diff style changes)
    #   blocked  — actual blocker (hard_risky or conflict_marker)
    publish_blocker_status: str = "clean"
    auto_resolved_files: list[str] = field(default_factory=list)   # back-compat alias
    auto_restored_files: list[str] = field(default_factory=list)
    auto_deleted_files: list[str] = field(default_factory=list)
    allowed_code_files: list[str] = field(default_factory=list)
    manual_required_files: list[str] = field(default_factory=list)  # WARNING bucket
    hard_risky_files: list[str] = field(default_factory=list)
    conflict_marker_files: list[str] = field(default_factory=list)
    warning_reasons: list[str] = field(default_factory=list)
    publish_blocker_message: str | None = None
    publish_blocker_report_path: str | None = None
    publish_blocker_recurring: dict[str, int] = field(default_factory=dict)
    # QA Gatekeeper state. Each sub-check resolves to passed / failed /
    # skipped. publish_allowed gates runner.py's publish_changes
    # handler — it refuses unless qa_gate signed off.
    qa_status: str = "skipped"   # passed | failed | skipped
    qa_publish_allowed: bool = False
    qa_failed_reason: str | None = None
    qa_failed_categories: list[str] = field(default_factory=list)
    # Stampport QA sub-check statuses (passed | failed | skipped).
    qa_build_artifact: str = "skipped"
    qa_api_health: str = "skipped"
    qa_screen_presence: str = "skipped"
    qa_flow_presence: str = "skipped"
    qa_domain_profile: str = "skipped"
    qa_report_path: str | None = None
    qa_feedback_path: str | None = None
    qa_fix_attempt: int = 0
    qa_fix_max_attempts: int = 2
    qa_fix_propose_status: str = "skipped"
    qa_fix_apply_status: str = "skipped"
    # Cycle effectiveness — was this cycle "real" (touched product
    # code) or did it spin without changing anything? Populated at the
    # end of main() and surfaced via heartbeat metadata so the
    # dashboard can show "이번 사이클 실제 변경" instead of just a green
    # check on a no-op.
    code_changed: bool = False
    no_code_change_reason: str | None = None
    failed_stage: str | None = None
    failed_reason: str | None = None
    suggested_action: str | None = None
    cycle_log: list[dict] = field(default_factory=list)
    # Implementation Ticket — written by stage_implementation_ticket
    # after PM 결정. claude_apply gates on this when ping-pong is
    # enabled: no ticket / no target files = cycle stays planning_only.
    implementation_ticket_status: str = "skipped"  # generated|missing|skipped|failed
    implementation_ticket_path: str | None = None
    implementation_ticket_at: str | None = None
    implementation_ticket_selected_feature: str | None = None
    implementation_ticket_target_files: list[str] = field(default_factory=list)
    implementation_ticket_target_screens: list[str] = field(default_factory=list)
    implementation_ticket_message: str | None = None
    implementation_ticket_skipped_reason: str | None = None
    # Where each piece of the cycle's "what to build" answer came from.
    # On spec_bypass cycles, all three should be `design_spec` so an old
    # planner feature (e.g. Local Visa) cannot leak into the ticket /
    # apply / diff. The factory_smoke report cross-checks these against
    # the design_spec acceptance gate.
    selected_feature: str | None = None
    selected_feature_id: str | None = None
    selected_feature_source: str | None = None     # planner|design_spec|...
    implementation_ticket_source: str | None = None  # planner_proposal|design_spec
    implementation_ticket_feature_id: str | None = None
    claude_apply_source: str | None = None         # claude_proposal|design_spec
    design_spec_feature: str | None = None
    design_spec_feature_id: str | None = None
    # Cycle Source-of-Truth Contract — locked once per cycle by
    # `_lock_source_of_truth` after product_planning / planner_revision
    # finishes. Every downstream stage (design_spec / implementation_
    # ticket / claude_propose / claude_apply) MUST emit feature_id ==
    # source_of_truth_feature_id; `validate_source_of_truth_contract`
    # enforces this and the apply_preflight short-circuits with code
    # `source_of_truth_mismatch` on violation. The lock prevents the
    # "design_spec belongs to a previous cycle / feature" symptom by
    # making one feature_id authoritative for the whole cycle instead
    # of letting each stage re-derive a candidate.
    source_of_truth_feature: str | None = None
    source_of_truth_feature_id: str | None = None
    # The stage that produced the canonical feature: one of
    # "planner_revision" | "product_planning" | "active_rework_feature".
    source_of_truth_stage: str | None = None
    source_of_truth_locked_at: str | None = None
    source_of_truth_contract_status: str | None = None  # locked|missing|failed|None
    source_of_truth_contract_reason: str | None = None
    # claude_proposal feature_id — set by stage_claude_propose so the
    # SoT validator can compare against the SoT slug without re-parsing
    # the proposal artifact header.
    claude_proposal_feature_id: str | None = None
    # Runtime artifact sweep — populated by stage_runtime_artifact_sweep
    # at cycle start (after claude_preflight, before product_planning).
    # Stale (cross-run) .runtime/ artifacts get moved to
    # `.runtime/stale_artifacts/<timestamp>/` instead of deleted so the
    # operator can still inspect them; same-run artifacts are kept in
    # place so apply-only retry can reuse implementation_ticket.md /
    # claude_proposal.md.
    runtime_artifact_sweep_status: str = "not_run"  # passed|skipped|not_run
    runtime_artifact_sweep_isolated_count: int = 0
    runtime_artifact_sweep_isolated_files: list[str] = field(default_factory=list)
    runtime_artifact_sweep_current_run_id: str | None = None
    # run_id — the autopilot run identifier this cycle belongs to. Set
    # at cycle start from FACTORY_RUN_ID / autopilot_state. Every
    # artifact this cycle writes carries the same id, so the UI's
    # freshness verdict (PREVIOUS RUN vs CURRENT CYCLE) is unambiguous
    # even when cycle counters reset across runs.
    run_id: str | None = None
    # Pipeline contract validators — populated by run_stage_contract
    # and surfaced in factory_state.json so smoke / observer / dashboard
    # can render the table without re-deriving. Each entry:
    #   {"name", "ok", "code", "message"}
    contract_results: list[dict] = field(default_factory=list)
    # Apply preflight outcome (set by stage_claude_apply before it
    # spends Claude budget). One of: passed | scope_mismatch_preflight |
    # stale_artifact_preflight | missing_ticket_contract |
    # feature_lock_conflict | None (preflight didn't run).
    apply_preflight_status: str | None = None
    apply_preflight_reason: str | None = None
    # Scope-consistency QA gate — set by claude_apply when the diff is
    # checked against design_spec target_files + keywords on spec_bypass
    # cycles. failed → verdict downgrades to FAIL (factory_smoke surfaces
    # diagnostic_code=scope_mismatch).
    scope_consistency_status: str | None = None    # passed|failed|not_applicable
    scope_mismatch_reason: str | None = None
    scope_consistency_keywords_matched: list[str] = field(default_factory=list)
    scope_consistency_keywords_total: int = 0
    # Per-tier file-change flags — set in main() after claude_apply by
    # _categorize_changed_files. The dashboard shows "FE 변경 / BE 변경 /
    # 관제실 변경" badges off these so the operator can tell at a glance
    # whether the cycle hit the user-facing surface.
    frontend_changed: bool = False
    backend_changed: bool = False
    control_tower_changed: bool = False
    docs_only: bool = False

    def to_dict(self) -> dict:
        payload = {
            "status": self.status,
            "current_stage": self.current_stage,
            "current_task": self.current_task,
            "progress": self.progress,
            "last_message": self.last_message,
            "cycle": self.cycle,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "goal": self.goal,
            "risky_files": list(self.risky_files),
            "stages": [s.__dict__ for s in self.stages],
            "product_planner_status": self.product_planner_status,
            "product_planner_path": self.product_planner_path,
            "product_planner_at": self.product_planner_at,
            "product_planner_bottleneck": self.product_planner_bottleneck,
            "product_planner_selected_feature": self.product_planner_selected_feature,
            "product_planner_solution_pattern": self.product_planner_solution_pattern,
            "product_planner_value_summary": self.product_planner_value_summary,
            "product_planner_llm_needed": self.product_planner_llm_needed,
            "product_planner_data_storage_needed": self.product_planner_data_storage_needed,
            "product_planner_external_integration_needed": self.product_planner_external_integration_needed,
            "product_planner_frontend_scope": self.product_planner_frontend_scope,
            "product_planner_backend_scope": self.product_planner_backend_scope,
            "product_planner_success_criteria": self.product_planner_success_criteria,
            "product_planner_candidate_count": self.product_planner_candidate_count,
            "product_planner_message": self.product_planner_message,
            "product_planner_skipped_reason": self.product_planner_skipped_reason,
            "product_planner_gate_failures": list(self.product_planner_gate_failures),
            "designer_critique_status": self.designer_critique_status,
            "designer_critique_path": self.designer_critique_path,
            "designer_critique_at": self.designer_critique_at,
            "designer_critique_message": self.designer_critique_message,
            "designer_critique_skipped_reason": self.designer_critique_skipped_reason,
            "planner_revision_status": self.planner_revision_status,
            "planner_revision_path": self.planner_revision_path,
            "planner_revision_at": self.planner_revision_at,
            "planner_revision_message": self.planner_revision_message,
            "planner_revision_skipped_reason": self.planner_revision_skipped_reason,
            "planner_revision_selected_feature": self.planner_revision_selected_feature,
            "designer_final_review_status": self.designer_final_review_status,
            "designer_final_review_path": self.designer_final_review_path,
            "designer_final_review_at": self.designer_final_review_at,
            "designer_final_review_message": self.designer_final_review_message,
            "designer_final_review_skipped_reason": self.designer_final_review_skipped_reason,
            "designer_final_review_verdict": self.designer_final_review_verdict,
            "pm_decision_status": self.pm_decision_status,
            "pm_decision_path": self.pm_decision_path,
            "pm_decision_at": self.pm_decision_at,
            "pm_decision_message": self.pm_decision_message,
            "pm_decision_skipped_reason": self.pm_decision_skipped_reason,
            "pm_decision_ship_ready": self.pm_decision_ship_ready,
            "pm_hold_type": self.pm_hold_type,
            "pm_hold_type_reason": self.pm_hold_type_reason,
            "pm_hold_soft_signals": list(self.pm_hold_soft_signals),
            "active_rework_feature": self.active_rework_feature,
            "active_rework_hold_count": self.active_rework_hold_count,
            "planner_feature_drift_detected": self.planner_feature_drift_detected,
            "planner_feature_drift_reason": self.planner_feature_drift_reason,
            "pm_hold_spec_mode_active": self.pm_hold_spec_mode_active,
            "pm_hold_spec_keywords": list(self.pm_hold_spec_keywords),
            "design_spec_status": self.design_spec_status,
            "design_spec_path": self.design_spec_path,
            "design_spec_at": self.design_spec_at,
            "design_spec_message": self.design_spec_message,
            "design_spec_skipped_reason": self.design_spec_skipped_reason,
            "design_spec_target_files": list(self.design_spec_target_files),
            "design_spec_target_files_count": len(self.design_spec_target_files),
            "design_spec_titlelabel_count": self.design_spec_titlelabel_count,
            "design_spec_title_label_count": self.design_spec_titlelabel_count,
            "design_spec_svg_paths": list(self.design_spec_svg_paths),
            "design_spec_svg_path_valid": len(self.design_spec_svg_paths) >= 3,
            "design_spec_acceptance_passed": self.design_spec_acceptance_passed,
            "design_spec_acceptance_failures": list(self.design_spec_acceptance_failures),
            "design_spec_acceptance_errors": list(self.design_spec_acceptance_failures),
            "stale_design_spec_detected": self.stale_design_spec_detected,
            "stale_design_spec_feature": self.stale_design_spec_feature,
            "stale_design_spec_cycle_id": self.stale_design_spec_cycle_id,
            "stale_design_spec_reason": self.stale_design_spec_reason,
            "current_cycle_feature": self.current_cycle_feature,
            "desire_scorecard": dict(self.desire_scorecard),
            "desire_scorecard_total": self.desire_scorecard_total,
            "desire_scorecard_path": self.desire_scorecard_path,
            "desire_scorecard_ship_ready": self.desire_scorecard_ship_ready,
            "desire_scorecard_rework": list(self.desire_scorecard_rework),
            "claude_executor_status": self.claude_executor_status,
            "claude_executor_stage": self.claude_executor_stage,
            "claude_executor_command": self.claude_executor_command,
            "claude_executor_exit_code": self.claude_executor_exit_code,
            "claude_executor_timed_out": self.claude_executor_timed_out,
            "claude_executor_duration_sec": self.claude_executor_duration_sec,
            "claude_executor_failure_code": self.claude_executor_failure_code,
            "claude_executor_failure_reason": self.claude_executor_failure_reason,
            "claude_executor_stdout_path": self.claude_executor_stdout_path,
            "claude_executor_stderr_path": self.claude_executor_stderr_path,
            "claude_executor_retryable": self.claude_executor_retryable,
            "claude_executor_retry_count": self.claude_executor_retry_count,
            "claude_executor_last_run_at": self.claude_executor_last_run_at,
            "claude_executor_max_cost_usd": self.claude_executor_max_cost_usd,
            "claude_executor_cost_budget_source": self.claude_executor_cost_budget_source,
            "claude_executor_exceeded_budget": self.claude_executor_exceeded_budget,
            "claude_proposal_status": self.claude_proposal_status,
            "claude_proposal_path": self.claude_proposal_path,
            "claude_proposal_at": self.claude_proposal_at,
            "claude_proposal_skipped_reason": self.claude_proposal_skipped_reason,
            "claude_apply_status": self.claude_apply_status,
            "claude_apply_at": self.claude_apply_at,
            "claude_apply_changed_files": list(self.claude_apply_changed_files),
            "claude_apply_diff_path": self.claude_apply_diff_path,
            "claude_apply_rollback": self.claude_apply_rollback,
            "claude_apply_skipped_reason": self.claude_apply_skipped_reason,
            "claude_apply_message": self.claude_apply_message,
            "publish_blocked": self.publish_blocked,
            "publish_blocker_status": self.publish_blocker_status,
            "auto_resolved_files": list(self.auto_resolved_files),
            "auto_restored_files": list(self.auto_restored_files),
            "auto_deleted_files": list(self.auto_deleted_files),
            "allowed_code_files": list(self.allowed_code_files),
            "manual_required_files": list(self.manual_required_files),
            "hard_risky_files": list(self.hard_risky_files),
            "conflict_marker_files": list(self.conflict_marker_files),
            "warning_reasons": list(self.warning_reasons),
            "publish_blocker_message": self.publish_blocker_message,
            "publish_blocker_report_path": self.publish_blocker_report_path,
            "publish_blocker_recurring": dict(self.publish_blocker_recurring),
            "qa_status": self.qa_status,
            "qa_publish_allowed": self.qa_publish_allowed,
            "qa_failed_reason": self.qa_failed_reason,
            "qa_failed_categories": list(self.qa_failed_categories),
            "qa_build_artifact": self.qa_build_artifact,
            "qa_api_health": self.qa_api_health,
            "qa_screen_presence": self.qa_screen_presence,
            "qa_flow_presence": self.qa_flow_presence,
            "qa_domain_profile": self.qa_domain_profile,
            "qa_report_path": self.qa_report_path,
            "qa_feedback_path": self.qa_feedback_path,
            "qa_fix_attempt": self.qa_fix_attempt,
            "qa_fix_max_attempts": self.qa_fix_max_attempts,
            "qa_fix_propose_status": self.qa_fix_propose_status,
            "qa_fix_apply_status": self.qa_fix_apply_status,
            "code_changed": self.code_changed,
            "no_code_change_reason": self.no_code_change_reason,
            "failed_stage": self.failed_stage,
            "failed_reason": self.failed_reason,
            "suggested_action": self.suggested_action,
            "cycle_log": list(self.cycle_log),
            "implementation_ticket_status": self.implementation_ticket_status,
            "implementation_ticket_path": self.implementation_ticket_path,
            "implementation_ticket_at": self.implementation_ticket_at,
            "implementation_ticket_selected_feature": self.implementation_ticket_selected_feature,
            "implementation_ticket_target_files": list(self.implementation_ticket_target_files),
            "implementation_ticket_target_screens": list(self.implementation_ticket_target_screens),
            "implementation_ticket_message": self.implementation_ticket_message,
            "implementation_ticket_skipped_reason": self.implementation_ticket_skipped_reason,
            "selected_feature": self.selected_feature
                or self.implementation_ticket_selected_feature
                or self.product_planner_selected_feature,
            "selected_feature_id": self.selected_feature_id,
            "selected_feature_source": self.selected_feature_source,
            "implementation_ticket_source": self.implementation_ticket_source,
            "implementation_ticket_feature_id": self.implementation_ticket_feature_id,
            "claude_apply_source": self.claude_apply_source,
            "design_spec_feature": self.design_spec_feature,
            "design_spec_feature_id": self.design_spec_feature_id,
            "source_of_truth_feature": self.source_of_truth_feature,
            "source_of_truth_feature_id": self.source_of_truth_feature_id,
            "source_of_truth_stage": self.source_of_truth_stage,
            "source_of_truth_locked_at": self.source_of_truth_locked_at,
            "source_of_truth_contract_status": self.source_of_truth_contract_status,
            "source_of_truth_contract_reason": self.source_of_truth_contract_reason,
            "claude_proposal_feature_id": self.claude_proposal_feature_id,
            "runtime_artifact_sweep_status": self.runtime_artifact_sweep_status,
            "runtime_artifact_sweep_isolated_count":
                self.runtime_artifact_sweep_isolated_count,
            "runtime_artifact_sweep_isolated_files":
                list(self.runtime_artifact_sweep_isolated_files),
            "runtime_artifact_sweep_current_run_id":
                self.runtime_artifact_sweep_current_run_id,
            "run_id": self.run_id,
            "contract_results": list(self.contract_results),
            "apply_preflight_status": self.apply_preflight_status,
            "apply_preflight_reason": self.apply_preflight_reason,
            "scope_consistency_status": self.scope_consistency_status,
            "scope_mismatch_reason": self.scope_mismatch_reason,
            "scope_consistency_keywords_matched": list(
                self.scope_consistency_keywords_matched
            ),
            "scope_consistency_keywords_total": self.scope_consistency_keywords_total,
            "frontend_changed": self.frontend_changed,
            "backend_changed": self.backend_changed,
            "control_tower_changed": self.control_tower_changed,
            "docs_only": self.docs_only,
        }
        # Pipeline decision contract — the single source of truth for
        # autopilot publish/commit/push gating. Computed from this same
        # dict so smoke / autopilot / observer all see the identical
        # verdict cycle.py used to reach its own conclusion.
        try:
            payload["pipeline_decision"] = build_pipeline_decision(payload)
        except Exception as exc:  # noqa: BLE001
            payload["pipeline_decision"] = {
                "pipeline_status": "blocked",
                "can_commit": False,
                "can_push": False,
                "can_publish": False,
                "blocking_code": "pipeline_decision_error",
                "blocking_reason": f"build_pipeline_decision raised: {exc}",
                "checks": {},
                "evidence": {},
            }
        return payload


def _load_cycle_number() -> int:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return int(data.get("cycle", 0)) + 1
        except (json.JSONDecodeError, ValueError, OSError):
            return 1
    return 1


# ---------------------------------------------------------------------------
# Safe write helpers — atomic + crash-safe + never-raises.
#
# Every state file and every artifact file in .runtime/ goes through
# these. The runtime directory is the dashboard's only source of truth
# for "did this cycle actually happen", so a half-written
# factory_state.json or a missing artifact directly translates to
# Watchdog/Pipeline/Supervisor false-positives.
#
# Atomic write protocol:
#   1. mkdirs(parents=True, exist_ok=True) for the parent directory
#   2. write the new payload to <path>.tmp
#   3. flush + fsync
#   4. os.replace(tmp, target) — atomic on POSIX
#
# On any OSError we log to stderr + LOG_FILE but never raise. A cycle
# that lost a state write is already in trouble; raising would kill
# the bash factory loop and leave nothing on disk.
# ---------------------------------------------------------------------------


def safe_write_text(path: Path, text: str) -> bool:
    """Atomic write of arbitrary text. Returns True when persisted."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp") if path.suffix else path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(text)
            try:
                f.flush()
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        return True
    except OSError as e:
        sys.stderr.write(f"[cycle] safe_write_text failed for {path}: {e}\n")
        try:
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(
                    f"[{utc_now_iso()}] safe_write_text failed for "
                    f"{path}: {e}\n"
                )
        except OSError:
            pass
        return False


def safe_write_json(path: Path, data: dict | list) -> bool:
    """Atomic JSON write with stable indent + utf-8."""
    try:
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    except (TypeError, ValueError) as e:
        sys.stderr.write(
            f"[cycle] safe_write_json could not serialise {path}: {e}\n"
        )
        return False
    return safe_write_text(path, payload)


# Stampport artifact metadata.
#
# Each markdown artifact we write under .runtime/ gets a small HTML
# comment header + YAML-ish front-matter block so downstream tools
# (cycle.py readers, Pipeline Recovery file-presence checks, the
# dashboard preview) can answer "which cycle / stage / agent produced
# this file" without grepping the body. HTML comments are ignored by
# every Markdown renderer we care about.
#
# The metadata is intentionally tiny — bigger blocks would interfere
# with the existing _extract_md_section() readers.
def _artifact_header(
    *,
    cycle_id: int | None,
    stage: str,
    source_agent: str,
    extra: dict | None = None,
    run_id: str | None = None,
    feature_id: str | None = None,
) -> str:
    """Compose the metadata block. `run_id` is sourced via
    `_resolve_run_id()` when not passed, so every artifact written by
    cycle.py / autopilot inherits the active run's identifier
    automatically. Pass an explicit value when re-writing a stale
    artifact under a different run."""
    rid = run_id if run_id is not None else _resolve_run_id()
    fields = [
        f"cycle_id: {cycle_id if cycle_id is not None else '—'}",
        f"run_id: {rid or '—'}",
        f"stage: {stage}",
        f"source_agent: {source_agent}",
        f"created_at: {utc_now_iso()}",
    ]
    if feature_id:
        fields.append(f"feature_id: {feature_id}")
    if extra:
        for k, v in extra.items():
            fields.append(f"{k}: {v}")
    inner = "\n".join(fields)
    return f"<!--\nstampport_artifact\n{inner}\n-->\n\n"


def safe_write_artifact(
    path: Path,
    body: str,
    *,
    cycle_id: int | None,
    stage: str,
    source_agent: str,
    extra: dict | None = None,
    run_id: str | None = None,
    feature_id: str | None = None,
) -> bool:
    """Atomic write of a markdown artifact with a metadata header.

    `run_id` and `feature_id` get embedded in the header so any
    consumer (PM, smoke, observer, dashboard) can prove the artifact
    belongs to the current factory run + feature without re-deriving.
    """
    header = _artifact_header(
        cycle_id=cycle_id, stage=stage, source_agent=source_agent,
        extra=extra, run_id=run_id, feature_id=feature_id,
    )
    payload = header + (body if body.endswith("\n") else body + "\n")
    return safe_write_text(path, payload)


def _move_stale_artifact_aside(path: Path) -> str | None:
    """Rename `path` → `path.prev` so the operator can still inspect the
    previous version, but the live filename no longer reads as the
    current cycle's output.

    Returns the new path string when something moved, or None when the
    file was absent / move failed.
    """
    if not path.exists():
        return None
    backup = path.with_suffix(path.suffix + ".prev")
    try:
        if backup.exists():
            backup.unlink()
        path.rename(backup)
        return str(backup)
    except OSError:
        return None


def _write_state(state: CycleState) -> None:
    state.updated_at = utc_now_iso()
    safe_write_json(STATE_FILE, state.to_dict())


def _log(line: str) -> None:
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{utc_now_iso()}] {line}\n")
    except OSError as e:
        sys.stderr.write(f"[cycle] _log failed: {e}\n")


# Cap the in-state cycle_log so the heartbeat payload doesn't grow
# unbounded over a long-lived factory loop. Older entries fall off
# the array but stay in factory_last_report.md / local_factory.log.
CYCLE_LOG_CAP = 60


def _emit_cycle_log(state: "CycleState", kind: str, message: str, **payload) -> None:
    """Append one structured log line to state.cycle_log. The dashboard
    derives synthetic System Log events from this list so the operator
    sees per-cycle markers (claude_apply_started / claude_apply_changed_files
    / validation_passed / cycle_produced_code_change / ...) without us
    having to extend the API event_bus.

    `kind` should match the keys the FE eventClassifier expects (kept
    in lowercase_snake_case so the keyword classifier still picks them
    up via the human-readable `message`)."""
    entry = {
        "at": utc_now_iso(),
        "cycle": int(state.cycle or 0),
        "kind": kind,
        "message": message,
    }
    if payload:
        entry["payload"] = payload
    state.cycle_log.append(entry)
    if len(state.cycle_log) > CYCLE_LOG_CAP:
        state.cycle_log = state.cycle_log[-CYCLE_LOG_CAP:]
    # Mirror to local_factory.log so the runner's log_tail also reflects
    # the structured marker — handy when grepping the log file.
    _log(f"[cycle_log] {kind}: {message}")


def _suggest_action_for_stage(stage_name: str) -> str:
    """Map a failed stage name onto a short Korean next-step hint
    surfaced in the dashboard's "이번 사이클 실제 변경" / failed_reason
    block. Generic fallback when the stage isn't in the table."""
    table = {
        "build_app":               "app/web 디렉터리에서 npm run build 직접 실행 후 오류 메시지 확인",
        "build_control":           "control_tower/web 디렉터리에서 npm run build 직접 실행 후 오류 메시지 확인",
        "syntax_check":            "py_compile 실패 — 변경 파일을 직접 점검하고 import/들여쓰기 오류 확인",
        "git_check":               "working tree 의 conflict marker / unmerged 파일 정리 후 재시도",
        "publish_blocker_resolve": "blocker_resolve_report.md 확인 후 hard_risky/secret 파일 처리",
        "claude_propose":          "claude CLI 설치/CLAUDE_BIN/예산 환경변수 확인 후 재시도",
        "implementation_ticket":   "PM 결정 / claude 제안에 수정 대상 파일이 명시됐는지 확인 — 비어 있으면 planner/proposal 다시 작성",
        "claude_apply":            "claude_apply.diff 확인 후 사람이 수동으로 적용 또는 롤백",
        "qa_gate":                 "qa_feedback.md 확인 — 실패 카테고리별 파일 수정 후 재시도",
        "qa_recheck":              "qa_feedback.md 확인 후 다시 cycle 실행",
        "product_planning":        "config/domain_profiles/stampport.json 의 PM 입력값 점검",
    }
    return table.get(
        stage_name,
        f"실패 단계({stage_name}) 로그 확인 후 운영자가 직접 판단",
    )


def _read_goal() -> str:
    if GOAL_FILE.exists():
        text = GOAL_FILE.read_text(encoding="utf-8").strip()
        if text:
            return text
    return DEFAULT_GOAL


# ---------------------------------------------------------------------------
# Subprocess helper (capped time, no shell, no user-controlled argv)
# ---------------------------------------------------------------------------


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 180.0,
    env_override: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Run `argv` with a fixed list (never a shell string). Return
    (ok, captured_output)."""
    env = os.environ.copy()
    if env_override:
        env.update(env_override)
    try:
        r = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        out = (r.stdout or "") + (("\n--stderr--\n" + r.stderr) if r.stderr else "")
        return (r.returncode == 0), out.strip()
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s: {' '.join(shlex.quote(a) for a in argv)}"
    except FileNotFoundError as e:
        return False, f"missing tool: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"error: {e}"


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def stage_git_check(state: CycleState) -> StageResult:
    label = next(lab for n, lab, _ in STAGES if n == "git_check")
    sr = StageResult(name="git_check", label=label, status="running")
    t0 = time.time()
    ok, out = _run(["git", "-C", str(REPO_ROOT), "status", "--short"], timeout=15)
    sr.duration_sec = round(time.time() - t0, 3)

    if not ok:
        sr.status = "failed"
        sr.message = "git status 실행 실패"
        sr.detail = out[-1500:]
        return sr

    risky: list[str] = []
    for raw_line in out.splitlines():
        # Format: "XY <path>"  (X=index, Y=worktree).
        # Renames look like "R  old -> new".
        path = raw_line[3:].strip() if len(raw_line) > 3 else raw_line
        if "->" in path:
            path = path.split("->", 1)[1].strip()
        if not path:
            continue
        for pat in RISKY_PATTERNS:
            if pat in path:
                risky.append(path)
                break
    state.risky_files = sorted(set(risky))

    sr.status = "passed"
    sr.message = f"변경 파일 {len(out.splitlines())}건"
    if state.risky_files:
        sr.message += f", 위험 파일 {len(state.risky_files)}건 감지"
    sr.detail = out[-1500:]
    return sr


def stage_web_build(state: CycleState, *, web_dir: Path, name: str) -> StageResult:
    label = next(lab for n, lab, _ in STAGES if n == name)
    sr = StageResult(name=name, label=label, status="running")
    t0 = time.time()

    if not web_dir.is_dir():
        sr.status = "skipped"
        sr.message = f"{web_dir} 없음 — 스킵"
        sr.duration_sec = round(time.time() - t0, 3)
        return sr

    npm = shutil.which("npm")
    if not npm:
        sr.status = "failed"
        sr.message = "npm 미설치"
        sr.duration_sec = round(time.time() - t0, 3)
        return sr

    ok, out = _run(
        [npm, "run", "build"],
        cwd=web_dir,
        timeout=300.0,
        env_override={"CI": "1"},
    )
    sr.duration_sec = round(time.time() - t0, 3)
    sr.status = "passed" if ok else "failed"
    sr.message = "빌드 성공" if ok else "빌드 실패"
    sr.detail = out[-1800:]
    return sr


def _collect_py_files(roots: list[Path], skip_dirs: set[str]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            parts = set(p.parts)
            if parts & skip_dirs:
                continue
            files.append(p)
    return files


def stage_syntax_check(state: CycleState) -> StageResult:
    label = next(lab for n, lab, _ in STAGES if n == "syntax_check")
    sr = StageResult(name="syntax_check", label=label, status="running")
    t0 = time.time()

    skip = {".venv", "venv", "__pycache__", "node_modules", "dist", "build", ".runtime"}

    py_roots = [
        REPO_ROOT / "app" / "api",
        REPO_ROOT / "control_tower" / "api",
        REPO_ROOT / "control_tower" / "local_runner",
    ]
    py_files = _collect_py_files(py_roots, skip)

    # Prefer the control_tower venv's python (3.11) so app/api newer
    # syntax compiles. Fall back to the current interpreter otherwise.
    venv_py = REPO_ROOT / "control_tower" / "api" / ".venv" / "bin" / "python"
    py_bin = str(venv_py) if venv_py.is_file() else sys.executable

    py_failures: list[tuple[Path, str]] = []
    for f in py_files:
        ok, out = _run([py_bin, "-m", "py_compile", str(f)], timeout=20)
        if not ok:
            py_failures.append((f, out))

    shell_files = [
        REPO_ROOT / "scripts" / "local_factory_start.sh",
        REPO_ROOT / "scripts" / "local_factory_stop.sh",
        REPO_ROOT / "scripts" / "local_factory_status.sh",
    ]
    shell_failures: list[tuple[Path, str]] = []
    for f in shell_files:
        if not f.is_file():
            continue
        ok, out = _run(["bash", "-n", str(f)], timeout=10)
        if not ok:
            shell_failures.append((f, out))

    sr.duration_sec = round(time.time() - t0, 3)
    failure_count = len(py_failures) + len(shell_failures)
    if failure_count == 0:
        sr.status = "passed"
        sr.message = (
            f"Python {len(py_files)}개 / shell {len(shell_files)}개 — 모두 통과"
        )
        sr.detail = ""
    else:
        sr.status = "failed"
        sr.message = (
            f"문법 오류 {failure_count}건 (Python {len(py_failures)} / shell {len(shell_failures)})"
        )
        details: list[str] = []
        for path, msg in py_failures[:8]:
            details.append(f"- {path.relative_to(REPO_ROOT)}\n{msg[:600]}")
        for path, msg in shell_failures[:8]:
            details.append(f"- {path.relative_to(REPO_ROOT)}\n{msg[:600]}")
        sr.detail = "\n\n".join(details)[-1800:]

    return sr


# ---------------------------------------------------------------------------
# Publish blocker stages
#
# stage_publish_blocker_check: scan `git status` for files that the
#   publish step would refuse, and split them into auto-resolvable vs
#   manual-resolution buckets.
#
# stage_publish_blocker_resolve: actually clean up the auto-resolvable
#   bucket (git restore for tracked artifacts, rm -rf / unlink for
#   generated junk). Re-checks afterwards — anything still left wins
#   the cycle a "manual_required" verdict and forces all downstream
#   development stages to skip.
# ---------------------------------------------------------------------------


def _classify_publish_blocker(path: str) -> str:
    """Return one of: 'hard_risky' | 'manual_required' | 'auto_restore'
    | 'auto_delete' | 'allowed_code'.

    NOTE: 'manual_required' is now a *warning* category — it does not
    block publish. Only 'hard_risky' (secret patterns) blocks. The
    bucket name is preserved for back-compat with state.json consumers.

    Verdict precedence (top → bottom, first match wins):

      1. hard_risky  — secret/credential pattern. NEVER read content.
      2. manual_required — package.json / requirements / deploy / .github / nginx / systemd.
                         (warning-only, surfaced for human eyeball)
      3. auto_restore — exact match in PUBLISH_AUTO_RESTORE_FILES.
      4. auto_delete — substring match in PUBLISH_AUTO_DELETE_PATTERNS.
      5. allowed_code — under one of PUBLISH_ALLOWED_CODE_PREFIXES.
      6. allowed_code (fallback) — anything else (top-level docs, README
         tweaks, CLAUDE.md, etc.). The publish step still runs the
         secret-scan + build/health gates, so an unknown path is no
         longer a blocker — just a regular change that rides along.
    """
    if not path:
        return "allowed_code"

    # 1. Hard-risky wins — even an `.env` under app/api/.
    for pat in PUBLISH_HARD_RISKY_PATTERNS:
        if pat in path:
            return "hard_risky"

    # 2. Build/CI/infra config — manual *warning*.
    for pat in PUBLISH_MANUAL_PATTERNS:
        if pat in path:
            return "manual_required"
    for root in PUBLISH_MANUAL_ROOTS:
        if path.startswith(root):
            return "manual_required"

    # 3. Auto-restore exact-match.
    if path in PUBLISH_AUTO_RESTORE_FILES:
        return "auto_restore"

    # 4. Auto-delete cache/junk.
    for pat in PUBLISH_AUTO_DELETE_PATTERNS:
        if pat in path:
            return "auto_delete"

    # 5. Source code under a known allowed prefix.
    for prefix in PUBLISH_ALLOWED_CODE_PREFIXES:
        if path.startswith(prefix):
            return "allowed_code"

    # 6. Liberal default — anything else (top-level CHANGELOG.md,
    # README.md, CLAUDE.md, an ad-hoc note) is treated as ordinary
    # publishable code. The secret/conflict/build/health gates
    # downstream catch the actually unsafe stuff.
    return "allowed_code"


# ---------------------------------------------------------------------------
# Warning classifier — non-blocking signals worth surfacing.
#
# These categories are about "주의 깊게 보면 좋은" diffs, not blockers.
# They feed `state.warning_reasons` so the report and dashboard can
# render the new "Release Safety Gate: passed with warnings" message
# with concrete reasons instead of a generic "위험 파일 변경 감지".
# ---------------------------------------------------------------------------


# Diff-volume thresholds for "many files" / "large diff" warnings.
WARNING_MANY_FILES_THRESHOLD = 25
WARNING_BIG_FE_FILES_THRESHOLD = 12


def _classify_warning_reasons(paths: list[str]) -> list[str]:
    """Return human-readable warning reasons (Korean) describing the
    *kind* of change in `paths`. Pure presentation: callers MUST NOT
    use these to block publish. Empty list = no warnings.

    The categories mirror the user-facing spec for "주의 깊게 보면
    좋은" signals (cycle.py 변경, runner.py 변경, deploy script 변경,
    nginx template 변경, 큰 diff 등).
    """
    reasons: list[str] = []
    if not paths:
        return reasons

    has = lambda needle: any(needle in p for p in paths)  # noqa: E731

    if has("control_tower/local_runner/cycle.py"):
        reasons.append("cycle.py 변경됨")
    if has("control_tower/local_runner/runner.py"):
        reasons.append("runner.py 변경됨")
    deploy_script_hits = [
        p for p in paths
        if p.startswith("scripts/server_")
        or p.startswith("scripts/deploy_")
        or p.startswith("deploy/")
    ]
    if deploy_script_hits:
        reasons.append("deploy script 변경됨")
    nginx_hits = [p for p in paths if "nginx" in p]
    if nginx_hits:
        reasons.append("nginx template 변경됨")
    api_contract_hits = [
        p for p in paths
        if p.startswith("app/api/")
        and (p.endswith("schemas.py") or "/schemas/" in p or p.endswith("main.py"))
    ]
    if api_contract_hits:
        reasons.append("API 계약 변경 추정")
    if len(paths) >= WARNING_MANY_FILES_THRESHOLD:
        reasons.append(f"많은 파일 변경 ({len(paths)}건)")
    fe_app = [p for p in paths if p.startswith("app/web/src/")]
    if len(fe_app) >= WARNING_BIG_FE_FILES_THRESHOLD:
        reasons.append(f"app/web/src 대규모 변경 ({len(fe_app)}건)")
    fe_ct = [p for p in paths if p.startswith("control_tower/web/src/")]
    if len(fe_ct) >= WARNING_BIG_FE_FILES_THRESHOLD:
        reasons.append(f"control_tower/web/src 대규모 변경 ({len(fe_ct)}건)")
    return reasons


def _scan_conflict_markers(paths: list[str]) -> list[str]:
    """Return the subset of `paths` that contain a git conflict marker.

    We only open files under ALLOWED_APPLY_DIRS with a CONFLICT_SCAN_TEXT_EXTS
    extension, capped at CONFLICT_SCAN_MAX_BYTES. Hard-risky paths are
    NEVER opened — the caller is responsible for filtering them out
    before passing to us.
    """
    hits: list[str] = []
    for rel in paths:
        if not any(rel.startswith(d) for d in ALLOWED_APPLY_DIRS):
            continue
        # Hard-risky always wins — never read.
        if any(pat in rel for pat in PUBLISH_HARD_RISKY_PATTERNS):
            continue
        if not rel.lower().endswith(CONFLICT_SCAN_TEXT_EXTS):
            continue
        full = REPO_ROOT / rel
        try:
            if not full.is_file():
                continue
            if full.stat().st_size > CONFLICT_SCAN_MAX_BYTES:
                continue
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Require all three markers to appear on their own lines so a
        # source file describing the syntax (this very file, for
        # instance) does not produce a false positive.
        marker_lines = {
            line for line in text.splitlines()
            if line.startswith(("<<<<<<< ", "<<<<<<<\t", "<<<<<<<"))
            and line.startswith("<<<<<<<") and len(line) >= 7
        }
        has_lt = any(line == "<<<<<<<" or line.startswith("<<<<<<< ") for line in text.splitlines())
        has_eq = any(line == "=======" for line in text.splitlines())
        has_gt = any(line == ">>>>>>>" or line.startswith(">>>>>>> ") for line in text.splitlines())
        del marker_lines  # only used for clarity above
        if has_lt and has_eq and has_gt:
            hits.append(rel)
    return sorted(set(hits))


def _safe_basename(path: str) -> str:
    """Return just the basename of a path — used when logging hard_risky
    files so we never echo the full directory traversal that contains a
    secret. The caller is still responsible for not opening the file."""
    if not path:
        return "<empty>"
    return path.rsplit("/", 1)[-1] or path


def _read_blocker_recurring() -> dict:
    if not BLOCKER_RECURRING_FILE.is_file():
        return {}
    try:
        return json.loads(BLOCKER_RECURRING_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_blocker_recurring(data: dict) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    try:
        BLOCKER_RECURRING_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _bump_recurring_counters(
    *, restored: list[str], deleted: list[str], manual: list[str], hard_risky_basenames: list[str]
) -> dict:
    """Bump the per-file appearance counter and return the updated dict.

    We track auto-resolved files under their full path, and hard_risky
    files under their basename only (so the counter file itself never
    contains a directory hint that would help an attacker locate the
    secret).
    """
    cur = _read_blocker_recurring()
    now = utc_now_iso()
    for p in restored:
        e = cur.setdefault(p, {"count": 0})
        e["count"] = int(e.get("count", 0)) + 1
        e["last_seen_at"] = now
        e["resolution"] = "auto_restore"
    for p in deleted:
        e = cur.setdefault(p, {"count": 0})
        e["count"] = int(e.get("count", 0)) + 1
        e["last_seen_at"] = now
        e["resolution"] = "auto_delete"
    for p in manual:
        e = cur.setdefault(p, {"count": 0})
        e["count"] = int(e.get("count", 0)) + 1
        e["last_seen_at"] = now
        e["resolution"] = "manual_required"
    for bn in hard_risky_basenames:
        # Stored under "<hard_risky>:basename" so we don't collide with
        # a hypothetical normal file of the same name.
        key = f"<hard_risky>:{bn}"
        e = cur.setdefault(key, {"count": 0})
        e["count"] = int(e.get("count", 0)) + 1
        e["last_seen_at"] = now
        e["resolution"] = "hard_risky"
    _save_blocker_recurring(cur)
    return cur


def _proactive_clean_recurring() -> tuple[list[str], list[str]]:
    """Before scanning the working tree, proactively re-run the same
    auto-resolution we'd do anyway on files we've cleaned >= 3 times in
    the past. This converts the "1 hour later, same .claude/settings
    drift" pattern into a no-op start-of-cycle cleanup that the rest
    of the stages don't even see.

    Returns (restored, deleted) — both lists of paths actually acted on.
    """
    cur = _read_blocker_recurring()
    if not cur:
        return [], []
    threshold = int(os.environ.get("FACTORY_BLOCKER_PROACTIVE_THRESHOLD", "3") or "3")
    restored: list[str] = []
    deleted: list[str] = []
    for path, entry in cur.items():
        if not isinstance(entry, dict):
            continue
        if path.startswith("<hard_risky>"):
            # Never auto-act on hard-risky entries — that's the human's call.
            continue
        if int(entry.get("count", 0)) < threshold:
            continue
        resolution = entry.get("resolution")
        full = REPO_ROOT / path
        if resolution == "auto_restore":
            ok, _ = _run(
                ["git", "-C", str(REPO_ROOT), "restore", "--", path],
                timeout=20,
            )
            if ok:
                restored.append(path)
        elif resolution == "auto_delete":
            try:
                if full.is_dir():
                    shutil.rmtree(full)
                    deleted.append(path)
                elif full.is_file() or full.is_symlink():
                    full.unlink()
                    deleted.append(path)
            except OSError:
                pass
    return restored, deleted


def _save_blocker_state(state: CycleState) -> None:
    """Write .runtime/blocker_state.json with the current 5-bucket
    snapshot. The dashboard heartbeat reads this directly so the UI
    can decide what chips to render without re-deriving the policy.
    """
    RUNTIME.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": state.publish_blocker_status,
        "auto_restored": list(state.auto_restored_files),
        "auto_deleted": list(state.auto_deleted_files),
        "allowed_code": list(state.allowed_code_files),
        "manual_required": list(state.manual_required_files),
        # Hard-risky paths are stored as basenames only — never the
        # full path. A secret leak via state file is exactly the
        # category of mistake this whole stage exists to prevent.
        "hard_risky": [_safe_basename(p) for p in state.hard_risky_files],
        "conflict_markers": list(state.conflict_marker_files),
        "warning_reasons": list(state.warning_reasons),
        "blocked_reason": state.publish_blocker_message,
        "updated_at": utc_now_iso(),
    }
    try:
        BLOCKER_STATE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _write_blocker_resolve_report(state: CycleState, *, recurring: dict) -> None:
    """Write the human-readable post-mortem. Hard-risky entries appear
    only as basenames; full paths are intentionally redacted."""
    lines: list[str] = [
        "# Publish Blocker — Resolve Report",
        "",
        f"_사이클 #{state.cycle} · {utc_now_iso()}_",
        "",
        "## 최종 상태",
        f"- status: {state.publish_blocker_status}",
        f"- publish_blocked: {'예' if state.publish_blocked else '아니오'}",
        f"- 메시지: {state.publish_blocker_message or '(없음)'}",
        "",
        "## 자동 복구 파일 (git restore)",
    ]
    if state.auto_restored_files:
        for p in state.auto_restored_files:
            lines.append(f"- `{p}`")
    else:
        lines.append("- (없음)")
    lines += ["", "## 자동 삭제 파일"]
    if state.auto_deleted_files:
        for p in state.auto_deleted_files:
            lines.append(f"- `{p}`")
    else:
        lines.append("- (없음)")
    lines += ["", "## 정상 코드 변경"]
    if state.allowed_code_files:
        for p in state.allowed_code_files[:50]:
            lines.append(f"- `{p}`")
        if len(state.allowed_code_files) > 50:
            lines.append(f"- … 외 {len(state.allowed_code_files) - 50}건")
    else:
        lines.append("- (없음)")
    lines += ["", "## Warning — 주의 깊게 보면 좋은 변경"]
    if state.warning_reasons:
        for r in state.warning_reasons:
            lines.append(f"- {r}")
    else:
        lines.append("- (없음)")
    if state.manual_required_files:
        lines.append("")
        lines.append("관련 파일:")
        for p in state.manual_required_files[:20]:
            lines.append(f"- `{p}`")
    lines += ["", "## 위험 파일 (hard_risky · 차단)"]
    if state.hard_risky_files:
        # Basenames only. The ".env" → ".env" mapping is intentional;
        # a "deploy/secrets/foo.env" → "foo.env" mapping is the point.
        for p in state.hard_risky_files:
            lines.append(f"- `{_safe_basename(p)}` (전체 경로 미노출)")
    else:
        lines.append("- (없음)")
    lines += ["", "## Conflict marker (차단)"]
    if state.conflict_marker_files:
        for p in state.conflict_marker_files:
            lines.append(f"- `{p}`")
    else:
        lines.append("- (없음)")

    lines += ["", "## 반복 발생"]
    counters = recurring or {}
    if counters:
        # Show top 5 by count, mask hard_risky entries to basenames only.
        items = sorted(
            counters.items(), key=lambda kv: int(kv[1].get("count", 0)), reverse=True
        )
        for key, entry in items[:5]:
            label = key
            if key.startswith("<hard_risky>:"):
                label = f"<hard_risky basename={key.split(':',1)[1]}>"
            lines.append(
                f"- {label} · {int(entry.get('count', 0))}회 · "
                f"resolution={entry.get('resolution', '?')}"
            )
    else:
        lines.append("- (없음)")

    lines += ["", "## 다음 조치"]
    if state.publish_blocker_status == "blocked":
        if state.hard_risky_files:
            lines.append(
                "- 위험 파일이 감지됨. .gitignore 확인 + 파일을 워킹 트리에서 제거하세요."
            )
        if state.conflict_marker_files:
            lines.append(
                "- Git conflict marker가 남아 있는 파일이 있습니다. 충돌을 해소한 뒤 다시 시도하세요."
            )
        lines.append(
            "- 위 차단 항목을 정리한 뒤 다음 사이클을 시작하세요. (build/health/secret 게이트는 통과해야 합니다.)"
        )
    elif state.publish_blocker_status == "warning":
        lines.append(
            "- Release Safety Gate: passed with warnings. build/health/secret 게이트가 통과하면 배포가 허용됩니다."
        )
        for r in state.warning_reasons[:5]:
            lines.append(f"  - 사유: {r}")
        lines.append("  - 결과: build/health 통과로 배포 허용 (변경은 다음 사이클에서 점검 가능).")
    elif state.publish_blocker_status == "resolved":
        lines.append(
            "- 자동 복구로 임시 드리프트가 해소되었습니다. publish_changes 가능 (QA Gate 통과 시)."
        )
    else:
        lines.append("- 차단 파일 없음. publish_changes 가능 (QA Gate 통과 시).")

    try:
        BLOCKER_REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        state.publish_blocker_report_path = str(BLOCKER_REPORT_FILE)
    except OSError:
        state.publish_blocker_report_path = None


def _parse_git_status_porcelain(out: str) -> list[tuple[str, str]]:
    """Parse `git status --porcelain` output into [(status_code, path), ...].

    Porcelain format is `XY<space><path>`. Each of X/Y is one char and
    can itself be a literal space (e.g. " M file" means worktree
    modified, index unchanged). The caller may already have called
    str.strip() on the whole multi-line output — that *eats the leading
    space of the first line*, leaving us with a line whose first byte
    is the worktree status, not the index status. So we need a
    tolerant parser:

      * If line starts with a leading status char and length is at
        least 4 with a space at index 2, treat as full XY format.
      * Otherwise (line like "M file"), treat the leading char as
        worktree status and pad index status with a space.
    """
    rows: list[tuple[str, str]] = []
    for raw in out.splitlines():
        if not raw:
            continue
        line = raw  # never strip — whitespace IS data here
        if len(line) >= 4 and line[2] == " ":
            code = line[:2]
            rest = line[3:]
        elif len(line) >= 3 and line[1] == " ":
            # str.strip() ate the leading status char.
            code = " " + line[0]
            rest = line[2:]
        else:
            continue
        rest = rest.strip()
        if "->" in rest:
            rest = rest.split("->", 1)[1].strip()
        if rest:
            rows.append((code, rest))
    return rows


def _git_status_porcelain_raw() -> tuple[bool, str]:
    """Like `_run(["git", "status", "--porcelain"])` but DOES NOT
    `.strip()` the output. The leading space in lines like ' M file'
    is significant — strip() would silently corrupt it."""
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False, "git status timeout"
    except FileNotFoundError as e:
        return False, f"git missing: {e}"
    if r.returncode != 0:
        return False, (r.stderr or "")[-300:]
    return True, r.stdout or ""


def _scan_and_classify_working_tree(state: CycleState) -> tuple[bool, str]:
    """Run `git status --porcelain` and split the result into the 5
    blocker buckets, populating the corresponding lists on `state`.

    Returns (ok, raw_porcelain_output) so the caller can stash the
    raw output for debugging/reporting.
    """
    ok, out = _git_status_porcelain_raw()
    if not ok:
        state.auto_restored_files = []
        state.auto_deleted_files = []
        state.allowed_code_files = []
        state.manual_required_files = []
        state.hard_risky_files = []
        state.auto_resolved_files = []
        return False, out

    auto_restore: list[str] = []
    auto_delete: list[str] = []
    allowed: list[str] = []
    manual: list[str] = []
    risky: list[str] = []
    for _code, path in _parse_git_status_porcelain(out):
        v = _classify_publish_blocker(path)
        if v == "hard_risky":
            risky.append(path)
        elif v == "manual_required":
            manual.append(path)
        elif v == "auto_restore":
            auto_restore.append(path)
        elif v == "auto_delete":
            auto_delete.append(path)
        elif v == "allowed_code":
            allowed.append(path)

    state.auto_restored_files = sorted(set(auto_restore))
    state.auto_deleted_files = sorted(set(auto_delete))
    state.allowed_code_files = sorted(set(allowed))
    state.manual_required_files = sorted(set(manual))
    state.hard_risky_files = sorted(set(risky))
    # Back-compat: pre-existing dashboards read auto_resolved_files.
    state.auto_resolved_files = sorted(
        set(state.auto_restored_files + state.auto_deleted_files)
    )
    return True, out


def stage_publish_blocker_check(state: CycleState) -> StageResult:
    """Detect files that would block a publish and split them into the
    5-bucket classification. Also runs a *proactive* sweep of files
    we've cleaned ≥ 3 times before, so the same `.claude/settings.local.json`
    drift doesn't burn a stage every cycle."""
    label = next(lab for n, lab, _ in STAGES if n == "publish_blocker_check")
    sr = StageResult(name="publish_blocker_check", label=label, status="running")
    t0 = time.time()

    # 1. Proactive sweep — run BEFORE the scan so the working tree we
    # classify is already as-clean-as-possible. This converts
    # "settings.local.json drifted again" from a stage into a no-op.
    proactive_restored, proactive_deleted = _proactive_clean_recurring()
    if proactive_restored or proactive_deleted:
        _log(
            f"blocker_proactive cleaned: restored={len(proactive_restored)} "
            f"deleted={len(proactive_deleted)}"
        )

    # 2. Scan + classify the (now-cleaner) working tree.
    ok, out = _scan_and_classify_working_tree(state)
    sr.duration_sec = round(time.time() - t0, 3)
    if not ok:
        sr.status = "failed"
        sr.message = "git status 실행 실패"
        sr.detail = out[-1500:]
        state.publish_blocked = True
        state.publish_blocker_status = "blocked"
        state.publish_blocker_message = "git status 실행 실패 — 수동 확인 필요"
        _save_blocker_state(state)
        return sr

    # Stash anything we already auto-cleaned proactively under the
    # auto_restored/auto_deleted lists so the report shows them.
    if proactive_restored:
        state.auto_restored_files = sorted(
            set(state.auto_restored_files + proactive_restored)
        )
    if proactive_deleted:
        state.auto_deleted_files = sorted(
            set(state.auto_deleted_files + proactive_deleted)
        )

    # Conflict-marker scan over allowed_code + manual_required (the
    # buckets we're about to consider for publish). Hard-risky paths
    # are *never* opened — already filtered by the helper.
    scan_paths = list(state.allowed_code_files) + list(state.manual_required_files)
    state.conflict_marker_files = _scan_conflict_markers(scan_paths)

    # Warning reasons cover the "주의 깊게 보면 좋은" categories.
    state.warning_reasons = _classify_warning_reasons(
        state.allowed_code_files + state.manual_required_files
    )

    has_real_blocker = bool(state.hard_risky_files or state.conflict_marker_files)
    has_warning = bool(state.manual_required_files or state.warning_reasons)
    has_auto_candidates = bool(
        state.auto_restored_files or state.auto_deleted_files
    )

    if not (has_real_blocker or has_warning or has_auto_candidates or state.allowed_code_files):
        state.publish_blocked = False
        state.publish_blocker_status = "clean"
        state.publish_blocker_message = "변경 없음 · Release Safety Gate clean"
        _save_blocker_state(state)
        sr.status = "passed"
        sr.message = "변경 없음 · Release Safety Gate clean"
        return sr

    # Provisional verdict — resolve stage flips it after cleanup.
    if has_real_blocker:
        state.publish_blocked = True
        state.publish_blocker_status = "blocked"
    elif has_auto_candidates:
        # Drift to clean up. Resolve stage will flip to 'resolved' or
        # 'warning' depending on what's left.
        state.publish_blocked = False
        state.publish_blocker_status = "blocked"  # provisional; resolve sets final
    elif has_warning:
        state.publish_blocked = False
        state.publish_blocker_status = "warning"
    else:
        # only allowed_code — pure clean.
        state.publish_blocked = False
        state.publish_blocker_status = "clean"

    parts: list[str] = []
    if state.auto_restored_files:
        parts.append(f"자동 복구 {len(state.auto_restored_files)}건")
    if state.auto_deleted_files:
        parts.append(f"자동 삭제 {len(state.auto_deleted_files)}건")
    if state.allowed_code_files:
        parts.append(f"정상 코드 {len(state.allowed_code_files)}건")
    if state.manual_required_files:
        parts.append(f"warning {len(state.manual_required_files)}건")
    if state.conflict_marker_files:
        parts.append(f"conflict marker {len(state.conflict_marker_files)}건")
    if state.hard_risky_files:
        parts.append(f"위험 {len(state.hard_risky_files)}건")
    state.publish_blocker_message = "변경 분류: " + ", ".join(parts)

    _save_blocker_state(state)
    sr.status = "passed"
    sr.message = state.publish_blocker_message
    detail_lines: list[str] = []
    if state.auto_restored_files:
        detail_lines.append("[auto_restore]")
        detail_lines += [f"- {p}" for p in state.auto_restored_files[:20]]
    if state.auto_deleted_files:
        if detail_lines: detail_lines.append("")
        detail_lines.append("[auto_delete]")
        detail_lines += [f"- {p}" for p in state.auto_deleted_files[:20]]
    if state.allowed_code_files:
        if detail_lines: detail_lines.append("")
        detail_lines.append("[allowed_code]")
        detail_lines += [f"- {p}" for p in state.allowed_code_files[:20]]
    if state.manual_required_files:
        if detail_lines: detail_lines.append("")
        detail_lines.append("[warning · manual_required]")
        detail_lines += [f"- {p}" for p in state.manual_required_files[:20]]
    if state.warning_reasons:
        if detail_lines: detail_lines.append("")
        detail_lines.append("[warning reasons]")
        detail_lines += [f"- {r}" for r in state.warning_reasons[:10]]
    if state.conflict_marker_files:
        if detail_lines: detail_lines.append("")
        detail_lines.append("[conflict_marker · 차단]")
        detail_lines += [f"- {p}" for p in state.conflict_marker_files[:20]]
    if state.hard_risky_files:
        if detail_lines: detail_lines.append("")
        detail_lines.append("[hard_risky · 차단] (basenames only)")
        detail_lines += [f"- {_safe_basename(p)}" for p in state.hard_risky_files[:20]]
    sr.detail = "\n".join(detail_lines)[-1800:]
    return sr


def stage_publish_blocker_resolve(state: CycleState) -> StageResult:
    """Run the auto-resolution actions for the buckets identified by
    publish_blocker_check, then re-classify to verify nothing leaked
    through. Always writes blocker_state.json + blocker_resolve_report.md
    so the dashboard / human reviewer have a single source of truth."""
    label = next(lab for n, lab, _ in STAGES if n == "publish_blocker_resolve")
    sr = StageResult(name="publish_blocker_resolve", label=label, status="running")
    t0 = time.time()

    initial_restore = list(state.auto_restored_files)
    initial_delete = list(state.auto_deleted_files)
    pre_manual = list(state.manual_required_files)
    pre_risky = list(state.hard_risky_files)
    allowed = list(state.allowed_code_files)

    actually_restored: list[str] = []
    actually_deleted: list[str] = []
    failed: list[tuple[str, str]] = []  # (path, reason) — path may be a basename for risky

    # 1. git restore the auto-restore set (in one call when possible).
    if initial_restore:
        ok, out = _run(
            ["git", "-C", str(REPO_ROOT), "restore", "--", *initial_restore],
            timeout=30,
        )
        if ok:
            actually_restored.extend(initial_restore)
        else:
            # Fall back to per-file restore so we know which one failed.
            for p in initial_restore:
                ok2, out2 = _run(
                    ["git", "-C", str(REPO_ROOT), "restore", "--", p], timeout=15,
                )
                if ok2:
                    actually_restored.append(p)
                else:
                    failed.append((p, (out2 or out)[-200:] or "git restore 실패"))

    # 2. Delete cache/junk paths.
    for path in initial_delete:
        full = REPO_ROOT / path
        try:
            if full.is_dir():
                shutil.rmtree(full)
                actually_deleted.append(path)
            elif full.is_file() or full.is_symlink():
                full.unlink()
                actually_deleted.append(path)
            else:
                # The git-status entry might be a parent dir glob like
                # `app/web/dist/`. If the precise child no longer
                # exists, count as already-clean.
                actually_deleted.append(path)
        except OSError as e:
            failed.append((path, str(e)))

    # 3. Re-classify after the cleanup. This catches leaks (e.g. the
    # file was modified again between scan and resolve).
    ok2, out2 = _scan_and_classify_working_tree(state)
    if not ok2:
        sr.status = "failed"
        sr.message = "재검사 git status 실패"
        sr.detail = out2[-1500:]
        # Be conservative: assume worst.
        state.publish_blocked = True
        state.publish_blocker_status = "blocked"
        state.publish_blocker_message = "재검사 실패 — 수동 확인 필요"
        _save_blocker_state(state)
        sr.duration_sec = round(time.time() - t0, 3)
        return sr

    # 4. Merge the action history into the post-scan state. The scan
    # above will have set auto_restored_files / auto_deleted_files
    # to whatever STILL needs cleanup; we want the report to show
    # everything we DID clean as well.
    state.auto_restored_files = sorted(set(state.auto_restored_files + actually_restored))
    state.auto_deleted_files = sorted(set(state.auto_deleted_files + actually_deleted))
    # allowed_code is informational and unaffected by cleanup.
    state.allowed_code_files = allowed if not state.allowed_code_files else state.allowed_code_files

    # Recompute warning reasons + conflict markers against the post-
    # cleanup tree so the verdict reflects what publish_changes will see.
    state.conflict_marker_files = _scan_conflict_markers(
        list(state.allowed_code_files) + list(state.manual_required_files)
    )
    state.warning_reasons = _classify_warning_reasons(
        state.allowed_code_files + state.manual_required_files
    )

    # 5. Decide final verdict.
    #
    # blocker  — hard_risky 또는 conflict_marker가 남아있는 경우.
    # warning  — manual_required(=주의 카테고리) 또는 warning_reasons만
    #            남은 경우. publish는 허용.
    # resolved — 자동 정리 활동이 있었고, 남은 차단/경고 모두 없음.
    # clean    — 변경 자체가 없음.
    leftover_auto = [
        p for p in (initial_restore + initial_delete)
        if p not in actually_restored and p not in actually_deleted
    ]
    has_real_blocker = bool(state.hard_risky_files or state.conflict_marker_files)
    has_warning = bool(state.manual_required_files or state.warning_reasons)

    if has_real_blocker or failed or leftover_auto:
        state.publish_blocked = True
        state.publish_blocker_status = "blocked"
        msg_parts: list[str] = []
        if actually_restored:
            msg_parts.append(f"자동 복구 {len(actually_restored)}건")
        if actually_deleted:
            msg_parts.append(f"자동 삭제 {len(actually_deleted)}건")
        if state.hard_risky_files:
            msg_parts.append(f"위험 {len(state.hard_risky_files)}건")
        if state.conflict_marker_files:
            msg_parts.append(f"conflict marker {len(state.conflict_marker_files)}건")
        if failed:
            msg_parts.append(f"자동 정리 실패 {len(failed)}건")
        state.publish_blocker_message = (
            "; ".join(msg_parts)
            + " — 배포를 중단했습니다. (secret/conflict/cleanup 차단)"
        )
        sr.status = "failed"
        sr.message = state.publish_blocker_message
    elif has_warning:
        state.publish_blocked = False
        state.publish_blocker_status = "warning"
        reason_summary = ", ".join(state.warning_reasons[:3]) or (
            f"warning 파일 {len(state.manual_required_files)}건"
        )
        state.publish_blocker_message = (
            f"Release Safety Gate: passed with warnings — 사유: {reason_summary} — "
            "결과: build/health 통과로 배포 허용"
        )
        sr.status = "passed"
        sr.message = state.publish_blocker_message
    elif actually_restored or actually_deleted:
        state.publish_blocked = False
        state.publish_blocker_status = "resolved"
        state.publish_blocker_message = (
            f"자동 복구 {len(actually_restored)}건, 자동 삭제 {len(actually_deleted)}건 — "
            "신규 개발 진행 가능"
        )
        sr.status = "passed"
        sr.message = state.publish_blocker_message
    else:
        state.publish_blocked = False
        state.publish_blocker_status = "clean"
        state.publish_blocker_message = "변경 없음 · Release Safety Gate clean"
        sr.status = "skipped"
        sr.message = "정리할 차단 파일 없음 — clean"

    # 6. Bump per-file recurring counters and write artifacts.
    recurring = _bump_recurring_counters(
        restored=actually_restored,
        deleted=actually_deleted,
        manual=state.manual_required_files,
        hard_risky_basenames=[_safe_basename(p) for p in state.hard_risky_files],
    )
    state.publish_blocker_recurring = {
        k: int(v.get("count", 0))
        for k, v in (recurring or {}).items()
        if isinstance(v, dict)
    }
    _save_blocker_state(state)
    _write_blocker_resolve_report(state, recurring=recurring)

    sr.duration_sec = round(time.time() - t0, 3)
    detail_lines: list[str] = []
    if actually_restored:
        detail_lines.append("[자동 복구]")
        detail_lines += [f"- {p}" for p in actually_restored[:20]]
    if actually_deleted:
        if detail_lines: detail_lines.append("")
        detail_lines.append("[자동 삭제]")
        detail_lines += [f"- {p}" for p in actually_deleted[:20]]
    if state.manual_required_files:
        if detail_lines: detail_lines.append("")
        detail_lines.append("[수동 확인 필요]")
        detail_lines += [f"- {p}" for p in state.manual_required_files[:20]]
    if state.hard_risky_files:
        if detail_lines: detail_lines.append("")
        detail_lines.append("[위험 — basenames only]")
        detail_lines += [f"- {_safe_basename(p)}" for p in state.hard_risky_files[:20]]
    if failed:
        if detail_lines: detail_lines.append("")
        detail_lines.append("[자동 정리 실패]")
        # For hard_risky entries use basenames only — but `failed`
        # paths come from the auto_restore / auto_delete lists, never
        # hard_risky, so the full path is safe to surface here.
        detail_lines += [f"- {p}: {err}" for p, err in failed[:10]]
    sr.detail = "\n".join(detail_lines)[-1800:]
    return sr


# ---------------------------------------------------------------------------
# Product Planner stage (opt-in via FACTORY_PRODUCT_PLANNER_MODE)
#
# v2 of the discovery stage. Forces Claude into a Product Planner role
# with a richer report template (LLM need, data storage, MVP scope,
# success criteria), validates the output against a quality gate, and
# extracts structured summary fields for the dashboard. claude_propose
# downstream is constrained to only build the planner's selected
# feature within the documented scope.
# ---------------------------------------------------------------------------


PRODUCT_PLANNER_PROMPT_TEMPLATE = """\
너는 Stampport의 기획자(Product Planner) 에이전트다.

Stampport는 카페·빵집·맛집·디저트 방문을 여권 도장처럼 모으는 로컬 취향 RPG 서비스다.
스탬프, EXP, 레벨, 뱃지, 칭호, 주간 퀘스트, 킥 포인트, 내 여권, 감성 공유 카드가 핵심 자산이다.

⚠️ 너의 임무는 단순한 요구사항 정리가 아니다. 너는 **욕구 루프 설계자**다.
- 매 사이클 새로운 보상/장치/루프를 발굴해 사용자의 다음 5개 욕구 중 최소 2개를
  자극하는 장치를 직접 제안한다:
    1. 수집욕 (collection)
    2. 과시욕 (show-off / share)
    3. 성장욕 (progression)
    4. 희소성 욕구 (rarity)
    5. 재방문 욕구 (revisit)
- 디자이너 에이전트가 다음 사이클에 반드시 ‘갖고 싶은가/자랑하고 싶은가’ 관점에서
  반박할 것임을 전제로 작성한다.
- 사용자가 해결책을 정해주지 않았다고 가정하라. 기존 코드/UI의 가장 큰 병목을 직접 찾아라.

=== Stampport Domain Profile (config/domain_profiles/stampport.json 일부) ===
{domain_profile}
=== END Domain Profile ===

=== Agent Collaboration Doctrine (docs/agent-collaboration.md 발췌) ===
{collab_doc}
=== END Agent Collaboration ===

이번 사이클의 더 큰 목표 (참고용):
{goal}

가능한 장치 패턴 (참고용 — 그대로 이름 복붙 금지):
- 새 뱃지 / 새 칭호 / 새 퀘스트
- 여권 빈 슬롯과 진행률 시각화
- 도장 자체의 진화 (희소 도장, 시즌 도장)
- 공유 카드 디자인 진화
- 같은 스탬프 보유자 방
- 월간 취향 리포트
- 킥 포인트 정확도 강화

매 사이클마다 다음 순서로 진행하라:

1. 현재 코드/UI를 직접 읽고, 수집/과시/성장/재방문 중 ‘가장 약한 동기 1개’를 찾는다.
   - 추상적이지 않게 한 문장으로 구체화. `path:line` 인용으로 근거를 댄다.
2. 그 약점을 해결할 신규 장치 후보를 **3개 이상** 제안한다.
   - 각 후보는 위 5개 욕구 중 **최소 2개 이상**을 자극해야 한다.
     예) 후보1 = 수집욕 + 과시욕, 후보2 = 성장욕 + 희소성, 후보3 = 재방문 + 과시욕.
   - 동일 패턴의 변형 3개(같은 보상의 색만 바꾼 3개)는 허용되지 않는다.
   - 각 후보는 아래 6개 항목을 **모두** 포함해야 한다:
       a) 기능명 (Stampport 톤의 고유 이름)
       b) 사용자 욕구 (자극하는 욕구 2개 이상)
       c) 핵심 루프 (방문→스탬프→보상→다음 방문 흐름)
       d) MVP 구현 범위 (3~5 bullet)
       e) 기대 행동 변화 (ship 후 사용자 행동이 어떻게 달라지나)
       f) 디자이너에게 던질 질문 (3개)
3. 각 후보를 ‘갖고 싶은가/자랑하고 싶은가/다음 방문을 만드는가’로 평가한다.
4. 이번 사이클에서 만들 장치 1개를 선택한다 (추상명 금지, Stampport 톤의 고유 이름).
5. 가장 작은 출하 단위로 자른다 (3~5 bullet).
6. 프론트 / 백엔드 / LLM / 데이터 저장 / 외부 연동 필요 여부를 자체 판단해 명시한다.
7. 디자이너에게 반박해 달라고 요청할 질문 3가지를 작성한다.

금지:
- 지도/리뷰/관리자/체크리스트 앱 톤의 제안 (Stampport 정체성 위반).
- 문구 개선 / 라벨 변경 / 주석 추가 / 하드코딩 텍스트 추가만 하는 제안.
- 구현 범위 없는 아이디어 나열.
- 사용자 동기 자극 포인트가 명확하지 않은 후보.

도구는 Read, Glob, Grep만 사용 가능. 어떤 파일도 수정하지 마라.
secret/private key/token 값은 어떤 경우에도 출력하지 마라.

출력은 다음 정확한 Markdown 구조만 사용한다. preamble/설명 금지:

# Stampport Product Planner Report

## 제품 방향
Stampport는 카페·빵집·맛집·디저트 방문을 여권 도장처럼 모으는 로컬 취향 RPG 서비스다.

## 사용자가 가진 욕구 중 가장 약한 곳
(수집욕/과시욕/성장욕/재방문 중 어느 동기가 가장 약하게 자극되는지 1~2문단)

## 현재 제품의 한계
(현재 코드/UI에서 위 동기가 충분히 자극되지 않는 지점. `path:line` 인용)

## 이번 사이클의 가장 큰 병목
한 문장으로 구체화한 핵심 병목 1개. 추상명 X.
근거: `path:line` (또는 화면 동작 사례)

## 신규 기능 아이디어 후보

| 기능 | 자극하는 욕구(2개 이상) | 사용자 가치 | 구현 난이도 | 제품 임팩트 | 리스크 |
|---|---|---|---|---|---|
| 후보1 | 예: 수집욕 + 과시욕 | ... | 낮/중/높 | 낮/중/높 | ... |
| 후보2 | 다른 조합 | ... | ... | ... | ... |
| 후보3 | 또 다른 조합 | ... | ... | ... | ... |

## 후보 상세
각 후보마다 아래 6개 항목을 빠짐없이 포함하라.

### 후보 1: <기능명>
- 사용자 욕구: <수집욕/과시욕/성장욕/희소성/재방문 중 2개 이상 + 자극 이유>
- 핵심 루프: <방문→스탬프→보상→다음 방문>
- MVP 구현 범위:
  - bullet 1
  - bullet 2
  - bullet 3
- 기대 행동 변화: <ship 후 사용자 행동이 어떻게 달라지나>
- 디자이너에게 던질 질문:
  1. ...
  2. ...
  3. ...

### 후보 2: <기능명>
- 사용자 욕구: ...
- 핵심 루프: ...
- MVP 구현 범위: ...
- 기대 행동 변화: ...
- 디자이너에게 던질 질문: ...

### 후보 3: <기능명>
- 사용자 욕구: ...
- 핵심 루프: ...
- MVP 구현 범위: ...
- 기대 행동 변화: ...
- 디자이너에게 던질 질문: ...

## 이번 사이클 선정 기능
선정한 기능명 한 줄. Stampport 톤의 고유 이름.

## 선정 이유
이번 병목을 가장 잘 해결하는 이유 한 문단.
다른 후보를 채택하지 않은 이유:
- 후보2: ...
- 후보3: ...

## 사용자 시나리오
사용자가 어떤 화면에서 시작해 도장을 찍고, 어떤 보상/카드/슬롯이 어떻게 보이는지 step-by-step.

## 해결 방식 (자체 판단)
- 핵심 패턴: (수집/과시/성장/재방문 자극 패턴 — Stampport 자산 위에 어떻게 올릴지)
- 왜 이 패턴이 이 병목에 적합한지 한 문단

## LLM 필요 여부
- 필요 / 불필요
- 이유: ...
- 입력: ...
- 출력 JSON schema: {{ "필드1": "타입", ... }}
- fallback 방식: LLM 응답 실패 시 룰 기반으로 어떻게 작동할지

## 데이터 저장 필요 여부
- 필요 / 불필요
- 필요하면: 어떤 데이터를, 어디에 (localStorage / API DB / SQLite / 기타)

## 외부 연동 필요 여부
- 필요 / 불필요
- 필요하면: 어떤 외부 API/데이터/서비스, 인증 방식

## 프론트 변경 범위
- `app/web/.../FILE.jsx` — 무엇을 추가/수정 (구체적으로)
- ...

## 백엔드 변경 범위
- `app/api/.../FILE.py` — 무엇을 추가/수정
- 또는: "백엔드 변경 불필요 — 사유: ..." (명시적으로)

## 이번 사이클 MVP 범위
- 이번 사이클에 반드시 만들 것 (3~5 bullet — 가장 작은 가치 단위)

## 이번 사이클에서 하지 않을 것
- 의도적으로 미루는 항목들 (스코프 확장 방지)

## 디자이너에게 던질 질문
- 이 도장/뱃지/카드가 정말 갖고 싶게 보이는가?
- 이 카드가 인스타 스토리에 자랑하고 싶게 보이는가?
- 이 슬롯/장치가 다음 방문 욕구를 만드는가?

## 성공 기준
- 사용자가 X를 N번 하면 Y가 보인다 같은 검증 가능한 기준 2~3개
"""


# Keywords whose presence in PM HOLD reasons / designer 약점 means the
# next cycle's bottleneck is *implementation specification*, not a new
# product idea. When any of these match, the planner is forced into
# spec-confirmation mode and the design_spec stage runs instead of
# treating the HOLD as a hint for fresh ideation.
PM_HOLD_SPEC_KEYWORDS: tuple[str, ...] = (
    "SVG path", "svg path",
    "titleLabel", "title_label",
    "좌표",
    "ShareCard", "sharecard", "share-card",
    "layout",
    "구현 명세",
    "badges.js",
    "selectedTitle",
    "locked",
)


def _detect_spec_mode_keywords(*texts: str) -> list[str]:
    """Scan the given texts for spec-mode trigger keywords. Returns the
    list of matched keywords (deduplicated, in detection order)."""
    seen: list[str] = []
    haystack = "\n".join(t for t in texts if t)
    if not haystack:
        return seen
    lower = haystack.lower()
    for kw in PM_HOLD_SPEC_KEYWORDS:
        if kw.lower() in lower and kw not in seen:
            seen.append(kw)
    return seen


def _read_pm_hold_artifacts() -> tuple[str, str, bool]:
    """Read pm_decision.md + designer_final_review.md and return
    (pm_md, designer_md, hold_active). hold_active is True when the
    PM verdict explicitly says hold."""
    pm_md = _read_artifact(PM_DECISION_FILE) or ""
    designer_md = _read_artifact(DESIGNER_FINAL_REVIEW_FILE) or ""
    if not pm_md.strip():
        return pm_md, designer_md, False
    decision = _extract_md_section(pm_md, "출하 결정").lower()
    return pm_md, designer_md, "hold" in decision


def _load_pm_hold_rework_context(*, return_spec_mode: bool = False):
    """If the previous cycle's PM verdict was HOLD, build a
    "Previous PM HOLD" rework context block to prepend to the next
    planner prompt.

    Reads pm_decision.md (출하 결정 / 결정 이유 / 다음 단계 담당) and
    designer_final_review.md (욕구 점수표 / 약점). Returns "" when
    there's no HOLD signal so callers can use the boolean-ish return.

    When `return_spec_mode=True` returns a tuple
    `(rework_text, spec_mode_active, spec_keywords)` so the caller can
    record the trigger keywords in CycleState.

    The block is *advisory* — the planner is told the prior cycle's
    weakness must be the bottleneck of this cycle, not a free choice
    of new candidates. When spec mode is active the block is upgraded
    from advisory to *prescriptive* (planner explicitly redirected to
    spec-confirmation, not new ideation).
    """
    pm_md, designer_md, hold_active = _read_pm_hold_artifacts()
    rework_lock = _load_active_rework_feature()
    locked_feature = (rework_lock.get("feature") or "").strip()
    locked_hold_count = int(rework_lock.get("hold_count") or 0)
    if not hold_active and not locked_feature:
        if return_spec_mode:
            return "", False, []
        return ""

    reason = _extract_md_section(pm_md, "결정 이유")
    next_owners = _extract_md_section(pm_md, "다음 단계 담당")
    qa_extra = _extract_md_section(pm_md, "QA가 추가로 점검할 것")
    weaknesses = _extract_md_section(designer_md, "약점")
    score_section = _extract_md_section(designer_md, "욕구 점수표")
    final = _extract_md_section(designer_md, "최종 판단")

    spec_keywords = _detect_spec_mode_keywords(
        reason, weaknesses, next_owners, final
    )
    spec_mode_active = bool(spec_keywords)

    score_summary = ""
    if score_section:
        # Pull all "| <축> | <n> |" rows so the next planner sees the
        # exact gates it must close. Cheap heuristic — anything with a
        # standalone digit 1-5 in cell 2 counts.
        rows: list[str] = []
        for line in score_section.splitlines():
            s = line.strip()
            if not s.startswith("|") or not s.endswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) >= 2 and re.fullmatch(r"[1-5]", cells[1]):
                rows.append(f"  - {cells[0]}: {cells[1]} / 5")
        if rows:
            score_summary = "\n".join(rows)

    pieces: list[str] = [
        "=== Previous PM HOLD (직전 사이클 재작업 입력) ===",
        "이번 사이클은 직전 사이클의 PM HOLD 지시를 해소하는 **rework cycle** 이다.",
        "직전 약점을 무시하고 새 후보 3개를 무작위로 제안하지 마라. 기존 HOLD 해소가 최우선 병목이다.",
        "",
        "## PM 출하 결정",
        "- hold (재작업 후 다음 사이클)",
    ]
    if reason:
        pieces += ["", "## 결정 이유", reason.strip()]
    if score_summary:
        pieces += ["", "## 직전 미달 점수 (욕구 점수표)", score_summary]
    if weaknesses:
        pieces += ["", "## 디자이너가 지적한 약점", weaknesses.strip()]
    if next_owners:
        pieces += ["", "## PM 다음 단계 담당", next_owners.strip()]
    if qa_extra:
        pieces += ["", "## QA 추가 점검 항목", qa_extra.strip()]
    if final:
        pieces += ["", "## 디자이너 최종 판단", final.strip()]

    if spec_mode_active:
        kw_list = ", ".join(f"`{k}`" for k in spec_keywords)
        pieces += [
            "",
            "## ⚠️ 이번 사이클은 디자인 구현 명세 확정 모드입니다",
            "이번 사이클은 새 기능 탐색이 아니라 직전 PM HOLD를 해소하는 구현 명세 확정 사이클입니다.",
            f"- 트리거된 keyword: {kw_list}",
            "- 새 후보 3개를 무작위 제안하지 마세요. 직전 HOLD에 명시된 구현 구멍을 닫는 단 한 가지 후보만 제안하세요.",
            "- 후보의 'MVP 구현 범위' 는 SVG path 좌표, titleLabel 최종 목록, ShareCard 렌더 조건 등 숫자/문자열 단위로 확정 가능한 항목을 포함해야 합니다.",
            "- 디자이너는 같은 사이클 안에서 `.runtime/design_spec.md` 를 작성합니다 (stage_design_spec). 기획자는 그 명세에 들어갈 항목을 후보 안에 미리 적어 두세요.",
        ]

    pieces += [
        "",
        "## 이번 사이클이 반드시 만족해야 할 제약",
        "- 직전 약점 (위 \"디자이너가 지적한 약점\" 섹션) 을 해소할 후보 1개 선정.",
        "- PM \"다음 단계 담당\" 의 디자이너/기획자 항목을 그대로 후보의 구현 범위에 포함.",
        "- `selectedTitle` 이 string 인 문제, 잠금 조건이 `progress === 0` 인 문제,"
        " ShareCard 칭호 라인 위치, SVG 배지 3종 (원형/방패/왕관) 등 직전 PM 결정에 명시된 구현 구멍을 다루는 후보를 우선.",
        "- 후보 3개 중 최소 1개는 직전 HOLD 사유와 직접적으로 연결되어야 한다.",
        "- 새 후보가 직전 약점을 우회하기만 하면 안 된다 — 해소를 명시적으로 제안하라.",
        "",
        "## 품질 가드 통과를 위한 강제 항목 (이전 사이클 가드 실패 → fallback 진입의 재발 방지)",
        "- **후보 3개를 반드시 작성**하라. 2개 이하면 다음 사이클도 fallback 으로 떨어진다.",
        "- **각 후보는 다음 5가지 욕구 중 최소 2가지를 자극**해야 한다:"
        " `수집욕`, `과시욕`, `성장욕`, `희소성`, `재방문`. 후보 상세의 `사용자 욕구`"
        " 항목에 해당 키워드가 그대로 등장해야 한다.",
        "- **각 후보의 MVP 구현 범위**는 1) 수정 대상 파일 경로 (예: `app/web/src/screens/...`),"
        " 2) 수정 대상 화면 이름, 3) 결정론적 트리거 조건을 함께 적어야 한다."
        " 추상적 표현 (\"더 명확하게\", \"개선\") 만으로는 가드를 통과할 수 없다.",
        "- **선정 기능 섹션**에는 `target_files` 가 최소 2개 이상 명시되어야 한다."
        " PM 의 implementation_ticket validator 가 이 목록을 그대로 사용한다.",
        "- 직전 HOLD 사유 자체를 후보 1개의 \"사용자 문제\" 로 그대로 옮겨 적어라."
        " HOLD 를 우회하는 새 아이디어는 다음 cycle 도 HOLD 한다.",
    ]
    if locked_feature:
        pieces += [
            "",
            "## 🔒 ACTIVE REWORK FEATURE LOCK (필수)",
            (
                f"직전 사이클의 selected_feature 가 ship 되지 못하고 HOLD 로 종료되어, "
                f"이번 사이클은 동일 기능을 재작업하는 cycle 로 강제됩니다."
            ),
            f"- 잠긴 selected_feature: **{locked_feature}**",
            f"- 누적 HOLD 횟수: {locked_hold_count}",
            (
                "- 새 후보 3개를 무작위로 제안하지 마세요. 후보 3개를 채울 때도 위 잠긴 기능을 "
                "이번 사이클의 `이번 사이클 선정 기능` 으로 그대로 사용해야 합니다."
            ),
            (
                "- `이번 사이클 선정 기능` 헤더에 위 잠긴 이름과 다른 단어를 적으면 product_planning "
                "단계에서 reject 되고 fallback 보고서로 강등됩니다 — 그러면 이번 cycle 도 HOLD 입니다."
            ),
            (
                "- 잠금을 풀 수 있는 유일한 방법은 이번 사이클에서 잠긴 기능을 ship (claude_apply 적용) "
                "시키는 것입니다."
            ),
        ]
    pieces += [
        "=== END Previous PM HOLD ===",
        "",
    ]
    text = "\n".join(pieces)
    if return_spec_mode:
        return text, spec_mode_active, spec_keywords
    return text


def _build_product_planner_prompt(goal: str) -> str:
    profile = _load_stampport_profile_text()
    collab = _load_agent_collab_text()
    base = PRODUCT_PLANNER_PROMPT_TEMPLATE.format(
        goal=goal.strip() or DEFAULT_GOAL,
        domain_profile=profile or "(stampport.json 미존재)",
        collab_doc=collab or "(agent-collaboration.md 미존재)",
    )
    rework = _load_pm_hold_rework_context()
    if rework:
        # Prepend rework context so the LLM reads the constraint
        # *before* the role description. We keep the role intro intact
        # below so the planner's behavior contract is unchanged.
        return rework + base
    return base


# Planner heading aliases — accepted variants for the canonical
# "기능" headings. The earlier prompt iterations used "장치" instead
# of "기능" so old / out-of-date Claude outputs would fail the gate
# even though the body shape was fine. We accept the alias at parse /
# validate time and rewrite to the canonical form via
# `_normalize_planner_body` so downstream stages see one shape.
PLANNER_HEADING_ALIASES: dict[str, tuple[str, ...]] = {
    "신규 기능 아이디어 후보": ("신규 장치 아이디어 후보",),
    "이번 사이클 선정 기능":   ("이번 사이클 선정 장치",),
}


def _heading_variants(heading: str) -> tuple[str, ...]:
    aliases = PLANNER_HEADING_ALIASES.get(heading, ())
    return (heading, *aliases)


def _extract_md_section(md: str, heading: str) -> str:
    """Return the body under '## heading' until the next ## or end-of-doc.

    Tries every accepted alias for `heading` before giving up — see
    `PLANNER_HEADING_ALIASES`. The first match wins.
    """
    for variant in _heading_variants(heading):
        pat = (
            r"^##\s+" + re.escape(variant)
            + r"\s*\n(.*?)(?=\n##\s|\Z)"
        )
        m = re.search(pat, md, re.MULTILINE | re.DOTALL)
        if m:
            return m.group(1).strip()
    return ""


def _normalize_planner_body(body: str) -> str:
    """Rewrite alias headings to the canonical "기능" form so downstream
    parsers / readers see one shape. Idempotent — running twice is safe.

    Only operates on `## ` headings so prose text containing the alias
    word ("이 장치는 ...") is left alone.
    """
    out = body
    for canonical, aliases in PLANNER_HEADING_ALIASES.items():
        for alias in aliases:
            out = re.sub(
                r"^##\s+" + re.escape(alias) + r"\s*$",
                f"## {canonical}",
                out,
                flags=re.MULTILINE,
            )
    return out


def _strip_md_emphasis(line: str) -> str:
    """Best-effort cleanup of markdown emphasis on a single line so the
    dashboard doesn't show stray `**...**` / `` `...` `` markers.

    We replace inline emphasis pairs FIRST (otherwise lstripping
    leading `*` would orphan the matching trailing `**` in lines like
    `**Feature Name** — description`)."""
    line = line.strip()
    # Drop leading non-emphasis bullets only ("-", "•"). Leave "*"
    # alone — it might be the open of a markdown bold/italic pair.
    line = re.sub(r"^[-•]\s*", "", line)
    # Replace paired inline emphasis with their inner content.
    line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
    line = re.sub(r"__(.+?)__", r"\1", line)
    line = re.sub(r"\*(.+?)\*", r"\1", line)
    line = re.sub(r"_(.+?)_", r"\1", line)
    line = re.sub(r"`(.+?)`", r"\1", line)
    # Final mop-up of any orphaned single markers left behind by an
    # unbalanced markdown line.
    return line.strip(" *_`-")


def _first_meaningful_line(text: str, max_chars: int = 140) -> str:
    """First non-empty line of `text`, with markdown stripped and capped."""
    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        line = _strip_md_emphasis(raw)
        if line:
            return line[: max_chars - 3] + "..." if len(line) > max_chars else line
    return ""


def _extract_selected_feature(md_body: str) -> str:
    """Pull the line under '## 이번 사이클 선정 기능' and clean markdown."""
    section = _extract_md_section(md_body, "이번 사이클 선정 기능")
    return _first_meaningful_line(section, max_chars=100)


def _extract_yes_no(section_text: str) -> str:
    """Return '필요' / '불필요' / '' from a section that should declare one.

    The first non-empty line typically reads `- 필요` or `- 불필요`.
    We check `불필요` first because it's a substring of `필요`."""
    if not section_text:
        return ""
    first = _first_meaningful_line(section_text, max_chars=200)
    if "불필요" in first:
        return "불필요"
    if "필요" in first:
        return "필요"
    return ""


def _extract_llm_needed(md_body: str) -> str:
    """Return '필요' / '불필요' / '' from the LLM section."""
    return _extract_yes_no(_extract_md_section(md_body, "LLM 필요 여부"))


def _extract_data_storage_needed(md_body: str) -> str:
    return _extract_yes_no(_extract_md_section(md_body, "데이터 저장 필요 여부"))


def _extract_external_integration_needed(md_body: str) -> str:
    return _extract_yes_no(_extract_md_section(md_body, "외부 연동 필요 여부"))


def _extract_solution_pattern(md_body: str) -> str:
    """The "## 해결 방식 (자체 판단)" section's first line is expected to be
    `- 핵심 패턴: <pattern>`. Pull just the pattern label."""
    section = _extract_md_section(md_body, "해결 방식 (자체 판단)")
    if not section:
        return ""
    first = _first_meaningful_line(section, max_chars=200)
    # Drop common leading "핵심 패턴:" prefix.
    first = re.sub(r"^\s*핵심\s*패턴\s*[:·]\s*", "", first)
    # If still no useful content, just bail.
    return first[:80] if first else ""


def _extract_bottleneck(md_body: str) -> str:
    """Pull the planner's diagnosed bottleneck (one-sentence)."""
    section = _extract_md_section(md_body, "이번 사이클의 가장 큰 병목")
    if not section:
        return ""
    # The section may contain a sentence + a "근거: path:line" reference.
    # Take the first non-empty line with the markdown stripped.
    return _first_meaningful_line(section, max_chars=180)


def _count_candidate_rows(md_body: str) -> int:
    """How many candidate features did the planner list?

    Accepts both the canonical Markdown table format and a numbered
    list fallback (1./2./3.) — Claude sometimes drops the table.
    """
    section = _extract_md_section(md_body, "신규 기능 아이디어 후보")
    if not section:
        return 0
    # Markdown table data rows: lines that start/end with `|` and aren't
    # the header (`| 기능 | ... |`) or the separator (`|---|---|`).
    table_rows = 0
    for line in section.splitlines():
        s = line.strip()
        if not s.startswith("|") or not s.endswith("|"):
            continue
        # Skip separator row.
        if re.fullmatch(r"\|[\s\-:|]+\|", s):
            continue
        # Skip header-ish row (contains '기능' literal in first cell as the column header).
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) >= 2 and cells[0] in {"기능", "기능명", "Feature", "feature"}:
            continue
        table_rows += 1

    list_items = len(re.findall(r"^\s*\d+[.)]\s+\S", section, re.MULTILINE))
    # H3-form candidates ("### 후보 1") — used by the fallback report
    # and increasingly by Claude itself as the prompt grew.
    h3_items = len(re.findall(r"^###\s+후보\s*\d+\b", section, re.MULTILINE))
    return max(table_rows, list_items, h3_items)


def _validate_planner_report(body: str) -> list[str]:
    """Return a list of human-readable reasons the report failed the
    quality gate. Empty list = passes."""
    fails: list[str] = []

    REQUIRED = [
        "이번 사이클의 가장 큰 병목",
        "신규 기능 아이디어 후보",
        "이번 사이클 선정 기능",
        "사용자 시나리오",
        "해결 방식 (자체 판단)",
        "LLM 필요 여부",
        "데이터 저장 필요 여부",
        "외부 연동 필요 여부",
        "프론트 변경 범위",
        "백엔드 변경 범위",
        "성공 기준",
    ]
    for h in REQUIRED:
        if not _extract_md_section(body, h):
            fails.append(f"필수 섹션 누락: ## {h}")

    # Bottleneck must be a real sentence (≥20 chars) — otherwise the
    # planner is dodging the diagnosis.
    bottleneck = _extract_bottleneck(body)
    if bottleneck and len(bottleneck) < 20:
        fails.append(f"가장 큰 병목 진단이 너무 짧음 (<20자): {bottleneck!r}")

    # ≥3 candidates
    n_candidates = _count_candidate_rows(body)
    if n_candidates < 3:
        fails.append(f"신규 기능 아이디어 후보 3개 미만 ({n_candidates}개)")

    # Non-empty selected feature
    if not _extract_selected_feature(body):
        fails.append("이번 사이클 선정 기능이 비어있음")

    # User scenario must have substance.
    sc = _extract_md_section(body, "사용자 시나리오")
    if len(sc) < 30:
        fails.append("사용자 시나리오가 너무 짧음 (<30자)")

    # Frontend scope must reference a concrete file. Accept the path
    # in either `code`, **bold**, or plain inline form — we just need
    # something that looks like a real source file.
    fe = _extract_md_section(body, "프론트 변경 범위")
    fe_file_pat = r"[\w./-]+\.(?:jsx?|tsx?|css|scss|html)\b"
    if not re.search(fe_file_pat, fe):
        fails.append("프론트 변경 범위에 구체 파일 참조 없음")

    # Backend scope: either has a concrete file ref OR explicit "불필요" reason.
    be = _extract_md_section(body, "백엔드 변경 범위")
    has_be_file = bool(re.search(r"[\w./-]+\.py\b", be))
    has_be_skip = "불필요" in be and len(be.strip()) >= 20
    if not has_be_file and not has_be_skip:
        fails.append("백엔드 변경 범위에 구체 파일 참조 또는 명시적 불필요 사유가 없음")

    # LLM / 데이터 저장 / 외부 연동 — 셋 다 명확하게 필요/불필요 결정 필요.
    if not _extract_llm_needed(body):
        fails.append("LLM 필요 여부 판단(필요/불필요)이 명확하지 않음")
    if not _extract_data_storage_needed(body):
        fails.append("데이터 저장 필요 여부 판단(필요/불필요)이 명확하지 않음")
    if not _extract_external_integration_needed(body):
        fails.append("외부 연동 필요 여부 판단(필요/불필요)이 명확하지 않음")
    if not _extract_solution_pattern(body):
        fails.append("해결 방식(자체 판단)의 핵심 패턴이 명확하지 않음")

    # Success criteria must be concrete.
    sk = _extract_md_section(body, "성공 기준")
    if len(sk) < 30:
        fails.append("성공 기준이 너무 짧음 (<30자)")

    # Trivial / abstract red flags. We tolerate a single "더 구체적으로"
    # in the report (e.g., as a counterexample) but ban it as the
    # PRIMARY pitch of the selected feature.
    selected = _extract_selected_feature(body)
    abstract_phrases_in_selected = [
        "더 구체적으로 안내", "조금 더 명확", "더 좋게", "더 친절"
    ]
    if any(p in selected for p in abstract_phrases_in_selected):
        fails.append("선정 기능이 추상 개선 표현만 담고 있음")

    # Are most candidates copy-only? Heuristic: count "문구"/"라벨"/"안내"
    # appearances in the candidate section.
    cand = _extract_md_section(body, "신규 기능 아이디어 후보")
    copy_signal = sum(cand.count(k) for k in ("문구 개선", "라벨 변경", "버튼 텍스트"))
    if n_candidates >= 3 and copy_signal >= n_candidates:
        fails.append("기능 후보가 대부분 문구/라벨 개선에 집중됨")

    # Desire-loop per-candidate fields. The new ping-pong protocol
    # requires each candidate to spell out: 사용자 욕구 (≥2 desires) /
    # 핵심 루프 / MVP 구현 범위 / 기대 행동 변화 / 디자이너에게 던질
    # 질문. We check the "## 후보 상세" section for ≥3 sub-cards and
    # the presence of these labels per sub-card. Tolerated when the
    # legacy table-only format is used — we only fail if the planner
    # did include a "후보 상세" block but skipped fields.
    detail = _extract_md_section(body, "후보 상세")
    if detail:
        sub_blocks = re.split(r"^###\s+후보\s*\d+\b", detail, flags=re.MULTILINE)
        # First chunk is the section preamble; real candidate blocks
        # are everything after.
        sub_blocks = [s.strip() for s in sub_blocks[1:] if s.strip()]
        if len(sub_blocks) < 3:
            fails.append(
                f"후보 상세 sub-card 3개 미만 ({len(sub_blocks)}개)"
            )
        REQUIRED_FIELDS = (
            "사용자 욕구",
            "핵심 루프",
            "MVP 구현 범위",
            "기대 행동 변화",
            "디자이너에게 던질 질문",
        )
        for i, block in enumerate(sub_blocks, start=1):
            for f in REQUIRED_FIELDS:
                if f not in block:
                    fails.append(f"후보{i} 상세에 필수 항목 '{f}' 없음")
            # 사용자 욕구는 최소 2개 이상이어야 한다.
            desires = ("수집욕", "과시욕", "성장욕", "희소성", "재방문")
            need_line = ""
            for line in block.splitlines():
                if line.lstrip("-* ").startswith("사용자 욕구"):
                    need_line = line
                    break
            if need_line:
                hits = sum(1 for d in desires if d in need_line)
                if hits < 2:
                    fails.append(
                        f"후보{i} 사용자 욕구가 2개 미만 (자극 욕구: {hits}개)"
                    )

    return fails


# ---------------------------------------------------------------------------
# Planner fallback report.
#
# When stage_product_planning's LLM call fails / returns empty / lacks
# the required header / fails the quality gate, the cycle used to bail
# without writing any artifact. That left the dashboard with no
# product_planner_report.md and every downstream stage (designer / pm /
# implementation_ticket / claude_apply) skipped. The watchdog then
# reported the same `planner_required_output_missing` over and over.
#
# The fallback fixes this end-to-end: it writes a Stampport-themed,
# validator-passing report with three concrete MVP candidates so the
# next stages can keep running. The actual feature is small but real
# (Local Visa / Taste Title / Passport 발급 대기 슬롯), and the next
# cycle's LLM gets a fresh shot. The original failure reason is kept
# in state.product_planner_gate_failures + state.product_planner_message
# so the operator can see *why* fallback fired.
# ---------------------------------------------------------------------------


def _build_planner_fallback_report(
    state: "CycleState",
    *,
    source_failure: str,
    gate_failures: list[str] | None = None,
) -> str:
    cycle_id = state.cycle
    failures = list(gate_failures or [])
    failure_lines = "\n".join(f"- {f}" for f in failures[:8]) or "- (no gate failures captured)"
    return f"""# Stampport Product Planner Report

(자동 fallback 보고서 — LLM 응답이 없거나 품질 가드를 통과하지 못해 안전 기본 후보로 작성됨. 다음 사이클의 LLM 기획자가 더 구체적인 후보를 만들 발판으로 사용.)

## 자동 fallback 사유
- 발생 시각: {utc_now_iso()}
- 사이클: #{cycle_id}
- 원인: {source_failure}
- 품질 가드 실패 항목:
{failure_lines}

## 이번 사이클의 가장 큰 병목
사용자가 다시 앱을 열 이유를 만들어 줄 수집 / 공유 장치가 부족합니다. 도장은 모이지만 자랑하거나 진화하는 흐름이 없어, 한 번 방문한 사용자가 같은 동네를 다시 찾을 동기가 약합니다. 이번 사이클은 fallback 후보 중 작은 범위 1개를 ship해서 다음 사이클의 기획자가 진짜 새 기능을 제안할 발판을 만듭니다.

## 신규 기능 아이디어 후보

### 후보 1
- 기능명: Local Visa 배지
- 사용자 문제: 도장은 모이는데 자랑/공유 욕구가 약함
- 핵심 루프: 방문 → 도장 → 같은 동네 3회 도장 시 Local Visa 자동 발급 → ShareCard에서 자랑
- 구현 범위: MVP — Visa 배지 1종, 발급 트리거 1종 (같은 dong_code 방문 3회)
- 수정 대상 화면: MyPassport, ShareCard
- 예상 수정 파일: app/web/src/screens/MyPassport.jsx, app/web/src/components/ShareCard.jsx
- 성공 기준: 동일 동네 도장 3회 시 Visa 자동 발급 + MyPassport/ShareCard 시각 노출
- 디자이너 검토 질문: 도장과 별개로 Visa 시각요소를 어떻게 차별화할지?

### 후보 2
- 기능명: Taste Title 진화
- 사용자 문제: 방문 데이터가 단순 누적으로 끝나고 사용자 정체성으로 이어지지 않음
- 핵심 루프: 카테고리별 도장 누적 → 칭호 진화 → MyPassport 헤더 갱신 → 친구에게 자랑
- 구현 범위: MVP — 카페 카테고리 1종, 도장 5/15/30개 임계값으로 3단계 진화
- 수정 대상 화면: MyPassport, Titles
- 예상 수정 파일: app/web/src/screens/MyPassport.jsx, app/web/src/components/TitleBadge.jsx
- 성공 기준: 카테고리 도장 5개 누적 시 칭호 1단계 자동 부여 + 헤더 갱신
- 디자이너 검토 질문: 진화 단계 1→2→3 시각언어를 어떤 모티브로 잡을지?

### 후보 3
- 기능명: Passport 발급 대기 슬롯
- 사용자 문제: 다음 도장까지 사용자가 무엇을 해야 하는지 불분명
- 핵심 루프: 미방문 동네 추천 → 슬롯 표시 → 방문 시 도장 → 슬롯 갱신
- 구현 범위: MVP — 추천 동네 3개 슬롯, 룰 기반 (LLM 미사용)
- 수정 대상 화면: MyPassport, Quests
- 예상 수정 파일: app/web/src/screens/MyPassport.jsx, app/web/src/components/QuestSlot.jsx
- 성공 기준: 슬롯에 표시된 동네 중 하나를 방문해 도장을 찍는 행동 한 번 이상 발생
- 디자이너 검토 질문: 슬롯 추천이 강요처럼 보이지 않게 하려면 톤을 어떻게?

## 후보 상세

### 후보 1 — Local Visa 배지
- 사용자 욕구: 수집욕, 과시욕, 희소성
- 핵심 루프: 방문 → 도장 → Visa 발급 → ShareCard 공유
- MVP 구현 범위: Visa 배지 1종 + 자동 발급 룰 + ShareCard 노출
- 기대 행동 변화: 같은 동네 재방문 비율 상승 + ShareCard 열람 횟수 증가
- 디자이너에게 던질 질문: Visa 도안의 색감/타이포로 도장과 어떻게 구분할지?

### 후보 2 — Taste Title 진화
- 사용자 욕구: 성장욕, 수집욕, 재방문
- 핵심 루프: 카테고리 누적 → 칭호 진화 → 헤더 갱신 → 친구 공유
- MVP 구현 범위: 카페 카테고리 3단계 + 헤더 갱신
- 기대 행동 변화: 카테고리 집중 방문 비율 상승 + 다음 단계 알림에 대한 클릭률 측정
- 디자이너에게 던질 질문: 진화 단계 1→2→3을 어떤 모티브 시각언어로?

### 후보 3 — Passport 발급 대기 슬롯
- 사용자 욕구: 재방문, 희소성, 수집욕
- 핵심 루프: 추천 슬롯 → 방문 → 도장 → 슬롯 갱신
- MVP 구현 범위: 추천 동네 3개 슬롯 + 클라이언트 룰
- 기대 행동 변화: 미방문 동네 방문 빈도 증가
- 디자이너에게 던질 질문: 슬롯 추천이 강요처럼 보이지 않게 톤을 어떻게?

## 이번 사이클 선정 기능
- 선정 기능: Local Visa 배지
- 선정 이유: 가장 작은 변경 범위 (FE 2~3개 파일)에서 사용자의 수집/과시 욕구를 동시에 자극할 수 있고, BE/외부 연동 없이 클라이언트 룰만으로 ship 가능. 다음 사이클이 카테고리 확장과 서버 sync로 자연스럽게 이어짐.
- 사용자 가치: "내가 이 동네를 정복했다"는 시각적 증거가 즉시 만들어지고, 친구에게 공유 가능한 자산이 됨.
- 이번 사이클 구현 범위: Visa 배지 1종, 발급 트리거 (같은 dong_code 방문 3회), MyPassport와 ShareCard 노출
- 수정 대상 화면: MyPassport, ShareCard
- 수정 대상 파일: app/web/src/screens/MyPassport.jsx, app/web/src/components/ShareCard.jsx, app/web/src/components/VisaBadge.jsx
- FE 작업: VisaBadge 신규 컴포넌트, MyPassport에 발급된 Visa 노출, ShareCard 미리보기에 Visa 일러스트 추가
- BE 작업: 불필요 — MVP는 클라이언트 LocalStorage 기반 룰만 사용
- AI/룰 작업: 같은 dong_code 방문 카운팅 + Visa 발급 함수 (LLM 미사용)
- 제외 범위: 서버 측 Visa 동기화, 다른 카테고리 Visa 종류, 외부 SNS 자동 게시
- QA 시나리오:
  1. 동일 dong_code 도장 3회 → Visa 배지가 발급됨
  2. MyPassport 헤더에 발급된 Visa가 노출됨
  3. ShareCard 미리보기에 Visa 일러스트 + 라벨이 등장함
- 성공 기준: 동일 동네 도장 3회 누적 시 Visa 자동 발급되고, MyPassport와 ShareCard 두 화면 모두 시각적으로 확인 가능

## 사용자 시나리오
사용자는 단골 동네의 카페 두 군데를 며칠에 걸쳐 방문해 도장을 찍었다. 세 번째 방문에서 도장을 찍자 자동으로 Local Visa 배지가 발급되고, MyPassport 상단에 "이 동네의 단골" 라벨이 등장한다. 사용자는 ShareCard 미리보기에서 새 Visa를 발견하고 친구에게 공유한다.

## 해결 방식 (자체 판단)
중심 패턴: 방문 카운팅 + 임계값 기반 자동 발급. LLM 호출 없이 클라이언트 룰만으로 구현되며, 다음 사이클에서 카테고리 확장 / 서버 sync로 점진 진화.

## LLM 필요 여부
불필요 — 이번 MVP는 결정론적 룰 기반. 다음 사이클이 카테고리 추천을 LLM 으로 확장.

## 데이터 저장 필요 여부
필요 — 클라이언트 LocalStorage 에 dong_code 별 방문 카운터 저장. 서버 저장은 다음 사이클로 미룸.

## 외부 연동 필요 여부
불필요 — 외부 SNS 게시는 다음 사이클 범위. 이번 사이클은 ShareCard 미리보기까지만.

## 프론트 변경 범위
app/web/src/screens/MyPassport.jsx
app/web/src/components/ShareCard.jsx
app/web/src/components/VisaBadge.jsx (신규)

## 백엔드 변경 범위
불필요 — 이번 사이클 MVP 는 클라이언트 룰 + LocalStorage 만 사용합니다. 서버 측 Visa 동기화 / 카테고리별 집계 / 다른 사용자와의 비교는 다음 사이클에서 BE 작업으로 분리해서 진행합니다.

## 성공 기준
1. 같은 dong_code 도장 3회 누적 시 Visa 자동 발급
2. MyPassport / ShareCard 두 화면에 Visa 시각적 노출
3. ShareCard 미리보기 caption 에 Visa 라벨 포함
4. 사용자가 ShareCard 를 한 번 이상 열어볼 수 있도록 발급 직후 알림 표시
5. 다음 사이클에서 fallback 이 아닌 LLM 기획이 정상 동작하면 fallback 사유가 비워짐
"""


# Selected feature label that the fallback always points at — kept in
# sync with the body's "## 이번 사이클 선정 기능" so downstream
# extractors don't have to re-parse.
PLANNER_FALLBACK_SELECTED = "Local Visa 배지"
PLANNER_FALLBACK_BOTTLENECK = (
    "사용자가 다시 앱을 열 이유를 만들어 줄 수집 / 공유 장치가 부족합니다."
)
PLANNER_FALLBACK_SOLUTION_PATTERN = "방문 카운팅 + 임계값 기반 자동 발급"


def _persist_planner_fallback(
    state: "CycleState",
    *,
    sr: "StageResult",
    source_failure: str,
    gate_failures: list[str] | None,
    raw_body: str | None = None,
) -> None:
    """Write the fallback report to both canonical filenames, set
    state.product_planner_status = fallback_generated, and emit a
    cycle_log marker so the dashboard surfaces the fallback path.

    The original LLM body (if any) is preserved as
    `product_planner_report.rejected.md` for operator inspection.
    """
    body = _build_planner_fallback_report(
        state, source_failure=source_failure, gate_failures=gate_failures,
    )

    # Preserve whatever the LLM produced (even if junk) for debugging.
    if raw_body:
        safe_write_artifact(
            PRODUCT_PLANNER_FILE.with_suffix(".rejected.md"),
            raw_body,
            cycle_id=state.cycle, stage="planner_proposal",
            source_agent="planner",
            extra={"verdict": "rejected_pre_fallback"},
        )

    safe_write_artifact(
        PRODUCT_PLANNER_FILE, body,
        cycle_id=state.cycle, stage="planner_proposal", source_agent="planner",
        extra={"fallback": "true", "source_failure": source_failure[:80]},
    )
    safe_write_artifact(
        PLANNER_PROPOSAL_FILE, body,
        cycle_id=state.cycle, stage="planner_proposal", source_agent="planner",
        extra={"fallback": "true", "source_failure": source_failure[:80]},
    )

    state.product_planner_status = "fallback_generated"
    state.product_planner_path = str(PRODUCT_PLANNER_FILE)
    state.product_planner_at = utc_now_iso()
    state.product_planner_bottleneck = PLANNER_FALLBACK_BOTTLENECK
    state.product_planner_selected_feature = PLANNER_FALLBACK_SELECTED
    state.product_planner_solution_pattern = PLANNER_FALLBACK_SOLUTION_PATTERN
    state.product_planner_value_summary = (
        "수집/과시 욕구를 동시에 자극하는 Local Visa MVP — 작은 변경 범위로 ship"
    )
    state.product_planner_llm_needed = "불필요"
    state.product_planner_data_storage_needed = "필요"
    state.product_planner_external_integration_needed = "불필요"
    state.product_planner_frontend_scope = (
        "app/web/src/screens/MyPassport.jsx, "
        "app/web/src/components/ShareCard.jsx, "
        "app/web/src/components/VisaBadge.jsx"
    )
    state.product_planner_backend_scope = "불필요 (클라이언트 룰만 사용)"
    state.product_planner_success_criteria = (
        "동일 dong_code 3회 도장 시 Visa 자동 발급 + MyPassport / ShareCard 시각 노출"
    )
    state.product_planner_candidate_count = 3
    state.product_planner_gate_failures = list(gate_failures or [])
    state.product_planner_message = (
        f"fallback 보고서로 진행 (사유: {source_failure[:80]})"
    )
    sr.status = "passed"
    sr.message = (
        f"기획 fallback 보고서 작성 — 후보 3, 선정={PLANNER_FALLBACK_SELECTED}"
    )
    _emit_cycle_log(
        state, "planner_fallback_used",
        f"planner fallback used: {source_failure[:160]}",
        gate_failures=list(gate_failures or [])[:8],
        selected_feature=PLANNER_FALLBACK_SELECTED,
    )


def stage_product_planning(state: CycleState) -> StageResult:
    label = next(lab for n, lab, _ in STAGES if n == "product_planning")
    sr = StageResult(name="product_planning", label=label, status="running")
    t0 = time.time()

    def _skip(reason: str) -> StageResult:
        sr.status = "skipped"
        sr.message = reason
        sr.duration_sec = round(time.time() - t0, 3)
        state.product_planner_status = "skipped"
        state.product_planner_skipped_reason = reason
        return sr

    # Pre-condition 0: publish blocker policy. If a blocker remains
    # after publish_blocker_resolve, refuse to generate new feature
    # work — fixing the deploy state has to happen first.
    if state.publish_blocked:
        return _skip(
            "차단 사유(secret/conflict)가 남아 있어 신규 개발을 중단했습니다."
        )

    if not _factory_flag_enabled("FACTORY_PRODUCT_PLANNER_MODE", default_on=True):
        return _skip("FACTORY_PRODUCT_PLANNER_MODE=false — 기획 단계 명시적 비활성")

    claude_bin = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not claude_bin:
        return _skip("claude CLI 미설치 — 스킵")

    prompt = _build_product_planner_prompt(state.goal)
    model = os.environ.get("FACTORY_CLAUDE_MODEL", "sonnet").strip() or "sonnet"
    budget_usd = os.environ.get("FACTORY_CLAUDE_BUDGET_USD", "1.0").strip() or "1.0"
    timeout_sec = float(
        os.environ.get(
            "FACTORY_CLAUDE_PLANNER_TIMEOUT_SEC",
            os.environ.get("FACTORY_CLAUDE_DISCOVERY_TIMEOUT_SEC", "600"),
        )
    )

    argv = [
        claude_bin,
        "-p", prompt,
        "--allowed-tools", "Read,Glob,Grep",
        "--output-format", "text",
        "--model", model,
        "--max-budget-usd", budget_usd,
    ]
    ok, out = _run(argv, cwd=REPO_ROOT, timeout=timeout_sec)
    sr.duration_sec = round(time.time() - t0, 3)

    if not ok:
        sr.detail = (out or "")[-1500:]
        _persist_planner_fallback(
            state, sr=sr,
            source_failure="claude CLI 실행 실패",
            gate_failures=["claude CLI 실행 실패"],
            raw_body=(out or None),
        )
        return sr

    body = (out or "").strip()
    if not body:
        _persist_planner_fallback(
            state, sr=sr,
            source_failure="claude 응답이 비어있음",
            gate_failures=["claude 응답 비어있음"],
            raw_body=None,
        )
        return sr

    HEADER = "# Stampport Product Planner Report"
    idx = body.find(HEADER)
    if idx == -1:
        sr.detail = body[:600]
        _persist_planner_fallback(
            state, sr=sr,
            source_failure="응답에 예상 헤더 없음 (# Stampport Product Planner Report)",
            gate_failures=["응답 헤더 누락"],
            raw_body=body,
        )
        return sr
    body = body[idx:].rstrip()

    # Repair-normalize: rewrite heading aliases ("장치" → "기능") to
    # the canonical form so the gate doesn't punish the LLM for using
    # the older prompt's wording. Idempotent.
    body = _normalize_planner_body(body)

    # Quality gate — refuse to advance with a half-baked plan. Instead
    # of bailing, fall back to the safe template so downstream stages
    # still proceed (the gate failures are preserved for the operator).
    gate_failures = _validate_planner_report(body)
    if gate_failures:
        sr.detail = "\n".join(f"- {r}" for r in gate_failures)
        _persist_planner_fallback(
            state, sr=sr,
            source_failure=(
                f"품질 가드 실패 ({len(gate_failures)}건)"
                f" — 첫 사유: {gate_failures[0][:80]}"
            ),
            gate_failures=gate_failures,
            raw_body=body,
        )
        return sr

    # Both filenames are kept in sync — `product_planner_report.md` is
    # the legacy reader path; `planner_proposal.md` is the ping-pong
    # canonical name. Pipeline Recovery accepts either, so writing both
    # makes the cycle robust against a partial-write recovery.
    safe_write_artifact(
        PRODUCT_PLANNER_FILE, body,
        cycle_id=state.cycle, stage="planner_proposal", source_agent="planner",
    )
    safe_write_artifact(
        PLANNER_PROPOSAL_FILE, body,
        cycle_id=state.cycle, stage="planner_proposal", source_agent="planner",
    )

    bottleneck = _extract_bottleneck(body)
    selected = _extract_selected_feature(body)
    pattern = _extract_solution_pattern(body)
    value_summary = _first_meaningful_line(
        _extract_md_section(body, "선정 이유"), max_chars=140,
    )
    llm_needed = _extract_llm_needed(body)
    data_storage = _extract_data_storage_needed(body)
    external_integration = _extract_external_integration_needed(body)
    fe_scope = _first_meaningful_line(
        _extract_md_section(body, "프론트 변경 범위"), max_chars=180,
    )
    be_scope = _first_meaningful_line(
        _extract_md_section(body, "백엔드 변경 범위"), max_chars=180,
    )
    success = _first_meaningful_line(
        _extract_md_section(body, "성공 기준"), max_chars=180,
    )
    n_candidates = _count_candidate_rows(body)

    # Active rework feature lock — when the previous cycle ended in HOLD
    # and saved a canonical feature, the planner here is REQUIRED to
    # keep that same feature. If the LLM drifted (no token overlap with
    # the locked feature), force the locked name back so all downstream
    # stages (designer/PM/ticket) work on the same target. Without this,
    # every HOLD cycle proposes a brand-new feature, the design_spec is
    # always stale, and implementation never runs — the exact symptom
    # the operator reported.
    rework_lock = _load_active_rework_feature()
    locked_feature = (rework_lock.get("feature") or "").strip()
    if locked_feature:
        state.active_rework_feature = locked_feature
        state.active_rework_hold_count = int(rework_lock.get("hold_count") or 0)
        if selected and not _features_match(selected, locked_feature):
            state.planner_feature_drift_detected = True
            state.planner_feature_drift_reason = (
                f"planner proposed '{selected}' but rework lock requires "
                f"'{locked_feature}' — overriding to keep rework on the "
                "same feature"
            )
            _emit_cycle_log(
                state, "planner_feature_drift_rejected",
                state.planner_feature_drift_reason,
                locked_feature=locked_feature,
                proposed_feature=selected,
            )
            selected = locked_feature

    state.product_planner_status = "generated"
    state.product_planner_path = str(PRODUCT_PLANNER_FILE)
    state.product_planner_at = utc_now_iso()
    state.product_planner_bottleneck = bottleneck or None
    state.product_planner_selected_feature = selected or None
    if selected:
        # Stamp the cycle-wide feature_id at planner time so every
        # downstream stage can compare ids rather than fragile names.
        state.selected_feature_id = state.selected_feature_id or _to_feature_id(selected)
    # Tentatively lock the cycle's source-of-truth feature here so
    # design_spec / ticket / propose can already compare against the
    # canonical id when planner_revision is disabled (ping-pong off).
    # planner_revision will overwrite the lock if it generates a
    # different feature later in the pipeline.
    _lock_source_of_truth(state)
    state.product_planner_solution_pattern = pattern or None
    state.product_planner_value_summary = value_summary or None
    state.product_planner_llm_needed = llm_needed or None
    state.product_planner_data_storage_needed = data_storage or None
    state.product_planner_external_integration_needed = external_integration or None
    state.product_planner_frontend_scope = fe_scope or None
    state.product_planner_backend_scope = be_scope or None
    state.product_planner_success_criteria = success or None
    state.product_planner_candidate_count = n_candidates
    state.product_planner_gate_failures = []
    state.product_planner_message = (
        f"신규 기능 기획 완료 (선정: {selected})" if selected
        else "신규 기능 기획 완료 (선정 기능 미파악)"
    )

    sr.status = "passed"
    sr.message = (
        f"제품 기획 생성 ({len(body)} chars, model={model}, 후보 {n_candidates}개"
        + (f", 병목: {bottleneck[:60]}" if bottleneck else "")
        + (f", 선정: {selected}" if selected else "")
        + ")"
    )
    return sr


# ---------------------------------------------------------------------------
# Planner ↔ Designer ping-pong (opt-in via FACTORY_PLANNER_DESIGNER_PINGPONG)
#
# Five sequential stages (designer_critique → planner_revision →
# designer_final_review → pm_decision) that consume the planner's
# proposal artifact and produce one .runtime/ Markdown each. The
# designer_final_review stage also writes a structured JSON scorecard
# (.runtime/desire_scorecard.json) which feeds the shipment gate.
#
# All four stages share the same shape: opt-in env check, prerequisite
# artifact present, run claude with Read/Glob/Grep, validate the output
# header, persist the artifact, update CycleState, surface a one-line
# message. Failures or skips never raise — they log a stage row and
# move on so the cycle still produces a report.
# ---------------------------------------------------------------------------


PINGPONG_ENV_FLAG = "FACTORY_PLANNER_DESIGNER_PINGPONG"


# ---------------------------------------------------------------------------
# Feature-flag semantics.
#
# Stampport's automation factory is an OPT-OUT product, not opt-in: the
# operator starts a cycle from Control Tower and expects the full
# pipeline (planner → designer → PM → ticket → claude_apply → QA →
# commit/push) to run. Historically each stage was gated on its own
# env flag defaulting to OFF, which left every cycle stuck at
# "skipped because flag unset".
#
# `_factory_flag_enabled(name, default_on=True)` honors the same
# `{true,1,yes,on}` enable set as before, but flips the default so that
# a *missing* env variable means ON. Explicit `false/0/no/off` still
# disables — operators running cycle.py manually for diagnostics can
# turn off any stage by setting the flag to `false`.
# ---------------------------------------------------------------------------


def _factory_flag_enabled(name: str, *, default_on: bool = True) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default_on
    return raw in {"true", "1", "yes", "on"}


def _pingpong_enabled() -> bool:
    """Honor the new opt-out semantics: ON unless explicitly disabled."""
    return _factory_flag_enabled(PINGPONG_ENV_FLAG, default_on=True)


def _read_artifact(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _ping_pong_skip(
    sr: StageResult, t0: float, reason: str, *, status_field: str, state: CycleState
) -> StageResult:
    sr.status = "skipped"
    sr.message = reason
    sr.duration_sec = round(time.time() - t0, 3)
    setattr(state, status_field, "skipped")
    setattr(state, status_field.replace("_status", "_skipped_reason"), reason)
    return sr


DESIGNER_CRITIQUE_PROMPT_TEMPLATE = """\
너는 Stampport의 디자이너(Designer) 에이전트다.

너는 단순한 UI 장식 담당이 아니다. 너는 **욕구 비평가**다.
- 기획자가 제안한 후보가 ‘갖고 싶다 / 자랑하고 싶다’를 만드는지 심사한다.
- 약하면 반드시 push back 한다. 침묵은 실패다.
- 일반 리뷰앱 / 관리자 대시보드 톤이면 즉시 반박한다.

=== Stampport Domain Profile ===
{domain_profile}
=== END Domain Profile ===

=== Agent Collaboration Doctrine ===
{collab_doc}
=== END Doctrine ===

=== 기획자 원안 (planner_proposal.md) ===
{planner_proposal}
=== END 원안 ===

심사 기준 (각 후보마다 모두 점검):
1. 일반 리뷰앱처럼 보이지 않는가?
2. 관리자 대시보드처럼 보이지 않는가?
3. 도장 / 여권 / RPG 감성이 살아 있는가?
4. 공유 카드로 올리고 싶은가?
5. 배지나 칭호가 진짜 갖고 싶어 보이는가?

도구는 Read, Glob, Grep만. 어떤 파일도 수정하지 마라.

출력은 다음 정확한 Markdown 구조만 사용한다. preamble/설명 금지:

# Stampport Designer Critique

## 전체 인상
원안이 Stampport(로컬 취향 RPG / 여권 / 도장) 정체성을 살리고 있는지 1~2문단.

## 후보별 비판
### 후보 1
- 디자인 비판: <어떤 부분이 약한가, 왜 갖고 싶지 않은가>
- 개선 방향: <어떻게 바꿔야 갖고 싶어지나>
- Figma식 UI 설명: <레이아웃/계층/간격/모션>
- 색상/레이아웃/카드/아이콘/문구 지침: <구체 토큰>
- 공유 욕구 점수: <1~5>
- 최종 판단: pass / revise / reject

### 후보 2
(같은 형식)

### 후보 3
(같은 형식)

## 다시 묻고 싶은 질문
기획자에게 추가로 묻고 싶은 3가지. 욕구 자극 관점에서.

## 추천 선정 후보
이 중 무엇을 revise해서 1개로 가져가야 하는지 + 이유 한 문단.
"""


def _build_designer_critique_prompt(planner_md: str) -> str:
    profile = _load_stampport_profile_text()
    collab = _load_agent_collab_text()
    return DESIGNER_CRITIQUE_PROMPT_TEMPLATE.format(
        domain_profile=profile or "(stampport.json 미존재)",
        collab_doc=collab or "(agent-collaboration.md 미존재)",
        planner_proposal=planner_md.strip(),
    )


def _run_pingpong_claude(
    prompt: str, expected_header: str, *, env_timeout_key: str = "FACTORY_CLAUDE_PINGPONG_TIMEOUT_SEC"
) -> tuple[bool, str]:
    """Common claude-CLI invocation for the four ping-pong stages.
    Returns (ok, body). On success the body starts at the expected
    header so callers can persist verbatim."""
    claude_bin = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not claude_bin:
        return False, "claude CLI 미설치"
    model = os.environ.get("FACTORY_CLAUDE_MODEL", "sonnet").strip() or "sonnet"
    budget_usd = os.environ.get("FACTORY_CLAUDE_BUDGET_USD", "1.0").strip() or "1.0"
    timeout_sec = float(os.environ.get(env_timeout_key, "600"))

    argv = [
        claude_bin,
        "-p", prompt,
        "--allowed-tools", "Read,Glob,Grep",
        "--output-format", "text",
        "--model", model,
        "--max-budget-usd", budget_usd,
    ]
    ok, out = _run(argv, cwd=REPO_ROOT, timeout=timeout_sec)
    if not ok:
        return False, f"claude CLI 실행 실패: {(out or '')[-400:]}"
    body = (out or "").strip()
    if not body:
        return False, "claude 응답이 비어있음"
    idx = body.find(expected_header)
    if idx == -1:
        return False, f"응답에 예상 헤더({expected_header}) 없음"
    return True, body[idx:].rstrip()


def stage_designer_critique(state: CycleState) -> StageResult:
    label = next(lab for n, lab, _ in STAGES if n == "designer_critique")
    sr = StageResult(name="designer_critique", label=label, status="running")
    t0 = time.time()

    if state.publish_blocked:
        return _ping_pong_skip(
            sr, t0, "차단 사유(secret/conflict)가 남아 있어 ping-pong 중단.",
            status_field="designer_critique_status", state=state,
        )
    if not _pingpong_enabled():
        return _ping_pong_skip(
            sr, t0, f"{PINGPONG_ENV_FLAG} 미설정 — 기본 OFF (스킵)",
            status_field="designer_critique_status", state=state,
        )
    # Accept fallback_generated too — the fallback report is by
    # construction valid for the designer to critique, and we want
    # downstream stages to keep running even when the LLM bailed.
    if state.product_planner_status not in {"generated", "fallback_generated"}:
        return _ping_pong_skip(
            sr, t0, "기획자 제안이 없어 디자이너 반박을 건너뜀",
            status_field="designer_critique_status", state=state,
        )
    planner_md = _read_artifact(PLANNER_PROPOSAL_FILE) or _read_artifact(PRODUCT_PLANNER_FILE)
    if not planner_md:
        return _ping_pong_skip(
            sr, t0, "planner_proposal.md를 읽지 못함",
            status_field="designer_critique_status", state=state,
        )

    prompt = _build_designer_critique_prompt(planner_md)
    ok, body = _run_pingpong_claude(prompt, "# Stampport Designer Critique")
    sr.duration_sec = round(time.time() - t0, 3)
    if not ok:
        sr.status = "failed"
        sr.message = body[:200]
        state.designer_critique_status = "failed"
        state.designer_critique_message = sr.message
        return sr

    safe_write_artifact(
        DESIGNER_CRITIQUE_FILE, body,
        cycle_id=state.cycle, stage="designer_critique",
        source_agent="designer",
    )
    state.designer_critique_status = "generated"
    state.designer_critique_path = str(DESIGNER_CRITIQUE_FILE)
    state.designer_critique_at = utc_now_iso()
    state.designer_critique_message = _first_meaningful_line(
        _extract_md_section(body, "전체 인상"), max_chars=160,
    )
    sr.status = "passed"
    sr.message = (
        f"디자이너 비판 생성 ({len(body)} chars)"
        + (f": {state.designer_critique_message[:80]}" if state.designer_critique_message else "")
    )
    return sr


PLANNER_REVISION_PROMPT_TEMPLATE = """\
너는 Stampport의 기획자(Product Planner) 에이전트다. 디자이너 에이전트가
원안을 강하게 반박했다. 이번 사이클에서 ship 할 후보 **1개**를 선택해
디자이너 비판을 모두 반영한 수정안을 작성한다.

=== 기획자 원안 (planner_proposal.md) ===
{planner_proposal}
=== END 원안 ===

=== 디자이너 비판 (designer_critique.md) ===
{designer_critique}
=== END 비판 ===

규칙:
- 원안의 후보 중 1개를 선택한다 (디자이너의 추천 + 자체 판단).
- 디자이너의 push back을 모두 흡수해 다시 쓴다.
- 같은 5개 욕구 중 최소 2개 이상을 자극해야 한다.
- 라벨/문구만 바꾸는 변경은 금지.

도구는 Read, Glob, Grep만 사용. 어떤 파일도 수정하지 마라.

출력은 다음 정확한 Markdown 구조만 사용한다. preamble/설명 금지:

# Stampport Planner Revision

## 선정 후보
<기능명 한 줄 — Stampport 톤의 고유 이름>

## 디자이너 비판 반영 요약
- 비판 1 → 어떻게 반영했는가
- 비판 2 → ...
- 비판 3 → ...

## 사용자 욕구 (2개 이상)
<자극하는 욕구와 그 이유>

## 핵심 루프
방문 → 스탬프 → 보상 → 다음 방문이 어떻게 이어지는지.

## MVP 구현 범위
- bullet 1
- bullet 2
- bullet 3

## 기대 행동 변화
ship 후 사용자 행동이 어떻게 달라지는지.

## 디자이너에게 다시 던질 질문
1. ...
2. ...
3. ...
"""


def _build_planner_revision_prompt(planner_md: str, critique_md: str) -> str:
    return PLANNER_REVISION_PROMPT_TEMPLATE.format(
        planner_proposal=planner_md.strip(),
        designer_critique=critique_md.strip(),
    )


def stage_planner_revision(state: CycleState) -> StageResult:
    label = next(lab for n, lab, _ in STAGES if n == "planner_revision")
    sr = StageResult(name="planner_revision", label=label, status="running")
    t0 = time.time()

    if state.publish_blocked:
        return _ping_pong_skip(
            sr, t0, "차단 사유로 ping-pong 중단",
            status_field="planner_revision_status", state=state,
        )
    if not _pingpong_enabled():
        return _ping_pong_skip(
            sr, t0, f"{PINGPONG_ENV_FLAG} 미설정 — 스킵",
            status_field="planner_revision_status", state=state,
        )
    if state.designer_critique_status != "generated":
        return _ping_pong_skip(
            sr, t0, "디자이너 비판이 없어 수정안을 작성할 수 없음",
            status_field="planner_revision_status", state=state,
        )
    planner_md = _read_artifact(PLANNER_PROPOSAL_FILE) or _read_artifact(PRODUCT_PLANNER_FILE) or ""
    critique_md = _read_artifact(DESIGNER_CRITIQUE_FILE) or ""
    if not planner_md or not critique_md:
        return _ping_pong_skip(
            sr, t0, "ping-pong 입력 아티팩트가 비어 있음",
            status_field="planner_revision_status", state=state,
        )

    prompt = _build_planner_revision_prompt(planner_md, critique_md)
    ok, body = _run_pingpong_claude(prompt, "# Stampport Planner Revision")
    sr.duration_sec = round(time.time() - t0, 3)
    if not ok:
        sr.status = "failed"
        sr.message = body[:200]
        state.planner_revision_status = "failed"
        state.planner_revision_message = sr.message
        return sr

    safe_write_artifact(
        PLANNER_REVISION_FILE, body,
        cycle_id=state.cycle, stage="planner_revision", source_agent="planner",
    )
    state.planner_revision_status = "generated"
    state.planner_revision_path = str(PLANNER_REVISION_FILE)
    state.planner_revision_at = utc_now_iso()
    revision_selected = _first_meaningful_line(
        _extract_md_section(body, "선정 후보"), max_chars=120,
    ) or None
    # Re-apply the rework feature lock here so a planner_revision that
    # drifted away from the locked feature still gets pinned back. The
    # design_spec / PM stages key off planner_revision_selected_feature
    # via current_feature, so without this clamp a drift here would
    # still cause stale_design_spec_detected on the next cycle.
    locked_for_revision = (state.active_rework_feature or "").strip()
    if locked_for_revision and revision_selected and not _features_match(
        revision_selected, locked_for_revision,
    ):
        state.planner_feature_drift_detected = True
        state.planner_feature_drift_reason = (
            (state.planner_feature_drift_reason or "")
            + f" | revision drift: '{revision_selected}' → '{locked_for_revision}'"
        ).strip(" |")
        _emit_cycle_log(
            state, "planner_revision_feature_drift_rejected",
            f"planner_revision proposed '{revision_selected}' but rework lock "
            f"requires '{locked_for_revision}' — overriding",
            locked_feature=locked_for_revision,
            proposed_feature=revision_selected,
        )
        revision_selected = locked_for_revision
    state.planner_revision_selected_feature = revision_selected
    if revision_selected:
        state.selected_feature_id = (
            state.selected_feature_id or _to_feature_id(revision_selected)
        )
    # Re-lock the cycle source-of-truth — per policy decision #3
    # planner_revision is the FINAL canonical source when it generates
    # a feature (overrides whatever product_planning tentatively
    # locked). _lock_source_of_truth resolves this naturally because
    # planner_revision_status now == "generated".
    _lock_source_of_truth(state)
    state.planner_revision_message = state.planner_revision_selected_feature or "수정안 생성"
    sr.status = "passed"
    sr.message = (
        f"기획자 수정안 생성 ({len(body)} chars)"
        + (f": {state.planner_revision_selected_feature[:80]}"
           if state.planner_revision_selected_feature else "")
    )
    return sr


DESIGNER_FINAL_REVIEW_PROMPT_TEMPLATE = """\
너는 Stampport의 디자이너(Designer) 에이전트다. 기획자의 수정안을
최종 심사한다. 욕구 점수표 6축을 1~5점으로 평가하고 각 점수의 이유를
한 줄로 적는다. 점수는 후한 인상 점수가 아니라 **냉정한 비평**이다.

=== 기획자 수정안 (planner_revision.md) ===
{planner_revision}
=== END 수정안 ===

=== 디자이너 원 비판 (designer_critique.md) ===
{designer_critique}
=== END 비판 ===

도구는 Read, Glob, Grep만. 어떤 파일도 수정하지 마라.

출력은 다음 정확한 Markdown 구조만 사용한다. preamble/설명 금지:

# Stampport Designer Final Review

## 첫인상
1~2문단으로 ‘갖고 싶은가 / 자랑하고 싶은가’ 관점에서.

## 욕구 점수표
| 축 | 점수 (1~5) | 이유 |
|---|---|---|
| Collection Score | <int> | 더 모으고 싶은 욕구를 만드는가 |
| Share Score | <int> | 인스타 스토리에 올리고 싶은가 |
| Progression Score | <int> | EXP/레벨/칭호 진행이 다음 방문을 자극하는가 |
| Rarity Score | <int> | 빈 슬롯/미획득 뱃지가 다음 행동을 자극하는가 |
| Revisit Score | <int> | 킥 포인트가 다음 방문지를 명확히 제시하는가 |
| Visual Desire Score | <int> | 도장/뱃지/카드가 진짜 갖고 싶어 보이는가 |

## 약점
한두 문단. 어디가 여전히 약한가.

## 개선 지침
- 색상/레이아웃 지침
- 카드/아이콘 지침
- 문구 지침

## 최종 판단
pass / revise / reject 중 하나만 선택. 한 문장 이유.
"""


def _build_designer_final_prompt(revision_md: str, critique_md: str) -> str:
    return DESIGNER_FINAL_REVIEW_PROMPT_TEMPLATE.format(
        planner_revision=revision_md.strip(),
        designer_critique=critique_md.strip(),
    )


# Map the 6 score-table row labels (case-insensitive substring match) to
# the canonical axis ids used in CycleState + dashboard. Stored once so
# both the parser and the gate logic share the same source of truth.
_SCORE_AXIS_LABELS: list[tuple[str, str]] = [
    ("collection score",     "collection"),
    ("share score",          "share"),
    ("progression score",    "progression"),
    ("rarity score",         "rarity"),
    ("revisit score",        "revisit"),
    ("visual desire",        "visual_desire"),
]


def _parse_desire_scorecard(body: str) -> dict[str, int]:
    """Parse the 6-axis score table out of designer_final_review.md.
    Tolerates extra whitespace / surrounding text. Returns {} on
    failure rather than raising — the caller treats that as a gate
    fail (no-score)."""
    section = _extract_md_section(body, "욕구 점수표")
    if not section:
        return {}
    out: dict[str, int] = {}
    for line in section.splitlines():
        s = line.strip()
        if not s.startswith("|") or not s.endswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2:
            continue
        label_low = cells[0].lower()
        # Skip header / separator rows.
        if "축" in cells[0] or "axis" in label_low or set(cells[0]) <= {"-", " "}:
            continue
        # Find a numeric score in the second column. Tolerate "4점", "4 / 5".
        m = re.search(r"\b([1-5])\b", cells[1])
        if not m:
            continue
        score = int(m.group(1))
        for needle, axis_id in _SCORE_AXIS_LABELS:
            if needle in label_low:
                out[axis_id] = score
                break
    return out


def _evaluate_desire_gate(scores: dict[str, int]) -> tuple[int, bool, list[str]]:
    """Return (total, ship_ready, rework_axes) based on the documented
    thresholds. ship_ready collapses *all* gate checks; rework_axes
    enumerates which specific axes tripped a re-work rule so the
    dashboard can render targeted badges."""
    if not scores:
        return 0, False, ["no_score"]
    total = sum(scores.values())
    rework: list[str] = []
    if scores.get("visual_desire", 0) < 4:
        rework.append("visual_desire")
    if scores.get("share", 0) <= 3:
        rework.append("share")
    if scores.get("revisit", 0) <= 3:
        rework.append("revisit")
    if total < 24:
        rework.append("total_below_24")
    ship_ready = (total >= 24) and not rework
    # If the only "rework" trigger was total_below_24 the rework field
    # still includes it — that's intentional, since both planner and
    # designer would need to push the loop to ≥24.
    return total, ship_ready, rework


def _extract_verdict(body: str) -> str | None:
    section = _extract_md_section(body, "최종 판단")
    if not section:
        return None
    head = section.lower()
    for kw in ("pass", "revise", "reject"):
        if kw in head:
            return kw
    return None


def stage_designer_final_review(state: CycleState) -> StageResult:
    label = next(lab for n, lab, _ in STAGES if n == "designer_final_review")
    sr = StageResult(name="designer_final_review", label=label, status="running")
    t0 = time.time()

    if state.publish_blocked:
        return _ping_pong_skip(
            sr, t0, "차단 사유로 ping-pong 중단",
            status_field="designer_final_review_status", state=state,
        )
    if not _pingpong_enabled():
        return _ping_pong_skip(
            sr, t0, f"{PINGPONG_ENV_FLAG} 미설정 — 스킵",
            status_field="designer_final_review_status", state=state,
        )
    if state.planner_revision_status != "generated":
        return _ping_pong_skip(
            sr, t0, "수정안이 없어 재평가를 건너뜀",
            status_field="designer_final_review_status", state=state,
        )

    revision_md = _read_artifact(PLANNER_REVISION_FILE) or ""
    critique_md = _read_artifact(DESIGNER_CRITIQUE_FILE) or ""
    if not revision_md:
        return _ping_pong_skip(
            sr, t0, "planner_revision.md를 읽지 못함",
            status_field="designer_final_review_status", state=state,
        )

    prompt = _build_designer_final_prompt(revision_md, critique_md)
    ok, body = _run_pingpong_claude(prompt, "# Stampport Designer Final Review")
    sr.duration_sec = round(time.time() - t0, 3)
    if not ok:
        sr.status = "failed"
        sr.message = body[:200]
        state.designer_final_review_status = "failed"
        state.designer_final_review_message = sr.message
        return sr

    safe_write_artifact(
        DESIGNER_FINAL_REVIEW_FILE, body,
        cycle_id=state.cycle, stage="designer_final_review",
        source_agent="designer",
    )
    scores = _parse_desire_scorecard(body)
    total, ship_ready, rework = _evaluate_desire_gate(scores)
    verdict = _extract_verdict(body)

    state.designer_final_review_status = "generated"
    state.designer_final_review_path = str(DESIGNER_FINAL_REVIEW_FILE)
    state.designer_final_review_at = utc_now_iso()
    state.designer_final_review_verdict = verdict
    state.desire_scorecard = dict(scores)
    state.desire_scorecard_total = total
    state.desire_scorecard_ship_ready = ship_ready
    state.desire_scorecard_rework = rework

    # Persist the structured scorecard so the PM stage + dashboard can
    # consume it without re-parsing Markdown.
    try:
        DESIRE_SCORECARD_FILE.write_text(
            json.dumps(
                {
                    "scores": scores,
                    "total": total,
                    "ship_ready": ship_ready,
                    "rework": rework,
                    "verdict": verdict,
                    "generated_at": state.designer_final_review_at,
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        state.desire_scorecard_path = str(DESIRE_SCORECARD_FILE)
    except OSError:
        state.desire_scorecard_path = None

    score_summary = (
        f"총점 {total}/30"
        + (f", verdict={verdict}" if verdict else "")
        + (f", rework={','.join(rework)}" if rework else ", ship-ready")
    )
    state.designer_final_review_message = score_summary
    sr.status = "passed"
    sr.message = f"디자이너 재평가 완료 ({score_summary})"
    return sr


# ---------------------------------------------------------------------------
# Design Implementation Spec stage
#
# Activated only when the previous cycle's PM HOLD reasons mention
# spec-mode keywords (SVG path, titleLabel, ShareCard, badges.js,
# layout, 좌표, locked, selectedTitle, 구현 명세). Emits
# .runtime/design_spec.md with concrete, validator-checked sections so
# PM can SHIP without endless abstract revisions.
# ---------------------------------------------------------------------------


DESIGN_SPEC_PROMPT_TEMPLATE = """\
너는 Stampport 의 디자이너 에이전트다. 단, 이번 호출에서는 시각 비평이 아니라
**구현 명세 확정** 만 한다.

직전 사이클에서 PM 이 다음 사유로 HOLD 했고, 다음 사이클이 반복적인 추상 논의로
흐르고 있다. 이번 사이클에 개발자가 바로 코드를 작성할 수 있는 design_spec.md 를
**한 번에 결정** 해 그 루프를 끊는다.

=== 직전 PM HOLD 결정 이유 ===
{pm_reason}
=== END ===

=== 디자이너가 지적한 약점 ===
{designer_weakness}
=== END ===

=== PM 다음 단계 담당 ===
{pm_next_owners}
=== END ===

규칙:
- Read, Glob, Grep 만 사용. 어떤 파일도 수정 금지.
- 출력은 아래 정확한 Markdown 헤딩만. preamble/설명 금지.
- SVG path 는 반드시 숫자 좌표를 포함하라. 자리표시자 (e.g. `M ... Z`) 는 거부된다.
- titleLabel 최소 13개. badges.js 와의 매핑을 1:1 로 적어라.
- 수정 대상 파일은 최소 3개. `app/web/src/...` 형태의 실제 경로.
- ShareCard 렌더 조건과 size 제약 (390×560) 은 반드시 명시.

# Stampport Design Implementation Spec

## 구현 대상 기능
- 기능명:
- 관련 PM HOLD 사유: (위 "결정 이유" 에서 1~3 줄)

## SVG Path 명세

### Tier 1 원형
- viewBox: 0 0 80 80
- 정의: `<circle cx="40" cy="40" r="30" stroke="..." fill="..." stroke-width="..." />`
- stroke / fill: (실제 색)

### Tier 2 방패
- viewBox: 0 0 80 80
- path: M10,8 L70,8 L70,48 C70,62 56,72 40,76 C24,72 10,62 10,48 Z (또는 동등 좌표)
- stroke / fill / stroke-width:

### Tier 3 왕관
- viewBox: 0 0 80 80
- path: M12,58 L18,24 L32,42 L40,16 L48,42 L62,24 L68,58 Z (또는 동등 좌표)
- stroke / fill / stroke-width:

## titleLabel 최종 목록
(badges.js 의 badge id 와 1:1 매핑. 13 개 이상.)

- cafe_starter: 카페 입문자
- cafe_lover: 카페 영사
- cafe_master: 카페 대사
- bakery_starter: 베이커리 견습관
- bakery_lover: 베이커리 영사
- bakery_master: 베이커리 대사
- restaurant_starter: 미식 입문자
- restaurant_lover: 미식 영사
- restaurant_master: 미식 대사
- dessert_starter: 디저트 입문자
- dessert_lover: 디저트 영사
- dessert_master: 디저트 대사
- traveler_starter: 동네 탐험가

(필요하면 13개 이상 추가)

## badges.js 스키마 변경
- level: 1 / 2 / 3
- tier: starter / lover / master
- titleLabel: 위 목록의 한 항목
- lockedUntilLevel: number — 사용자의 currentTitleLevel 보다 큰 level 은 잠금 슬롯으로 렌더
- currentTitleLevel 계산 방식: badges 중 selectedTitleId 의 level 또는 max(unlocked.level)

## ShareCard 레이아웃 명세
- share-title-seal 위치: share-note 블록 바로 아래 독립 블록
- share-foot 의 기존 Lv 텍스트: 제거
- "<title>까지 N곳 남음" 보조 텍스트 렌더 조건:
  - relatedBadge 가 있을 때만 표시
  - relatedBadge 가 null 이면 미렌더
- share-canvas 크기 제약:
  - max-width / width: 390px
  - max-height: 560px
  - overflow: hidden
  - share-note: line-clamp 3 / overflow-wrap break-word

## 수정 대상 파일
- app/web/src/data/badges.js
- app/web/src/screens/Badges.jsx
- app/web/src/screens/Share.jsx
- app/web/src/components/TitleSeal.jsx (또는 신규 컴포넌트)
- (선택) app/web/src/components/TitleEvolveModal.jsx — 모달 필요 여부 판단

## QA 기준
- iOS Safari 390px 에서 Lv1 / Lv3 카드가 육안으로 구분되는가
- share-note 가 100자 이상일 때 share-canvas 가 560px 를 넘지 않는가
- relatedBadge 가 null 일 때 보조 텍스트가 렌더되지 않는가
- Lv1 사용자가 tier-2 badge 를 선택했을 때 의도한 잠금 슬롯 스타일이 렌더되는가
"""


def _build_design_spec_prompt(
    pm_reason: str, designer_weakness: str, pm_next_owners: str,
) -> str:
    return DESIGN_SPEC_PROMPT_TEMPLATE.format(
        pm_reason=(pm_reason.strip() or "(직전 결정 이유 없음)"),
        designer_weakness=(designer_weakness.strip() or "(디자이너 약점 기록 없음)"),
        pm_next_owners=(pm_next_owners.strip() or "(PM 다음 단계 담당 미기재)"),
    )


# Regex used to detect at least one numeric SVG path coordinate inside
# a tier section. Any `<digit>,<digit>` pair counts; placeholders like
# `...` or empty `M ... Z` won't match.
_SVG_PATH_NUMERIC = re.compile(r"\d+\s*[, ]\s*\d+")
_TARGET_FILE_LINE = re.compile(
    r"`?\s*((?:app|control_tower|scripts)/[\w./-]+\.[\w]+)\s*`?"
)
_TITLELABEL_LINE = re.compile(r"^\s*-\s+([A-Za-z][\w]+)\s*[:：]\s+\S")
# Table separator row, e.g. "|---|---|---|" or "| --- | :---: | ---: |".
# Cells contain only dashes / colons / spaces.
_TITLELABEL_TABLE_SEPARATOR = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
# A bare badge id token, optionally backtick-wrapped: cafe_starter / `cafe_starter`.
_TITLELABEL_TABLE_ID_CELL = re.compile(r"^`?([A-Za-z][\w]+)`?$")


def _extract_design_spec_target_files(body: str) -> list[str]:
    section = _extract_md_section(body, "수정 대상 파일")
    if not section:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for m in _TARGET_FILE_LINE.finditer(section):
        path = m.group(1).strip()
        if path in seen:
            continue
        seen.add(path)
        found.append(path)
    return found


def _split_md_table_row(line: str) -> list[str]:
    """Split a single Markdown table row into cell strings.

    Tolerates leading/trailing pipes and trims surrounding whitespace.
    Empty edge cells (from leading/trailing `|`) are dropped.
    """
    s = line.strip()
    if not s.startswith("|") and "|" not in s:
        return []
    cells = [c.strip() for c in s.split("|")]
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return cells


def _detect_titlelabel_table(lines: list[str]) -> tuple[int, int, int] | None:
    """Find (separator_idx, id_col_idx, label_col_idx) for a titleLabel
    table inside a list of section lines, or None.

    A table is recognized when there is a header row containing a cell
    matching `badge id` / `id` AND a cell matching `titleLabel` / `title
    label`, immediately followed by a separator row.
    """
    n = len(lines)
    for i, raw in enumerate(lines):
        cells = _split_md_table_row(raw)
        if len(cells) < 2:
            continue
        # Locate the column indexes by header text.
        id_col = -1
        label_col = -1
        for idx, cell in enumerate(cells):
            norm = cell.strip().lower().replace("_", " ").strip("`")
            if norm in ("badge id", "id", "badge"):
                id_col = idx
            elif norm in ("titlelabel", "title label"):
                label_col = idx
            elif "titlelabel" in norm.replace(" ", ""):
                # e.g. "titleLabel (확정)" — accept any header that starts
                # with titleLabel as the label column.
                if label_col < 0:
                    label_col = idx
        if id_col < 0 or label_col < 0:
            continue
        # Next non-empty line must be a separator row.
        j = i + 1
        while j < n and not lines[j].strip():
            j += 1
        if j >= n or not _TITLELABEL_TABLE_SEPARATOR.match(lines[j]):
            continue
        return (j, id_col, label_col)
    return None


def _extract_design_spec_titlelabel_count(body: str) -> int:
    """Count the unique badge ids that appear with a non-empty
    titleLabel under '## titleLabel 최종 목록'.

    Accepts both bullet form (`- cafe_starter: 카페 입문자`) and a
    Markdown table whose header has `badge id` (or `id`) + `titleLabel`
    columns. Backtick-wrapped ids are accepted in table cells.
    """
    section = _extract_md_section(body, "titleLabel 최종 목록")
    if not section:
        return 0
    seen: set[str] = set()

    # Bullet form (existing behavior).
    for line in section.splitlines():
        m = _TITLELABEL_LINE.match(line)
        if not m:
            continue
        seen.add(m.group(1))

    # Markdown-table form. We walk only lines after the separator row.
    lines = section.splitlines()
    detected = _detect_titlelabel_table(lines)
    if detected is not None:
        sep_idx, id_col, label_col = detected
        for raw in lines[sep_idx + 1:]:
            row = raw.strip()
            if not row:
                # Blank line ends the table.
                break
            cells = _split_md_table_row(row)
            if not cells:
                break
            # A line that looks like a section break — abort.
            if row.startswith("#") or row.startswith(">"):
                continue
            if len(cells) <= max(id_col, label_col):
                continue
            id_cell = cells[id_col].strip()
            label_cell = cells[label_col].strip()
            if not id_cell or not label_cell:
                continue
            m = _TITLELABEL_TABLE_ID_CELL.match(id_cell)
            if not m:
                continue
            seen.add(m.group(1))

    return len(seen)


def _extract_design_spec_svg_paths(body: str) -> list[str]:
    """Return the names of tier sections that contain a numeric path /
    circle definition. ["Tier 1", "Tier 2", "Tier 3"] when all three are
    present and parsed."""
    svg_section = _extract_md_section(body, "SVG Path 명세")
    if not svg_section:
        return []
    out: list[str] = []
    # Each "### Tier N <name>" subsection is one tier block.
    blocks = re.split(r"^###\s+", svg_section, flags=re.MULTILINE)
    for block in blocks:
        if not block.strip():
            continue
        title_line = block.splitlines()[0].strip()
        if not title_line.lower().startswith("tier"):
            continue
        # circle.cx/cy + r counts as a numeric definition for tier 1.
        has_circle = bool(re.search(r"<circle\b[^>]*cx=", block))
        has_path_numeric = bool(_SVG_PATH_NUMERIC.search(block))
        # `M ... Z` placeholders without numbers must NOT count.
        if "..." in block and not has_path_numeric and not has_circle:
            continue
        if has_circle or has_path_numeric:
            out.append(title_line[:40])
    return out


def _extract_design_spec_feature(body: str) -> str | None:
    """Pull the design_spec's chosen feature name out of the
    "## 구현 대상 기능" block. Returns None when absent — callers
    fall back to selected_feature heuristics in that case."""
    section = _extract_md_section(body, "구현 대상 기능")
    if not section:
        return None
    for raw in section.splitlines():
        line = raw.strip().lstrip("-").strip()
        m = re.match(r"^기능명\s*[:：]\s*(.+)$", line)
        if m:
            name = m.group(1).strip()
            return name[:120] or None
    return None


# Pull cycle_id out of the artifact's HTML metadata comment written by
# safe_write_artifact. None when the comment is absent or malformed.
_ARTIFACT_CYCLE_ID_RE = re.compile(r"cycle_id:\s*([0-9]+)")


def _parse_artifact_cycle_id(body: str) -> int | None:
    if not body:
        return None
    head = body[:512]
    m = _ARTIFACT_CYCLE_ID_RE.search(head)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def _normalize_feature_name(name: str | None) -> str:
    if not name:
        return ""
    s = re.sub(r"\s+", " ", name.strip().lower())
    return s


def _features_match(a: str | None, b: str | None) -> bool:
    """Loose feature-name comparison.

    Returns True when:
      * either side is empty (callers cannot prove a mismatch yet), or
      * normalized strings are equal, or
      * ≥2 unique 2+character tokens overlap (Korean and ASCII).
    """
    sa = _normalize_feature_name(a)
    sb = _normalize_feature_name(b)
    if not sa or not sb:
        return True
    if sa == sb:
        return True
    toks_a = set(re.findall(r"[A-Za-z가-힣0-9]{2,}", sa))
    toks_b = set(re.findall(r"[A-Za-z가-힣0-9]{2,}", sb))
    if not toks_a or not toks_b:
        return False
    return len(toks_a & toks_b) >= 2


# ---------------------------------------------------------------------------
# run_id / feature_id — deterministic identifiers for the factory pipeline
#
# run_id  : a value unique to one autopilot run. Every artifact written
#           in that run records the same run_id. UI / smoke / observer
#           use it as the primary freshness key — same cycle_id but
#           different run_id means PREVIOUS RUN, not CURRENT CYCLE.
#
# feature_id : a stable slug derived from the human feature name. Stages
#           compare feature_id rather than feature_name because PMs /
#           planners often rephrase the same feature ("TitleSeal 컴포넌트"
#           vs "TitleSeal seal component") between cycles. Sluggifying
#           collapses trivial variations and refuses Korean punctuation
#           drift.
# ---------------------------------------------------------------------------

_FEATURE_ID_KEEP_RE = re.compile(r"[^a-z0-9가-힣]+")


def _to_feature_id(name: str | None) -> str:
    """Convert a feature name to a deterministic identifier.

    Lowercase, strip non-alphanumeric / non-hangul, collapse to a
    single-hyphen-separated slug, truncate at 80 characters. Returns
    "" when the input is empty or sluggifies to nothing.
    """
    if not name:
        return ""
    s = name.strip().lower()
    s = _FEATURE_ID_KEEP_RE.sub("-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:80]


def _feature_ids_match(a: str | None, b: str | None) -> bool:
    """Strict-by-id feature comparison.

    Unlike `_features_match` (which is intentionally loose for human
    naming variation), this compares the slug-form ids exactly. Empty
    on either side returns True so callers cannot turn an unknown
    feature_id into a false-positive mismatch — combine with explicit
    "neither side is empty" checks when you need a hard gate.
    """
    ia = _to_feature_id(a)
    ib = _to_feature_id(b)
    if not ia or not ib:
        return True
    return ia == ib


def _lock_source_of_truth(state: "CycleState") -> dict:
    """Lock the cycle's canonical source-of-truth feature_id.

    The kernel rule for the autopilot pipeline is "one cycle, one
    feature_id". Any drift between planner / design_spec /
    implementation_ticket / claude_proposal feature_ids is a contract
    violation surfaced as `source_of_truth_mismatch` at apply preflight.
    This helper computes the canonical feature once based on the most
    recent planner output and stamps it onto state.

    Resolution order (latest planner verdict wins; planner_revision
    overrides product_planning per policy decision #3):
      1. planner_revision_selected_feature when planner_revision_status
         == "generated"
      2. else product_planner_selected_feature when product_planner_
         status in {"generated", "fallback_generated"}
      3. else active_rework_feature carry-over (HOLD lock)

    Comparison is by `_to_feature_id` slug — never by string title —
    per policy decision #4. Subsequent stages MUST consult
    `state.source_of_truth_feature_id`, not the per-stage selected_
    feature fields, when emitting their own feature_id.

    Returns a dict {ok, code, feature, feature_id, stage} for callers
    that want to log the lock outcome.
    """
    feature: str | None = None
    stage: str | None = None
    if (
        state.planner_revision_status == "generated"
        and (state.planner_revision_selected_feature or "").strip()
    ):
        feature = state.planner_revision_selected_feature
        stage = "planner_revision"
    elif (
        state.product_planner_status in {"generated", "fallback_generated"}
        and (state.product_planner_selected_feature or "").strip()
    ):
        feature = state.product_planner_selected_feature
        stage = "product_planning"
    elif (state.active_rework_feature or "").strip():
        feature = state.active_rework_feature
        stage = "active_rework_feature"
    if not feature:
        state.source_of_truth_contract_status = "missing"
        state.source_of_truth_contract_reason = (
            "neither planner_revision nor product_planner produced a feature"
        )
        return {
            "ok": False,
            "code": "missing_source_of_truth",
            "feature": None,
            "feature_id": None,
            "stage": None,
        }
    feature = feature.strip()
    fid = _to_feature_id(feature)
    if not fid:
        state.source_of_truth_contract_status = "failed"
        state.source_of_truth_contract_reason = (
            f"feature='{feature}' did not slugify"
        )
        return {
            "ok": False,
            "code": "missing_source_of_truth",
            "feature": feature,
            "feature_id": None,
            "stage": stage,
        }
    state.source_of_truth_feature = feature
    state.source_of_truth_feature_id = fid
    state.source_of_truth_stage = stage
    state.source_of_truth_locked_at = utc_now_iso()
    state.source_of_truth_contract_status = "locked"
    state.source_of_truth_contract_reason = None
    # Mirror to the legacy selected_feature_id so older readers
    # (validate_planner_contract / scope_contract) stay aligned.
    if not state.selected_feature_id:
        state.selected_feature_id = fid
    return {
        "ok": True,
        "code": "locked",
        "feature": feature,
        "feature_id": fid,
        "stage": stage,
    }


def _resolve_run_id() -> str:
    """Return the active run_id for this cycle process.

    Resolution order:
      1. `FACTORY_RUN_ID` env var — set by autopilot.run_loop when it
         spawns factory_smoke / cycle.py. This is the authoritative
         value for autopilot-driven runs.
      2. autopilot_state.current_run_id (when present on disk and the
         autopilot status is live).
      3. A fresh local id derived from the cycle process's start time.
         Used by manual cycle.py invocations / smoke probes.

    The fresh local id format is `r-<epoch_us>-<rand4>` so it sorts
    chronologically and is trivial to grep for in artifacts.
    """
    env_val = (os.environ.get("FACTORY_RUN_ID") or "").strip()
    if env_val:
        return env_val[:64]
    try:
        ap_state_path = RUNTIME / "autopilot_state.json"
        if ap_state_path.is_file():
            ap = json.loads(ap_state_path.read_text(encoding="utf-8"))
            if isinstance(ap, dict):
                live = (ap.get("status") or "").lower() in {
                    "running", "starting", "stopping", "restarting",
                }
                rid = (ap.get("current_run_id") or "").strip()
                if live and rid:
                    return rid[:64]
    except (json.JSONDecodeError, OSError):
        pass
    rand4 = "".join(
        f"{b:02x}" for b in os.urandom(2)
    )
    return f"r-{int(time.time() * 1_000_000)}-{rand4}"


_ARTIFACT_RUN_ID_RE = re.compile(r"run_id:\s*([A-Za-z0-9_\-]+)")
_ARTIFACT_FEATURE_ID_RE = re.compile(r"feature_id:\s*([A-Za-z0-9_\-가-힣]+)")


def _parse_artifact_run_id(body: str) -> str | None:
    """Pull `run_id` out of the metadata header of a markdown artifact.

    Returns None when absent — callers should treat that as "legacy
    artifact written before run_id existed" and fall back to cycle_id /
    timestamp comparisons.
    """
    if not body:
        return None
    head = body[:512]
    m = _ARTIFACT_RUN_ID_RE.search(head)
    if not m:
        return None
    return (m.group(1) or "").strip() or None


def _parse_artifact_feature_id(body: str) -> str | None:
    if not body:
        return None
    head = body[:512]
    m = _ARTIFACT_FEATURE_ID_RE.search(head)
    if not m:
        return None
    return (m.group(1) or "").strip() or None


# ---------------------------------------------------------------------------
# Active rework feature lock
#
# When a cycle ends with `hold_for_rework`, we persist the canonical
# selected_feature into .runtime/active_rework_feature.json. The next
# cycle's planner stage reads it and refuses to drift to a new candidate
# — without this, every HOLD cycle proposes 3 brand-new ideas, the
# design_spec is always "stale" (different feature than current), and
# implementation never runs. The file is cleared automatically after
# claude_apply succeeds with a real code change.
# ---------------------------------------------------------------------------


def _load_active_rework_feature() -> dict:
    """Return the persisted rework lock or an empty dict.

    Shape: {"feature": str, "feature_id": str, "run_id": str,
            "hold_count": int, "last_hold_at": iso,
            "last_hold_type": "soft"|"hard"|None}
    """
    try:
        if not ACTIVE_REWORK_FEATURE_FILE.is_file():
            return {}
        data = json.loads(
            ACTIVE_REWORK_FEATURE_FILE.read_text(encoding="utf-8")
        ) or {}
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def _save_active_rework_feature(
    *,
    feature: str | None,
    hold_count: int,
    hold_type: str | None,
    pm_message: str | None = None,
    feature_id: str | None = None,
    run_id: str | None = None,
) -> None:
    """Persist the rework lock. A blank feature still writes the file
    (zero-feature lock has no effect downstream but keeps history).

    Includes `feature_id` (slug form) and `run_id` so the lock can be
    invalidated cleanly when a fresh autopilot run starts or when the
    accepted design_spec.feature_id diverges from the lock.
    """
    payload = {
        "feature": (feature or "").strip(),
        "feature_id": (feature_id or _to_feature_id(feature)),
        "run_id": (run_id or _resolve_run_id()),
        "hold_count": int(hold_count),
        "last_hold_at": utc_now_iso(),
        "last_hold_type": hold_type,
        "pm_message": pm_message,
    }
    safe_write_json(ACTIVE_REWORK_FEATURE_FILE, payload)


def _clear_active_rework_feature() -> bool:
    """Remove the rework lock file. Returns True when something was
    deleted (used by tests + the cycle's success path)."""
    try:
        if ACTIVE_REWORK_FEATURE_FILE.is_file():
            ACTIVE_REWORK_FEATURE_FILE.unlink()
            return True
    except OSError:
        pass
    return False


# Header-bearing markdown artifacts swept at cycle start. Each carries
# a `run_id:` field in its `<!-- stampport_artifact -->` header that we
# parse to decide whether the file belongs to the current run.
RUNTIME_SWEEP_MARKDOWN_TARGETS: tuple[Path, ...] = (
    DESIGN_SPEC_FILE,
    IMPLEMENTATION_TICKET_FILE,
    PROPOSAL_FILE,
)
# JSON artifacts swept at cycle start. The on-disk shape includes a
# top-level `run_id` field (see `_save_active_rework_feature`).
RUNTIME_SWEEP_JSON_TARGETS: tuple[Path, ...] = (
    ACTIVE_REWORK_FEATURE_FILE,
)


def _runtime_artifact_sweep(
    state: "CycleState",
) -> dict:
    """Move .runtime artifacts whose embedded run_id != current_run_id
    into `.runtime/stale_artifacts/<timestamp>/`. Same-run artifacts
    are left in place so the apply-only retry path can reuse the
    current cycle's implementation_ticket.md / claude_proposal.md.

    Pure-ish: returns a summary dict and writes only to the filesystem
    (no state mutation). The caller (stage_runtime_artifact_sweep)
    mirrors the result onto state.

    `.diff` files (claude_apply.diff, claude_apply_rolled_back.diff)
    have no header so they are intentionally NOT swept here — the
    runner / apply_preflight gate covers diff freshness separately,
    and the operator may need them as forensic evidence on failure.
    """
    cur_run = (state.run_id or _resolve_run_id() or "").strip()
    if not cur_run:
        return {
            "status": "skipped",
            "isolated": [],
            "current_run_id": None,
        }
    isolated: list[str] = []
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    dest_dir: Path | None = None

    def _ensure_dest() -> Path | None:
        nonlocal dest_dir
        if dest_dir is None:
            try:
                d = STALE_ARTIFACTS_DIR / ts
                d.mkdir(parents=True, exist_ok=True)
                dest_dir = d
            except OSError:
                return None
        return dest_dir

    for path in RUNTIME_SWEEP_MARKDOWN_TARGETS:
        try:
            if not path.is_file():
                continue
            head = path.read_text(encoding="utf-8")[:512]
        except OSError:
            continue
        rid = _parse_artifact_run_id(head)
        if not rid or rid == cur_run:
            continue
        d = _ensure_dest()
        if d is None:
            continue
        try:
            target = d / path.name
            if target.exists():
                target.unlink()
            path.rename(target)
            isolated.append(str(target))
        except OSError:
            pass

    for path in RUNTIME_SWEEP_JSON_TARGETS:
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rid = ""
        if isinstance(data, dict):
            rid = (data.get("run_id") or "").strip()
        if not rid or rid == cur_run:
            continue
        d = _ensure_dest()
        if d is None:
            continue
        try:
            target = d / path.name
            if target.exists():
                target.unlink()
            path.rename(target)
            isolated.append(str(target))
        except OSError:
            pass

    return {
        "status": "passed",
        "isolated": isolated,
        "current_run_id": cur_run,
    }


def stage_runtime_artifact_sweep(state: CycleState) -> StageResult:
    """Cycle-init hygiene step: move stale (cross-run) artifacts aside
    so design_spec / implementation_ticket / claude_propose stages
    cannot consume a previous run's output as if it were fresh."""
    label = next(
        (lab for n, lab, _ in STAGES if n == "runtime_artifact_sweep"),
        "런타임 아티팩트 정리",
    )
    sr = StageResult(name="runtime_artifact_sweep", label=label, status="running")
    t0 = time.time()
    out = _runtime_artifact_sweep(state)
    sr.duration_sec = round(time.time() - t0, 3)
    state.runtime_artifact_sweep_status = out["status"]
    state.runtime_artifact_sweep_isolated_count = len(out["isolated"])
    state.runtime_artifact_sweep_isolated_files = list(out["isolated"])
    state.runtime_artifact_sweep_current_run_id = out["current_run_id"]
    if out["status"] == "skipped":
        sr.status = "skipped"
        sr.message = "current_run_id 미확정 — sweep 스킵"
        return sr
    if out["isolated"]:
        sr.status = "passed"
        sr.message = (
            f"stale .runtime 아티팩트 {len(out['isolated'])}건 격리 → "
            f".runtime/stale_artifacts/"
        )
        _emit_cycle_log(
            state, "runtime_artifact_sweep_isolated",
            sr.message,
            isolated=out["isolated"][:8],
            current_run_id=out["current_run_id"],
        )
    else:
        sr.status = "passed"
        sr.message = "stale 아티팩트 없음 — 모든 파일 current run_id 일치"
    return sr


def _classify_design_spec_freshness(
    *,
    current_cycle_id: int,
    current_feature: str | None,
    design_spec_md: str,
) -> tuple[bool, dict]:
    """Decide whether the on-disk design_spec belongs to *this* cycle.

    Returns (is_stale, evidence) where evidence carries the parsed
    fingerprint so the caller can mirror it into factory_state.json /
    factory_smoke_state.json without re-parsing.

    A spec is considered stale only when BOTH:
      * its embedded `cycle_id` is older than the current cycle, AND
      * its `기능명` does not match the current selected feature.

    A missing fingerprint OR a missing current-feature falls back to
    "fresh" so we never block a legitimate spec_bypass on a transient
    parse failure.
    """
    evidence = {
        "spec_cycle_id": None,
        "spec_feature": None,
        "current_cycle_id": current_cycle_id,
        "current_feature": (current_feature or "").strip() or None,
        "reason": None,
    }
    if not design_spec_md:
        return False, evidence

    spec_cycle_id = _parse_artifact_cycle_id(design_spec_md)
    spec_feature = _extract_design_spec_feature(design_spec_md)
    evidence["spec_cycle_id"] = spec_cycle_id
    evidence["spec_feature"] = spec_feature

    feature_match = _features_match(current_feature, spec_feature)
    cycle_match = (
        spec_cycle_id is None
        or current_cycle_id is None
        or spec_cycle_id == current_cycle_id
    )

    if cycle_match and feature_match:
        return False, evidence

    # An older cycle_id alone isn't enough — rework cycles legitimately
    # carry the spec forward as long as the feature still matches.
    if cycle_match and not feature_match:
        evidence["reason"] = (
            f"design_spec feature='{spec_feature}' "
            f"!= current_feature='{current_feature or '(none)'}'"
        )
        return True, evidence
    if not cycle_match and not feature_match:
        evidence["reason"] = (
            f"design_spec cycle_id={spec_cycle_id} "
            f"(current={current_cycle_id}) AND feature mismatch "
            f"('{spec_feature}' vs '{current_feature or '(none)'}')"
        )
        return True, evidence
    # Different cycle but feature matches — treat as fresh-enough.
    return False, evidence


# Tokens we always recognize as schema fields when they appear in the
# design_spec's badges.js section. Used as the curated whitelist for
# scope-consistency keyword extraction so generic English words like
# "level" still get picked up (they lack camelCase / hyphen markers).
_DESIGN_SPEC_SCHEMA_FIELDS = (
    "level", "tier", "titleLabel", "lockedUntilLevel",
    "currentTitleLevel", "relatedBadge",
)


def _extract_design_spec_scope_keywords(body: str) -> list[str]:
    """Curated identifiers we can verify in claude_apply.diff to confirm
    the cycle actually built the design_spec (not a different feature).

    Pulls four classes of token:
      1. Capitalized component basenames from "## 수정 대상 파일"
         (e.g. TitleSeal from `app/web/src/components/TitleSeal.jsx`).
      2. Schema field names from "## badges.js 스키마 변경"
         (e.g. level, tier, titleLabel, lockedUntilLevel).
      3. CSS class names from "## ShareCard 레이아웃 명세"
         matching the `share-*` pattern.
      4. Pixel constants like `560px`, `390px` from any section so a
         hard-coded ShareCard size constraint shows up as a signal.

    Order is deterministic and de-duplicated.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        t = tok.strip()
        if not t:
            return
        key = t.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(t)

    for path in _extract_design_spec_target_files(body):
        base = path.rsplit("/", 1)[-1]
        name = base.rsplit(".", 1)[0] if "." in base else base
        if name and name[0].isupper() and len(name) >= 4:
            _add(name)

    badges_section = _extract_md_section(body, "badges.js 스키마 변경")
    for fixed in _DESIGN_SPEC_SCHEMA_FIELDS:
        if re.search(rf"\b{fixed}\b", badges_section):
            _add(fixed)

    sharecard_section = _extract_md_section(body, "ShareCard 레이아웃 명세")
    for tok in re.findall(r"\b(share-[a-z][a-z0-9-]{2,})\b", sharecard_section):
        _add(tok)

    for section in (sharecard_section, body):
        for tok in re.findall(r"\b(\d{2,4})\s*px\b", section):
            try:
                v = int(tok)
            except ValueError:
                continue
            if 100 <= v <= 2000:
                _add(f"{v}px")

    return out


def _check_scope_consistency(
    *,
    design_spec_md: str,
    design_spec_target_files: list[str],
    design_spec_feature: str | None,
    diff_text: str,
    changed_files: list[str],
    selected_feature: str | None,
    min_keyword_matches: int = 3,
) -> tuple[bool, str | None, list[str], int]:
    """Verify the apply diff actually built what design_spec said.

    Three gates (any failing → scope_mismatch):
      G1. changed_files ∩ design_spec_target_files ≥ 1
      G2. selected_feature aligns with design_spec_feature
      G3. claude_apply.diff contains ≥ min_keyword_matches of the
          curated scope keywords

    Returns (passed, reason_if_failed, matched_keywords, total_keywords).
    """
    spec_files_norm = {f.strip().lower() for f in design_spec_target_files if f}
    changed_norm = {f.strip().lower() for f in changed_files if f}
    if spec_files_norm and not (spec_files_norm & changed_norm):
        return (
            False,
            (
                "scope_mismatch: changed_files 가 design_spec target_files 와 "
                "교집합이 없음 — design_spec 외 다른 기능이 적용된 것으로 보임"
            ),
            [],
            0,
        )

    if (
        design_spec_feature
        and selected_feature
        and design_spec_feature.strip()
        and selected_feature.strip()
    ):
        ds = design_spec_feature.strip()
        sel = selected_feature.strip()
        if (
            ds not in sel
            and sel not in ds
            and ds.split()[0] not in sel
            and sel.split()[0] not in ds
        ):
            return (
                False,
                (
                    f"scope_mismatch: implementation_ticket selected_feature="
                    f"'{sel}' 가 design_spec 기능명='{ds}' 와 일치하지 않음"
                ),
                [],
                0,
            )

    keywords = _extract_design_spec_scope_keywords(design_spec_md)
    if not keywords:
        # No keywords to anchor on — trust the file/feature gates we
        # already passed. Mark passed but with 0/0.
        return True, None, [], 0
    haystack = (diff_text or "").lower()
    matched: list[str] = []
    for kw in keywords:
        if kw.lower() in haystack:
            matched.append(kw)
    if len(matched) < min_keyword_matches:
        return (
            False,
            (
                f"scope_mismatch: claude_apply.diff 가 design_spec 키워드 "
                f"{min_keyword_matches}개 이상을 포함하지 않음 — "
                f"매칭 {len(matched)}/{len(keywords)}"
                + (f" ({', '.join(matched[:6])})" if matched else "")
            ),
            matched,
            len(keywords),
        )
    return True, None, matched, len(keywords)


def _validate_design_spec(body: str) -> list[str]:
    """Acceptance: PM treats design_spec as SHIP-equivalent when this
    returns []. Any non-empty list keeps PM in HOLD."""
    fails: list[str] = []
    REQUIRED = (
        "구현 대상 기능",
        "SVG Path 명세",
        "titleLabel 최종 목록",
        "badges.js 스키마 변경",
        "ShareCard 레이아웃 명세",
        "수정 대상 파일",
        "QA 기준",
    )
    for h in REQUIRED:
        if not _extract_md_section(body, h):
            fails.append(f"필수 섹션 누락: ## {h}")

    svg_tiers = _extract_design_spec_svg_paths(body)
    if len(svg_tiers) < 3:
        fails.append(
            f"SVG Path tier 3종 (원형/방패/왕관) 모두에 숫자 좌표 필요 — 현재 {len(svg_tiers)}/3"
        )

    titlelabel_count = _extract_design_spec_titlelabel_count(body)
    if titlelabel_count < 13:
        fails.append(
            f"titleLabel 13개 이상 필요 — 현재 {titlelabel_count}개"
        )

    target_files = _extract_design_spec_target_files(body)
    if len(target_files) < 3:
        fails.append(
            f"수정 대상 파일 3개 이상 필요 — 현재 {len(target_files)}개"
        )

    sharecard = _extract_md_section(body, "ShareCard 레이아웃 명세")
    if not re.search(r"relatedBadge", sharecard):
        fails.append("ShareCard 보조 텍스트 렌더 조건 (relatedBadge) 명시 필요")
    if not re.search(r"560", sharecard):
        fails.append("ShareCard size 제약 (560px) 명시 필요")

    qa = _extract_md_section(body, "QA 기준")
    if len(qa) < 30:
        fails.append("QA 기준이 너무 짧음 (<30자)")

    return fails


def stage_design_spec(state: CycleState) -> StageResult:
    """Build .runtime/design_spec.md when the prior PM HOLD demanded
    a concrete implementation specification. Skipped (with reason
    `not_required`) on cycles where prior HOLD didn't trigger spec
    mode.
    """
    label = next(lab for n, lab, _ in STAGES if n == "design_spec")
    sr = StageResult(name="design_spec", label=label, status="running")
    t0 = time.time()

    def _skip(reason: str) -> StageResult:
        sr.status = "skipped"
        sr.message = reason
        sr.duration_sec = round(time.time() - t0, 3)
        state.design_spec_status = "skipped"
        state.design_spec_skipped_reason = reason
        return sr

    if state.publish_blocked:
        return _skip("차단 사유로 design_spec 보류")

    pm_md, designer_md, hold_active = _read_pm_hold_artifacts()
    if not hold_active:
        state.pm_hold_spec_mode_active = False
        return _skip("직전 PM HOLD 없음 — design_spec 미필요")

    pm_reason = _extract_md_section(pm_md, "결정 이유")
    weaknesses = _extract_md_section(designer_md, "약점")
    next_owners = _extract_md_section(pm_md, "다음 단계 담당")
    final = _extract_md_section(designer_md, "최종 판단")

    keywords = _detect_spec_mode_keywords(pm_reason, weaknesses, next_owners, final)
    state.pm_hold_spec_keywords = list(keywords)
    # Soft-HOLD signals (rework axes from the desire scorecard) also
    # warrant a design_spec — UI/감성/공유/재방문 점수 미달은 추상 기획
    # 문제가 아니라 시각/구현 명세가 필요한 케이스다. Without this, a
    # PM HOLD that fires only because Visual Desire == 3 leaves the
    # cycle stuck (no spec, no implementation) for every subsequent
    # iteration.
    soft_signals: list[str] = list(state.desire_scorecard_rework or [])
    soft_blob = (
        (pm_reason or "") + "\n" + (weaknesses or "") + "\n" + (final or "")
    )
    SOFT_HOLD_TRIGGER_TOKENS = (
        "visual_desire", "Visual Desire",
        "share", "Share",
        "revisit", "Revisit",
        "rarity", "Rarity",
        "total_below_24",
        "공유 카드", "재방문", "시각", "감성", "약점",
    )
    if not soft_signals:
        for tok in SOFT_HOLD_TRIGGER_TOKENS:
            if tok in soft_blob and tok not in soft_signals:
                soft_signals.append(tok)
    state.pm_hold_soft_signals = list(soft_signals)

    # Classify the HOLD type up-front so the skip-when-no-signal short-
    # circuit can be restricted to hard HOLD. Soft HOLD (selected_feature
    # + target_files exist; PM said hold for visual_desire/share/revisit
    # /total_below_24 etc.) MUST still produce a design_spec so the
    # rework cycle has something concrete to ship — without this, every
    # soft HOLD with no embedded spec-keyword loops forever.
    if not state.pm_hold_type:
        ht_ds, hr_ds = _classify_pm_hold_type(state)
        state.pm_hold_type = ht_ds
        state.pm_hold_type_reason = hr_ds

    if not keywords and not soft_signals:
        if state.pm_hold_type == "soft":
            # Soft HOLD without explicit signals — still proceed. Mark
            # an implicit signal so spec_mode gating downstream knows
            # the spec was generated to break the loop, not because a
            # spec-keyword was matched.
            soft_signals = ["soft_hold_default"]
            state.pm_hold_soft_signals = list(soft_signals)
        else:
            state.pm_hold_spec_mode_active = False
            return _skip(
                "PM HOLD (hard) — design_spec 미필요 "
                f"(spec-mode keyword / soft-hold 신호 없음, "
                f"hold_reason={state.pm_hold_type_reason or '—'})"
            )
    state.pm_hold_spec_mode_active = True

    claude_bin = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not claude_bin:
        return _skip("claude CLI 미설치 — design_spec 스킵")

    prompt = _build_design_spec_prompt(pm_reason, weaknesses, next_owners)
    model = os.environ.get("FACTORY_CLAUDE_MODEL", "sonnet").strip() or "sonnet"
    budget_usd = os.environ.get("FACTORY_CLAUDE_BUDGET_USD", "1.0").strip() or "1.0"
    timeout_sec = float(
        os.environ.get(
            "FACTORY_CLAUDE_DESIGN_SPEC_TIMEOUT_SEC",
            os.environ.get("FACTORY_CLAUDE_DISCOVERY_TIMEOUT_SEC", "600"),
        )
    )
    argv = [
        claude_bin,
        "-p", prompt,
        "--allowed-tools", "Read,Glob,Grep",
        "--output-format", "text",
        "--model", model,
        "--max-budget-usd", budget_usd,
    ]
    ok, out = _run(argv, cwd=REPO_ROOT, timeout=timeout_sec)
    sr.duration_sec = round(time.time() - t0, 3)

    if not ok:
        sr.status = "failed"
        sr.message = (out or "")[-200:]
        sr.detail = (out or "")[-1500:]
        state.design_spec_status = "failed"
        state.design_spec_message = sr.message
        return sr

    body = (out or "").strip()
    HEADER = "# Stampport Design Implementation Spec"
    idx = body.find(HEADER)
    if idx == -1:
        sr.status = "failed"
        sr.message = "응답에 예상 헤더 없음 (# Stampport Design Implementation Spec)"
        sr.detail = body[:600]
        state.design_spec_status = "failed"
        state.design_spec_message = sr.message
        return sr
    body = body[idx:].rstrip()

    fails = _validate_design_spec(body)
    target_files = _extract_design_spec_target_files(body)
    titlelabel_count = _extract_design_spec_titlelabel_count(body)
    svg_tiers = _extract_design_spec_svg_paths(body)

    # Resolve the spec's canonical feature + feature_id BEFORE writing,
    # so the artifact header carries the same id every downstream stage
    # will see. The Cycle Source-of-Truth Contract makes the planner
    # (or planner_revision) the canonical source of feature_id; the
    # design_spec stage is a CONSUMER, not the SoT. If the LLM
    # reframed the feature under a different name, we hard-fail the
    # spec with `failed_scope_mismatch` so the next cycle's planner /
    # design_spec retry can converge — claude_apply must never try to
    # apply a spec under a different feature_id than the cycle's SoT.
    ds_feature_name = _extract_design_spec_feature(body)
    ds_feature_id = _to_feature_id(ds_feature_name)
    sot_fid = (state.source_of_truth_feature_id or "").strip()
    if sot_fid and ds_feature_id and ds_feature_id != sot_fid:
        # Persist the mismatch on state so the smoke report and the
        # source_of_truth validator can both render the divergence,
        # but DO NOT write design_spec.md to disk under the wrong
        # feature_id — runtime artifact sweep would just have to
        # isolate it on the next cycle.
        state.design_spec_status = "failed_scope_mismatch"
        state.design_spec_acceptance_passed = False
        state.design_spec_acceptance_failures = [
            f"design_spec feature_id='{ds_feature_id}' "
            f"!= source_of_truth_feature_id='{sot_fid}'"
        ]
        state.design_spec_feature = ds_feature_name or state.design_spec_feature
        state.design_spec_feature_id = ds_feature_id
        state.design_spec_message = (
            f"design_spec feature_id mismatch — "
            f"SoT={sot_fid}, spec={ds_feature_id} (격리)"
        )
        sr.status = "failed"
        sr.message = state.design_spec_message
        sr.detail = state.design_spec_acceptance_failures[0]
        _emit_cycle_log(
            state, "design_spec_scope_mismatch",
            state.design_spec_message,
            source_of_truth_feature_id=sot_fid,
            design_spec_feature_id=ds_feature_id,
        )
        # Move any prior spec aside so it cannot be read as current.
        moved = _move_stale_artifact_aside(DESIGN_SPEC_FILE)
        if moved:
            _emit_cycle_log(
                state, "design_spec_prior_moved",
                f"prior design_spec.md moved aside → {moved}",
            )
        return sr
    safe_write_artifact(
        DESIGN_SPEC_FILE, body,
        cycle_id=state.cycle, stage="design_spec", source_agent="designer",
        feature_id=ds_feature_id or sot_fid or None,
        extra={
            "spec_mode_keywords": ",".join(keywords)[:200],
            "acceptance": "passed" if not fails else "insufficient",
            "source_of_truth_feature_id": sot_fid or "—",
        },
    )
    state.design_spec_path = str(DESIGN_SPEC_FILE)
    state.design_spec_at = utc_now_iso()
    state.design_spec_target_files = list(target_files)
    state.design_spec_titlelabel_count = titlelabel_count
    state.design_spec_svg_paths = list(svg_tiers)
    state.design_spec_acceptance_passed = not fails
    state.design_spec_acceptance_failures = list(fails)
    state.design_spec_feature = ds_feature_name or state.design_spec_feature
    state.design_spec_feature_id = ds_feature_id or state.design_spec_feature_id

    if fails:
        state.design_spec_status = "insufficient"
        state.design_spec_message = (
            f"design_spec 작성됨 — 수용 기준 미달 ({len(fails)}건)"
        )
        sr.status = "passed"  # the artifact is on disk; PM will HOLD again.
        sr.message = state.design_spec_message
        sr.detail = "\n".join(f"- {f}" for f in fails)
        _emit_cycle_log(
            state, "design_spec_insufficient",
            f"design_spec produced but acceptance failed ({len(fails)}건)",
            keywords=keywords[:8],
            failures=fails[:8],
        )
        return sr

    state.design_spec_status = "generated"
    state.design_spec_message = (
        f"design_spec 작성 완료 — SVG {len(svg_tiers)}/3, "
        f"titleLabel {titlelabel_count}개, target_files {len(target_files)}개"
    )
    sr.status = "passed"
    sr.message = state.design_spec_message
    _emit_cycle_log(
        state, "design_spec_generated",
        state.design_spec_message,
        keywords=keywords[:8],
        target_files=target_files[:20],
    )
    return sr


PM_DECISION_PROMPT_TEMPLATE = """\
너는 Stampport의 PM 에이전트다. 기획자–디자이너 ping-pong이 끝났다.
욕구 점수표 결과를 토대로 이번 사이클에 ship 할지 여부와 출하 단위를 결정한다.

=== 기획자 수정안 (planner_revision.md) ===
{planner_revision}
=== END 수정안 ===

=== 디자이너 최종 평가 (designer_final_review.md) ===
{designer_final_review}
=== END 평가 ===

=== Desire Scorecard (JSON) ===
{scorecard_json}
=== END Scorecard ===

{design_spec_block}

출하 기준 (반드시 준수):
- 총점 ≥ 24 → ship 후보
- Visual Desire Score ≥ 4 → 통과 (미달 시 디자이너 재작업)
- Share Score ≥ 4 → 통과 (3 이하면 공유 카드 개선 필요)
- Revisit Score ≥ 4 → 통과 (3 이하면 기획자 재작업)

design_spec 우회 규칙:
- `.runtime/design_spec.md` 가 존재하고 acceptance 가 통과(SVG 3종 숫자 좌표,
  titleLabel ≥ 13, 수정 대상 파일 ≥ 3, ShareCard 렌더 조건 명시, QA 기준 명시)
  하면 욕구 점수 미달이라도 ship 으로 판단할 수 있다 — 이는 직전 HOLD 가
  '구현 명세 부족' 을 사유로 삼은 경우 추상 논의를 끊기 위한 우회 게이트다.
- design_spec acceptance 가 실패면 점수 게이트와 동일하게 hold.

도구는 Read, Glob, Grep만. 어떤 파일도 수정하지 마라.

출력은 다음 정확한 Markdown 구조만 사용한다. preamble/설명 금지:

# Stampport PM Decision

## 출하 결정
ship / hold (재작업 후 다음 사이클) 중 하나만.

## 결정 이유
욕구 점수표 결과를 토대로 한 문단.

## 출하 단위 (가장 작은)
- bullet 1
- bullet 2
- bullet 3

## 다음 단계 담당
- 디자이너: <re-work 필요 시 어떤 부분을 다시 그리나, 아니면 'N/A'>
- 기획자: <revisit/share rework 필요 시 무엇을 다시 설계, 아니면 'N/A'>
- 프론트/백엔드: <ship 결정일 때만 작업 지시. 그 외 'N/A'>

## QA가 추가로 점검할 것
- 기능 게이트 외 욕구 점수 검증 항목 1~3개
"""


def _build_pm_decision_prompt(
    revision_md: str,
    final_review_md: str,
    scorecard: dict,
    *,
    design_spec_md: str = "",
    design_spec_acceptance_passed: bool = False,
    design_spec_failures: list[str] | None = None,
) -> str:
    if design_spec_md:
        verdict = "PASSED" if design_spec_acceptance_passed else "INSUFFICIENT"
        fail_block = ""
        if design_spec_failures:
            fail_block = "\n실패 항목:\n" + "\n".join(
                f"- {f}" for f in design_spec_failures[:8]
            )
        block = (
            f"=== Design Spec (.runtime/design_spec.md) — acceptance: {verdict} ==="
            + fail_block
            + "\n"
            + design_spec_md.strip()
            + "\n=== END Design Spec ==="
        )
    else:
        block = "=== Design Spec === (이번 사이클은 design_spec 미작성 — 일반 점수 게이트만 적용)"
    return PM_DECISION_PROMPT_TEMPLATE.format(
        planner_revision=revision_md.strip(),
        designer_final_review=final_review_md.strip(),
        scorecard_json=json.dumps(scorecard, ensure_ascii=False, indent=2),
        design_spec_block=block,
    )


def _decide_pm_ship(
    *,
    decision_section: str,
    score_gate_ok: bool,
    design_spec_status: str | None,
    design_spec_acceptance_passed: bool,
    spec_bypass_eligible: bool = True,
) -> tuple[bool, bool]:
    """Decide whether the PM verdict should ship, and whether design_spec
    acceptance is the reason it's allowed to.

    Returns (pm_ship, spec_bypass).

    spec_bypass is True only when the cycle wrote a design_spec.md whose
    acceptance gate passed AND the spec is fresh for the current cycle
    (caller passes ``spec_bypass_eligible=False`` when the spec is stale).
    That's the bypass path that lets PM ship even when desire scores
    didn't recover yet (avoids the abstract rework-loop trap documented
    in docs/factory-smoke.md), but it must never be unlocked by a
    leftover spec from a previous cycle's different feature.
    """
    decision = (decision_section or "").lower()
    ship_word = "ship" in decision
    hold_word = "hold" in decision
    spec_bypass = (
        bool(spec_bypass_eligible)
        and design_spec_status == "generated"
        and bool(design_spec_acceptance_passed)
    )
    pm_ship = ship_word and not hold_word and (score_gate_ok or spec_bypass)
    return pm_ship, spec_bypass


def stage_pm_decision(state: CycleState) -> StageResult:
    label = next(lab for n, lab, _ in STAGES if n == "pm_decision")
    sr = StageResult(name="pm_decision", label=label, status="running")
    t0 = time.time()

    if state.publish_blocked:
        return _ping_pong_skip(
            sr, t0, "차단 사유로 ping-pong 중단",
            status_field="pm_decision_status", state=state,
        )
    if not _pingpong_enabled():
        return _ping_pong_skip(
            sr, t0, f"{PINGPONG_ENV_FLAG} 미설정 — 스킵",
            status_field="pm_decision_status", state=state,
        )
    if state.designer_final_review_status != "generated":
        return _ping_pong_skip(
            sr, t0, "디자이너 재평가가 없어 PM 결정을 건너뜀",
            status_field="pm_decision_status", state=state,
        )

    revision_md = _read_artifact(PLANNER_REVISION_FILE) or ""
    final_review_md = _read_artifact(DESIGNER_FINAL_REVIEW_FILE) or ""
    design_spec_md = _read_artifact(DESIGN_SPEC_FILE) or ""
    scorecard = {
        "scores": dict(state.desire_scorecard),
        "total": state.desire_scorecard_total,
        "ship_ready": state.desire_scorecard_ship_ready,
        "rework": list(state.desire_scorecard_rework),
        "verdict": state.designer_final_review_verdict,
    }

    # ------------------------------------------------------------------
    # Stale design_spec gate.
    #
    # If .runtime/design_spec.md belongs to an older cycle whose feature
    # disagrees with this cycle's selected feature, exclude the spec
    # body from the PM prompt and forbid spec_bypass. Otherwise an old
    # "TitleSeal" spec drags a brand-new "PNG Share" cycle into the same
    # HOLD loop that produced the stale spec in the first place.
    # ------------------------------------------------------------------
    current_feature = (
        state.planner_revision_selected_feature
        or state.product_planner_selected_feature
        or state.selected_feature
        or ""
    )
    state.current_cycle_feature = current_feature or None
    spec_stale, spec_evidence = _classify_design_spec_freshness(
        current_cycle_id=state.cycle,
        current_feature=current_feature,
        design_spec_md=design_spec_md,
    )
    state.stale_design_spec_detected = bool(spec_stale)
    state.stale_design_spec_feature = spec_evidence.get("spec_feature")
    state.stale_design_spec_cycle_id = spec_evidence.get("spec_cycle_id")
    state.stale_design_spec_reason = spec_evidence.get("reason")

    if spec_stale:
        # Surface the diagnostic so smoke_report / dashboard can show
        # "stale design_spec" without the PM HOLD reason getting
        # contaminated with TitleSeal-shaped content from a different
        # cycle.
        _emit_cycle_log(
            state, "stale_design_spec_isolated",
            (
                f"이전 사이클 design_spec ('{spec_evidence.get('spec_feature') or '—'}', "
                f"cycle_id={spec_evidence.get('spec_cycle_id')}) 이 현재 평가 대상 "
                f"'{current_feature or '(미정)'}' 과 달라 PM 프롬프트에서 제외했습니다."
            ),
            spec_feature=spec_evidence.get("spec_feature"),
            spec_cycle_id=spec_evidence.get("spec_cycle_id"),
            current_feature=current_feature,
        )
        prompt_design_spec_md = ""
        prompt_design_spec_passed = False
        prompt_design_spec_failures: list[str] = []
    else:
        prompt_design_spec_md = design_spec_md
        prompt_design_spec_passed = state.design_spec_acceptance_passed
        prompt_design_spec_failures = list(state.design_spec_acceptance_failures)

    prompt = _build_pm_decision_prompt(
        revision_md,
        final_review_md,
        scorecard,
        design_spec_md=prompt_design_spec_md,
        design_spec_acceptance_passed=prompt_design_spec_passed,
        design_spec_failures=prompt_design_spec_failures,
    )
    ok, body = _run_pingpong_claude(prompt, "# Stampport PM Decision")
    sr.duration_sec = round(time.time() - t0, 3)
    if not ok:
        sr.status = "failed"
        sr.message = body[:200]
        state.pm_decision_status = "failed"
        state.pm_decision_message = sr.message
        return sr

    safe_write_artifact(
        PM_DECISION_FILE, body,
        cycle_id=state.cycle, stage="pm_decision", source_agent="pm",
    )
    decision_section = _extract_md_section(body, "출하 결정")
    score_gate_ok = state.desire_scorecard_ship_ready
    pm_ship, spec_bypass = _decide_pm_ship(
        decision_section=decision_section,
        score_gate_ok=score_gate_ok,
        design_spec_status=state.design_spec_status,
        design_spec_acceptance_passed=state.design_spec_acceptance_passed,
        spec_bypass_eligible=not state.stale_design_spec_detected,
    )

    state.pm_decision_status = "generated"
    state.pm_decision_path = str(PM_DECISION_FILE)
    state.pm_decision_at = utc_now_iso()
    state.pm_decision_ship_ready = pm_ship
    bypass_tag = " · spec_bypass" if (pm_ship and spec_bypass and not score_gate_ok) else ""
    summary = (
        ("SHIP" if pm_ship else "HOLD")
        + f" (총점 {state.desire_scorecard_total}/30"
        + (f", rework={','.join(state.desire_scorecard_rework)}"
           if state.desire_scorecard_rework else "")
        + bypass_tag
        + ")"
    )
    state.pm_decision_message = summary
    sr.status = "passed"
    sr.message = f"PM 결정 완료 — {summary}"
    return sr


# ---------------------------------------------------------------------------
# Claude Code patch proposal stage (opt-in)
# ---------------------------------------------------------------------------


CLAUDE_PROPOSAL_PROMPT_TEMPLATE = """\
당신은 Stampport 프로젝트의 한 사이클 개선 제안을 작성하는 Claude Code 입니다.

이번 사이클의 개선 목표:
{goal}

규칙:
- 어떤 파일도 수정하지 마세요. 사용 가능한 도구는 Read, Glob, Grep 뿐입니다.
- 출력은 아래에 명시한 섹션 헤더 그대로의 한국어 Markdown 한 문서입니다.
- 한 사이클에서 적용할 단 하나의 구체적 개선만 제안하세요. 여러 안을 나열하지 마세요.
- 200줄 이하의 코드 변경으로 구현 가능한 범위여야 합니다.
- 가능한 곳마다 파일 경로와 줄 번호로 근거를 인용하세요.
- secret/private key/token이 있더라도 그 값을 출력하지 마세요. 파일 경로만 언급하세요.

다음 정확한 구조의 Markdown만 출력하세요. preamble/설명 금지:

# Stampport Claude 패치 제안

## 개선 목표
(이번 사이클 목표를 한 문단으로)

## 현재 문제
(코드에서 관찰한 구체적 문제 1~3개. `path/to/file:line` 형식으로 인용)

## 수정 제안
(어떻게 고칠지. 의사코드 스니펫 가능. 한 가지 안만)

## 변경 대상 파일
- `path/to/file.py` — 무엇을 바꿀지 한 줄
- ...

## 예상 위험
(이 변경이 깨뜨릴 수 있는 것 1~3개)

## 검증 방법
(빌드/문법/사용자 시나리오로 어떻게 확인할지)

## 적용 여부 판단 기준
(자동 적용 OK 조건과 reject 조건)
"""


CLAUDE_PROPOSAL_PLANNER_PROMPT_TEMPLATE = """\
당신은 Stampport 프로젝트의 한 사이클 개선 제안을 작성하는 Claude Code 입니다.

⚠️ 이번 사이클은 Product Planner Mode 가 켜져 있습니다.
직전 stage에서 다음 제품 기획 리포트가 자동 생성·검증되었습니다. 이 안의
"이번 사이클 선정 기능" 1개만 제안 대상이며, "이번 사이클 MVP 범위" 와
"프론트/백엔드 변경 범위" 안에서만 변경을 제안할 수 있습니다.

=== START Product Planner Report ===
{planner}
=== END Product Planner Report ===

추가 규칙 (Product Planner Mode 전용):
- 위 리포트의 "이번 사이클 선정 기능" / "MVP 범위" 외의 임의 수정 금지.
- "이번 사이클에서 하지 않을 것"에 적힌 항목은 절대 건드리지 마세요.
- 주석 추가/문구 변경/라벨 교체만 하는 제안 금지 — 새 component, 새 API
  필드, 새 함수, localStorage/state 추가처럼 사용자가 보거나 호출할 수 있는
  변경을 제안하세요.
- LLM 필요 여부가 "필요"이면 fallback 동작도 명시한 제안을 하세요.

이번 사이클의 더 큰 목표 (참고용):
{goal}

기본 규칙:
- 어떤 파일도 수정하지 마세요. 사용 가능한 도구는 Read, Glob, Grep 뿐입니다.
- 출력은 아래에 명시한 섹션 헤더 그대로의 한국어 Markdown 한 문서입니다.
- 한 사이클에서 적용할 단 하나의 구체적 개선만 제안하세요. 여러 안을 나열하지 마세요.
- 200줄 이하의 코드 변경으로 구현 가능한 범위여야 합니다.
- 가능한 곳마다 파일 경로와 줄 번호로 근거를 인용하세요.
- secret/private key/token이 있더라도 그 값을 출력하지 마세요. 파일 경로만 언급하세요.

다음 정확한 구조의 Markdown만 출력하세요. preamble/설명 금지:

# Stampport Claude 패치 제안

## 개선 목표
(선정 기능 이름 + 이번 사이클에서 어떻게 그 기능을 처음 만들 것인지)

## 현재 문제
(현재 코드에서 그 기능이 없거나 미흡한 지점. `path:line` 인용)

## 수정 제안
(선정 기능을 구현하기 위한 단 하나의 안. 의사코드 스니펫 가능)

## 변경 대상 파일
- `path/to/file.py` — 무엇을 바꿀지 한 줄
- ...

## 예상 위험
(이 변경이 깨뜨릴 수 있는 것 1~3개)

## 검증 방법
(빌드/문법/사용자 시나리오로 어떻게 확인할지)

## 적용 여부 판단 기준
(자동 적용 OK 조건과 reject 조건)
"""


def _build_claude_proposal_prompt(
    goal: str, *, planner: str | None = None,
) -> str:
    """Pick the right template based on whether Product Planner Mode
    produced a validated report for this cycle. Planner-mode prompt
    embeds the full report and constrains claude to the selected
    feature's documented MVP scope."""
    if planner and planner.strip():
        return CLAUDE_PROPOSAL_PLANNER_PROMPT_TEMPLATE.format(
            goal=goal.strip() or DEFAULT_GOAL,
            planner=planner.strip(),
        )
    return CLAUDE_PROPOSAL_PROMPT_TEMPLATE.format(goal=goal.strip() or DEFAULT_GOAL)


def stage_claude_propose(state: CycleState) -> StageResult:
    label = next(lab for n, lab, _ in STAGES if n == "claude_propose")
    sr = StageResult(name="claude_propose", label=label, status="running")
    t0 = time.time()

    def _skip(reason: str) -> StageResult:
        sr.status = "skipped"
        sr.message = reason
        sr.duration_sec = round(time.time() - t0, 3)
        state.claude_proposal_status = "skipped"
        state.claude_proposal_skipped_reason = reason
        return sr

    # Pre-condition 0: publish blocker policy. We refuse to ask Claude
    # to propose new code on top of an unpublishable working tree.
    if state.publish_blocked:
        return _skip(
            "차단 사유(secret/conflict)가 남아 있어 신규 개발을 중단했습니다."
        )

    # Pre-condition 1: opt-out. Default ON — Stampport's automation
    # factory is meant to ship code, so every cycle should reach the
    # claude_propose stage. The operator can disable explicitly with
    # FACTORY_RUN_CLAUDE=false for diagnostic-only cycles.
    if not _factory_flag_enabled("FACTORY_RUN_CLAUDE", default_on=True):
        return _skip("FACTORY_RUN_CLAUDE=false — Claude 호출 명시적 비활성")

    # Pre-condition 1b: PM HOLD gate. When the PM-decision verdict is
    # "hold" (재작업 필요) we MUST NOT advance to development stages —
    # claude_propose, implementation_ticket, claude_apply are all
    # skipped. The operator can opt out with
    # FACTORY_ALLOW_PM_HOLD_TO_IMPLEMENT=true for cases where the
    # planner-rework loop is broken and we want to force forward
    # progress anyway.
    #
    # Spec-mode override: a passed design_spec is the implementation
    # contract — let claude_propose run even on PM HOLD so the rework
    # cycle can actually ship the spec.
    spec_acceptance_bypass = bool(
        state.design_spec_status == "generated"
        and state.design_spec_acceptance_passed
        and not state.stale_design_spec_detected
    )
    pm_hold = (
        state.pm_decision_status == "generated"
        and not state.pm_decision_ship_ready
    )
    # claude_propose only skips on hard HOLD. Soft HOLD (selected_feature
    # and target_files exist) is allowed to advance — without this the
    # propose / apply pipeline never runs and the rework loop spins
    # forever on the same HOLD. We classify here too because
    # claude_propose runs BEFORE implementation_ticket in the pipeline.
    if pm_hold and not state.pm_hold_type:
        ht, hr = _classify_pm_hold_type(state)
        state.pm_hold_type = ht
        state.pm_hold_type_reason = hr
    soft_hold_bypass_propose = bool(pm_hold and state.pm_hold_type == "soft")
    if (
        pm_hold
        and not spec_acceptance_bypass
        and not soft_hold_bypass_propose
        and not _factory_flag_enabled(
            "FACTORY_ALLOW_PM_HOLD_TO_IMPLEMENT", default_on=False,
        )
    ):
        return _skip(
            f"PM HOLD (hard) — 재작업 사이클이라 Claude 제안 건너뜀 "
            f"(사유: {state.pm_hold_type_reason or '—'})"
        )

    # Pre-condition 2: don't ask Claude to propose changes when the
    # working tree is leaking secrets / build artifacts.
    if state.risky_files:
        return _skip(
            f"위험 파일 {len(state.risky_files)}건 감지 — Claude 제안 건너뜀"
        )

    # Pre-condition 3: don't propose on top of a broken build/syntax.
    failed_prior = [
        s for s in state.stages
        if s.status == "failed" and s.name in {"build_app", "build_control", "syntax_check", "git_check"}
    ]
    if failed_prior:
        names = ", ".join(s.name for s in failed_prior)
        return _skip(f"이전 단계 실패({names}) — Claude 제안 건너뜀")

    # Pre-condition 4: claude CLI must be installed.
    claude_bin = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not claude_bin:
        return _skip("claude CLI 미설치 — 스킵")

    # When the design_spec is the cycle's source of truth (accepted +
    # not stale), we feed the design_spec body itself into the proposal
    # prompt instead of the product_planner output. Cycle Source-of-
    # Truth Contract layer: even when the spec is on disk, we refuse
    # to feed it into the prompt if its feature_id diverges from the
    # locked SoT — that would just produce a proposal under the wrong
    # feature_id and trip the apply_preflight on the next stage.
    sot_fid_propose = (state.source_of_truth_feature_id or "").strip()
    spec_for_propose = bool(
        state.design_spec_status == "generated"
        and state.design_spec_acceptance_passed
        and not state.stale_design_spec_detected
        and DESIGN_SPEC_FILE.is_file()
        and (
            not sot_fid_propose
            or _feature_ids_match(
                state.design_spec_feature_id, sot_fid_propose,
            )
        )
    )
    planner_md: str | None = None
    if spec_for_propose:
        try:
            planner_md = DESIGN_SPEC_FILE.read_text(encoding="utf-8")
        except OSError:
            planner_md = None
    if planner_md is None and (
        state.product_planner_status in {"generated", "fallback_generated"}
        and PRODUCT_PLANNER_FILE.is_file()
    ):
        try:
            planner_md = PRODUCT_PLANNER_FILE.read_text(encoding="utf-8")
        except OSError:
            planner_md = None

    # Build prompt + invoke. Read-only tools only; no Edit/Write/Bash.
    # The output is captured and we (cycle.py) write it to the file —
    # so Claude never has filesystem write access.
    prompt = _build_claude_proposal_prompt(state.goal, planner=planner_md)
    model = os.environ.get("FACTORY_CLAUDE_MODEL", "sonnet").strip() or "sonnet"
    # Stage-aware budget — claude_propose uses CLAUDE_PROPOSE_MAX_COST_USD
    # (default 1.00). FACTORY_CLAUDE_BUDGET_USD remains a fallback so
    # operators with the legacy env var keep working.
    budget_usd, _budget_source = get_claude_budget_usd("claude_propose")
    timeout_sec = float(os.environ.get("FACTORY_CLAUDE_TIMEOUT_SEC", "600"))

    argv = [
        claude_bin,
        "-p", prompt,
        "--allowed-tools", "Read,Glob,Grep",
        "--output-format", "text",
        "--model", model,
        "--max-budget-usd", budget_usd,
    ]
    ok, out = _run(argv, cwd=REPO_ROOT, timeout=timeout_sec)
    sr.duration_sec = round(time.time() - t0, 3)

    if not ok:
        sr.status = "failed"
        sr.message = "claude CLI 실행 실패"
        sr.detail = out[-1500:]
        state.claude_proposal_status = "failed"
        state.claude_proposal_skipped_reason = sr.message
        return sr

    body = (out or "").strip()
    if not body:
        sr.status = "failed"
        sr.message = "claude가 빈 응답을 반환"
        state.claude_proposal_status = "failed"
        state.claude_proposal_skipped_reason = sr.message
        return sr

    # Find our required heading anywhere in the response and slice from
    # there — Claude sometimes prepends a sentence like "Here's the
    # proposal:" before the markdown, which is harmless to drop. If the
    # heading isn't present at all, that means Claude refused or got
    # confused; flag and don't overwrite the file.
    HEADER = "# Stampport Claude 패치 제안"
    idx = body.find(HEADER)
    if idx == -1:
        sr.status = "failed"
        sr.message = "응답에 예상 헤더가 없음"
        sr.detail = body[:600]
        state.claude_proposal_status = "failed"
        state.claude_proposal_skipped_reason = sr.message
        return sr
    body = body[idx:].rstrip()

    # SoT precedence: the locked source-of-truth feature_id ALWAYS
    # wins. design_spec_feature_id is the per-stage representation;
    # planner candidates are the legacy fallback. Without this, a
    # spec_anchored proposal could carry the spec's feature_id when
    # SoT had already converged on a different planner-revision
    # feature, and apply_preflight would fail with
    # source_of_truth_mismatch downstream.
    propose_feature_id = (
        sot_fid_propose
        or (
            state.design_spec_feature_id
            if spec_for_propose
            else state.selected_feature_id
        )
        or _to_feature_id(
            state.design_spec_feature
            if spec_for_propose
            else (
                state.planner_revision_selected_feature
                or state.product_planner_selected_feature
            )
        )
    )
    safe_write_artifact(
        PROPOSAL_FILE, body,
        cycle_id=state.cycle, stage="claude_propose",
        source_agent="claude_propose",
        feature_id=propose_feature_id or None,
        extra={
            "spec_anchored": "true" if spec_for_propose else "false",
            "source_of_truth_feature_id": sot_fid_propose or "—",
        },
    )
    state.claude_proposal_status = "generated"
    state.claude_proposal_path = str(PROPOSAL_FILE)
    state.claude_proposal_at = utc_now_iso()
    state.claude_proposal_feature_id = propose_feature_id or None
    state.claude_proposal_skipped_reason = None

    sr.status = "passed"
    sr.message = (
        f"제안 생성 ({len(body)} chars, model={model}, "
        f"feature_id={propose_feature_id or '—'})"
    )
    return sr


# ---------------------------------------------------------------------------
# Implementation Ticket stage
# ---------------------------------------------------------------------------
#
# Bridges the planner ↔ designer ↔ PM artifacts (high-level intent) with
# the Claude proposal (concrete change set) into a single ticket file.
#
# Cycle contract:
#   * Ticket present + has target files → claude_apply may run.
#   * Ticket missing or no target files  → cycle stays planning_only.
#
# This stage does NOT call Claude. It parses what we already have on disk
# (planner_revision.md / pm_decision.md / claude_proposal.md) and
# composes a deterministic ticket. That keeps the bridge cheap and
# means a planner-only run can still report "we wanted to do X but
# nothing was concrete enough to ship".


# Code paths that count as "real product code". Anything outside these
# roots is treated as docs/config-only — see _categorize_changed_files.
PRODUCT_CODE_PREFIXES: tuple[str, ...] = (
    "app/web/src/",
    "app/api/",
    "control_tower/web/src/",
    "control_tower/api/",
    "control_tower/local_runner/",
)
FRONTEND_PATH_PREFIXES: tuple[str, ...] = (
    "app/web/src/",
)
BACKEND_PATH_PREFIXES: tuple[str, ...] = (
    "app/api/",
)
CONTROL_TOWER_PATH_PREFIXES: tuple[str, ...] = (
    "control_tower/web/src/",
    "control_tower/api/",
    "control_tower/local_runner/",
)


def _categorize_changed_files(files: list[str]) -> dict:
    """Classify a list of changed paths into FE / BE / control_tower /
    docs-only. Used by main() to decide whether an "applied" cycle
    counts as real code work or a docs-only reshuffle."""
    files = [f for f in (files or []) if f]
    fe = any(f.startswith(p) for f in files for p in FRONTEND_PATH_PREFIXES)
    be = any(f.startswith(p) for f in files for p in BACKEND_PATH_PREFIXES)
    ct = any(f.startswith(p) for f in files for p in CONTROL_TOWER_PATH_PREFIXES)
    product_files = [
        f for f in files
        if any(f.startswith(p) for p in PRODUCT_CODE_PREFIXES)
    ]
    docs_only = bool(files) and not product_files
    return {
        "frontend": fe,
        "backend": be,
        "control_tower": ct,
        "docs_only": docs_only,
        "product_files": product_files,
    }


_TICKET_FILE_LINE = re.compile(
    r"""(?ix)                       # ignore-case, verbose
    ^[\-\*•]\s*                # bullet (- / * / •)
    `?                              # optional backtick wrapping
    (
        (?:app|control_tower|scripts|config|docs)
        /[\w./\-]+                  # subpath
    )
    """,
    re.MULTILINE,
)


def _parse_target_files_from_md(md: str) -> list[str]:
    """Pull bullet-list file paths out of a markdown body. Looks first
    inside the `## 변경 대상 파일` / `## 수정 대상 파일` section, then
    falls back to the whole document. Path-shape filter keeps stray
    prose out."""
    if not md:
        return []
    headings = ("## 변경 대상 파일", "## 수정 대상 파일", "## 수정 대상")
    section = ""
    for h in headings:
        slice_text = _extract_md_section(md, h.lstrip("# ").strip())
        if slice_text:
            section = slice_text
            break
    haystack = section or md
    found: list[str] = []
    seen: set[str] = set()
    for m in _TICKET_FILE_LINE.finditer(haystack):
        path = m.group(1).strip().rstrip("`,. ")
        # Skip obviously non-file ish lines.
        if "/" not in path:
            continue
        if path in seen:
            continue
        seen.add(path)
        found.append(path)
    return found


def _parse_screens_from_md(md: str) -> list[str]:
    """Pull the `## 수정 대상 화면` (or 변경 대상 화면) bullet list. Looser
    than file parsing — we keep raw human strings since "스탬프 결과 화면"
    isn't a path."""
    if not md:
        return []
    for h in ("수정 대상 화면", "변경 대상 화면"):
        section = _extract_md_section(md, h)
        if not section:
            continue
        out: list[str] = []
        for line in section.splitlines():
            ls = line.strip()
            if not ls.startswith(("-", "*", "•")):
                continue
            text = ls[1:].strip()
            if text:
                out.append(text[:80])
        if out:
            return out[:8]
    return []


def _selected_feature_for_ticket(state: CycleState) -> str | None:
    """Best-effort pick of the cycle's chosen feature name. Falls back
    through PM decision → planner revision → product planner."""
    candidates = [
        state.implementation_ticket_selected_feature,
        state.product_planner_selected_feature,
        state.planner_revision_selected_feature,
    ]
    for c in candidates:
        if c and c.strip():
            return c.strip()[:120]
    # Try to read pm_decision.md "출하 결정" line.
    if PM_DECISION_FILE.is_file():
        try:
            body = PM_DECISION_FILE.read_text(encoding="utf-8")
        except OSError:
            body = ""
        for h in ("선정 기능", "선택 기능", "선택한 기능"):
            section = _extract_md_section(body, h)
            line = _first_meaningful_line(section)
            if line:
                return line[:120]
    return None


def _build_ticket_markdown(
    state: CycleState,
    *,
    feature: str | None,
    target_files: list[str],
    target_screens: list[str],
    pm_md: str,
    planner_md: str,
    proposal_md: str,
) -> str:
    """Compose the deterministic ticket body. The exact section headings
    here match the contract in CLAUDE.md / docs so claude_apply and the
    dashboard panel can both rely on the layout."""
    def _section(md: str, heading: str) -> str:
        s = _extract_md_section(md, heading)
        return s.strip() or "(자료 없음)"

    user_problem = _section(planner_md, "사용자 문제") if planner_md else "(자료 없음)"
    if user_problem == "(자료 없음)":
        user_problem = _section(pm_md, "사용자 문제")
    cycle_scope = _section(planner_md, "MVP 범위") or _section(pm_md, "이번 사이클 구현 범위")
    success = (
        _section(planner_md, "성공 기준")
        or _section(pm_md, "성공 기준")
        or "수동 QA 시나리오 통과 + 검증 통과"
    )
    qa_scenario = _section(pm_md, "수동 QA 시나리오") or _section(
        planner_md, "수동 QA 시나리오"
    )

    files_block = (
        "\n".join(f"- {p}" for p in target_files) if target_files else "(없음)"
    )
    screens_block = (
        "\n".join(f"- {s}" for s in target_screens) if target_screens else "(별도 표기 없음)"
    )

    return (
        "# Implementation Ticket\n\n"
        f"## 선택한 기능\n{feature or '(미정)'}\n\n"
        f"## 사용자 문제\n{user_problem}\n\n"
        f"## 이번 사이클 구현 범위\n{cycle_scope}\n\n"
        f"## 수정 대상 화면\n{screens_block}\n\n"
        f"## 수정 대상 파일\n{files_block}\n\n"
        "## 구현해야 할 동작\n"
        + (proposal_md.strip() or pm_md.strip() or "(claude_proposal.md / pm_decision.md 참조)")
        + "\n\n"
        "## UI 변경사항\n(claude_apply 단계에서 위 파일들에 반영)\n\n"
        "## 데이터 변경사항\n(스키마/저장소 변경이 있다면 위 파일 목록에 명시)\n\n"
        "## 제외 범위\n"
        "- 위 파일 목록에 없는 경로 수정 금지\n"
        "- 단순 스탬프/배지/칭호 이름 추가만 하는 변경 금지\n"
        "- 사용자 행동을 바꾸지 않는 문구 변경만 하는 변경 금지\n\n"
        f"## 수동 QA 시나리오\n{qa_scenario}\n\n"
        f"## 성공 기준\n{success}\n"
    )


def _build_ticket_from_design_spec(
    design_spec_md: str,
    *,
    target_files: list[str],
    target_screens: list[str],
) -> tuple[str, str | None]:
    """Compose implementation_ticket.md from design_spec.md ALONE.

    No proposal / planner / PM body is consulted. This is the spec_bypass
    path: when design_spec_acceptance has passed, the design_spec is the
    single source of truth for the cycle, and a stale Local Visa proposal
    cannot leak into a TitleSeal cycle's ticket.

    Returns (body, feature_name_or_none).
    """
    feature = _extract_design_spec_feature(design_spec_md)
    impl_target = _extract_md_section(design_spec_md, "구현 대상 기능")
    svg_section = _extract_md_section(design_spec_md, "SVG Path 명세")
    titlelabel_section = _extract_md_section(
        design_spec_md, "titleLabel 최종 목록"
    )
    badges_section = _extract_md_section(
        design_spec_md, "badges.js 스키마 변경"
    )
    sharecard_section = _extract_md_section(
        design_spec_md, "ShareCard 레이아웃 명세"
    )
    qa_section = _extract_md_section(design_spec_md, "QA 기준")

    # Extract user problem from "관련 PM HOLD 사유" line inside 구현 대상 기능.
    user_problem = "(자료 없음)"
    for raw in (impl_target or "").splitlines():
        line = raw.strip().lstrip("-").strip()
        m = re.match(r"^관련 PM HOLD 사유\s*[:：]\s*(.+)$", line)
        if m:
            user_problem = m.group(1).strip()
            break

    files_block = (
        "\n".join(f"- {p}" for p in target_files)
        if target_files else "(없음)"
    )
    screens_block = (
        "\n".join(f"- {s}" for s in target_screens)
        if target_screens else "(별도 표기 없음)"
    )

    behavior_parts: list[str] = []
    if svg_section:
        behavior_parts.append("### SVG Path 명세\n" + svg_section.strip())
    if titlelabel_section:
        behavior_parts.append(
            "### titleLabel 최종 목록\n" + titlelabel_section.strip()
        )
    if badges_section:
        behavior_parts.append(
            "### badges.js 스키마 변경\n" + badges_section.strip()
        )
    if sharecard_section:
        behavior_parts.append(
            "### ShareCard 레이아웃 명세\n" + sharecard_section.strip()
        )
    behavior_block = (
        "\n\n".join(behavior_parts)
        or "(design_spec.md 본문 참조)"
    )

    body = (
        "# Implementation Ticket\n\n"
        "<!-- source: design_spec — 단일 source of truth -->\n\n"
        f"## 선택한 기능\n{feature or '(design_spec 기능명 미기재)'}\n\n"
        f"## 사용자 문제\n{user_problem}\n\n"
        "## 이번 사이클 구현 범위\n"
        + (impl_target.strip() if impl_target else "(design_spec 구현 대상 기능 참조)")
        + "\n\n"
        f"## 수정 대상 화면\n{screens_block}\n\n"
        f"## 수정 대상 파일\n{files_block}\n\n"
        "## 구현해야 할 동작\n"
        + behavior_block
        + "\n\n"
        "## UI 변경사항\n"
        "claude_apply 단계에서 위 SVG Path / ShareCard 레이아웃 / "
        "TitleSeal 명세를 그대로 적용한다.\n\n"
        "## 데이터 변경사항\n"
        + (badges_section.strip() if badges_section else
           "(badges.js 스키마 외 데이터 변경 없음)")
        + "\n\n"
        "## 제외 범위\n"
        "- 위 파일 목록에 없는 경로 수정 금지\n"
        "- 이전 사이클의 selected_feature 를 다시 적용하지 마라 — "
        "이번 사이클의 단일 source of truth 는 design_spec.md\n"
        "- claude_proposal.md 본문 무시 (있더라도 사용하지 않음)\n\n"
        "## 수동 QA 시나리오\n"
        + (qa_section.strip() if qa_section else "(QA 기준 미기재)")
        + "\n\n"
        "## 성공 기준\n"
        "- design_spec acceptance 조건 만족 (SVG 3종 / titleLabel ≥13 / "
        "수정 대상 파일 / ShareCard 렌더 조건 / QA 기준)\n"
        "- `npm run build` 통과\n"
        "- 위 수동 QA 시나리오 통과\n"
    )
    return body, feature


def _build_apply_input_from_design_spec(
    *,
    design_spec_md: str,
    ticket_md: str,
    target_files: list[str],
) -> str:
    """Assemble the proposal-shaped body that claude_apply consumes on
    a spec_bypass cycle. Uses the existing CLAUDE_APPLY_PROMPT_TEMPLATE
    structure so the prompt's "수정 제안" / "변경 대상 파일" anchors
    stay valid, but the *content* comes from the ticket + design_spec —
    not from claude_proposal.md."""
    files_block = (
        "\n".join(
            f"- `{p}` — design_spec 명세 그대로 적용"
            for p in target_files
        )
        if target_files else "(없음)"
    )
    return (
        "# Stampport 구현 적용 (Design Spec 기반)\n\n"
        "이번 사이클의 단일 source of truth 는 implementation_ticket.md / "
        "design_spec.md 입니다. claude_proposal.md 가 있더라도 무시하세요.\n\n"
        "## 수정 제안\n"
        + ticket_md.strip()
        + "\n\n## 변경 대상 파일\n"
        + files_block
        + "\n\n## 검증 방법\n"
        "- `npm run build` 통과\n"
        "- design_spec QA 기준 충족\n"
        "- design_spec target_files 외 다른 경로는 수정 금지\n\n"
        "## 적용 여부 판단 기준\n"
        "- design_spec.md 의 SVG Path / titleLabel / badges.js 스키마 / "
        "ShareCard 레이아웃 명세를 그대로 반영하지 못하는 변경은 거부.\n\n"
        "=== START Design Spec (단일 source of truth) ===\n"
        + design_spec_md.strip()
        + "\n=== END Design Spec ===\n"
    )


# ---------------------------------------------------------------------------
# Pipeline contract validators
#
# Each validator is a pure function over CycleState (and optionally the
# active rework lock + on-disk artifacts) and returns a result dict:
#
#   {
#     "name":     <validator name>,
#     "ok":       bool,
#     "code":     short failure code (snake_case) | "passed",
#     "message":  human-readable summary,
#     "evidence": dict of fields useful for debug + report tables,
#   }
#
# The dashboard / smoke / observer reports render these directly so a
# stage contract failure is always traceable to a specific validator
# code (scope_mismatch_preflight, missing_ticket_contract, …) rather
# than a freeform string. This is the kernel-contract layer that turns
# per-symptom HOLD-loop bandaids into a single deterministic gate.
# ---------------------------------------------------------------------------


def _validator_result(
    name: str, ok: bool, code: str, message: str,
    evidence: dict | None = None,
) -> dict:
    return {
        "name": name,
        "ok": bool(ok),
        "code": code,
        "message": message,
        "evidence": dict(evidence or {}),
    }


def validate_planner_contract(state: "CycleState") -> dict:
    """Planner output is "complete enough" to feed the rest of the
    cycle: at least one of product_planner / planner_revision must be
    `generated` AND yield a selected feature."""
    pp_status = state.product_planner_status
    pr_status = state.planner_revision_status
    feature = (
        state.planner_revision_selected_feature
        or state.product_planner_selected_feature
        or state.selected_feature
        or ""
    ).strip()
    feature_id = state.selected_feature_id or _to_feature_id(feature)
    has_planner = pp_status in {"generated", "fallback_generated"} or pr_status in {
        "generated", "fallback_generated",
    }
    if not has_planner:
        return _validator_result(
            "planner_contract", False, "missing_planner",
            f"planner output not generated (product_planner={pp_status}, "
            f"planner_revision={pr_status})",
            {
                "product_planner_status": pp_status,
                "planner_revision_status": pr_status,
            },
        )
    if not feature:
        return _validator_result(
            "planner_contract", False, "missing_selected_feature",
            "planner ran but did not yield a selected_feature",
            {
                "product_planner_status": pp_status,
                "planner_revision_status": pr_status,
            },
        )
    if not feature_id:
        return _validator_result(
            "planner_contract", False, "missing_feature_id",
            f"selected_feature='{feature}' did not produce a slug feature_id",
            {"feature": feature},
        )
    return _validator_result(
        "planner_contract", True, "passed",
        f"planner contract OK (feature_id={feature_id})",
        {"feature": feature, "feature_id": feature_id},
    )


def validate_design_spec_contract(state: "CycleState") -> dict:
    """When design_spec is generated + accepted, the spec must own the
    feature_id for the rest of the cycle. Validator passes when:
      * design_spec_status == "generated"
      * design_spec_acceptance_passed
      * not stale_design_spec_detected
      * a non-empty feature_id was extracted from the spec body
    Skipped (ok=True) when design_spec didn't run at all — the
    implementation_ticket validator owns the planner-only path.
    """
    if state.design_spec_status not in {"generated", "insufficient", "failed"}:
        return _validator_result(
            "design_spec_contract", True, "skipped",
            "design_spec did not run this cycle",
            {"design_spec_status": state.design_spec_status},
        )
    if state.design_spec_status != "generated":
        return _validator_result(
            "design_spec_contract", False, "design_spec_not_accepted",
            f"design_spec_status={state.design_spec_status}",
            {"design_spec_status": state.design_spec_status},
        )
    if not state.design_spec_acceptance_passed:
        return _validator_result(
            "design_spec_contract", False, "design_spec_acceptance_failed",
            "design_spec generated but acceptance gate failed",
            {
                "failures": list(
                    state.design_spec_acceptance_failures or []
                )[:6],
            },
        )
    if state.stale_design_spec_detected:
        return _validator_result(
            "design_spec_contract", False, "design_spec_stale",
            "design_spec belongs to a previous cycle / feature",
            {
                "stale_feature": state.stale_design_spec_feature,
                "stale_cycle_id": state.stale_design_spec_cycle_id,
            },
        )
    fid = state.design_spec_feature_id or _to_feature_id(state.design_spec_feature)
    if not fid:
        return _validator_result(
            "design_spec_contract", False, "design_spec_missing_feature_id",
            "design_spec accepted but no feature_id parsed from body",
            {"design_spec_feature": state.design_spec_feature},
        )
    return _validator_result(
        "design_spec_contract", True, "passed",
        f"design_spec accepted (feature_id={fid})",
        {
            "feature": state.design_spec_feature,
            "feature_id": fid,
            "target_files_count": len(state.design_spec_target_files or []),
        },
    )


def validate_implementation_ticket_contract(state: "CycleState") -> dict:
    """The ticket must align with the cycle's source of truth.

    When design_spec is the source (accepted + not stale), the ticket's
    selected_feature_id MUST equal the design_spec_feature_id. When
    design_spec didn't run, the ticket's feature_id must equal the
    planner's selected feature_id. Either way, target_files must be
    non-empty (otherwise claude_apply has nothing to do).
    """
    if state.implementation_ticket_status != "generated":
        return _validator_result(
            "implementation_ticket_contract", True, "skipped",
            f"implementation_ticket status="
            f"{state.implementation_ticket_status}",
            {"status": state.implementation_ticket_status},
        )
    target_files = list(state.implementation_ticket_target_files or [])
    if not target_files:
        return _validator_result(
            "implementation_ticket_contract", False, "missing_ticket_contract",
            "ticket generated without any target_files — nothing to apply",
            {},
        )
    ticket_feature = (
        state.implementation_ticket_selected_feature
        or state.selected_feature
        or ""
    ).strip()
    ticket_fid = state.implementation_ticket_feature_id or _to_feature_id(
        ticket_feature
    )
    spec_ok = (
        state.design_spec_status == "generated"
        and state.design_spec_acceptance_passed
        and not state.stale_design_spec_detected
    )
    if spec_ok:
        spec_fid = state.design_spec_feature_id or _to_feature_id(
            state.design_spec_feature
        )
        if spec_fid and ticket_fid and spec_fid != ticket_fid:
            return _validator_result(
                "implementation_ticket_contract", False,
                "scope_mismatch_preflight",
                f"ticket feature_id='{ticket_fid}' does not match "
                f"design_spec feature_id='{spec_fid}'",
                {
                    "ticket_feature": ticket_feature,
                    "ticket_feature_id": ticket_fid,
                    "design_spec_feature": state.design_spec_feature,
                    "design_spec_feature_id": spec_fid,
                },
            )
    return _validator_result(
        "implementation_ticket_contract", True, "passed",
        f"ticket feature_id={ticket_fid} target_files={len(target_files)}",
        {
            "ticket_feature": ticket_feature,
            "ticket_feature_id": ticket_fid,
            "target_files_count": len(target_files),
        },
    )


def validate_scope_contract(state: "CycleState") -> dict:
    """Cross-stage feature_id consistency. Every populated stage that
    carries a feature_id must agree:

      planner.selected_feature_id ≡ ticket.feature_id ≡ design_spec.feature_id

    When a stage hasn't run, its feature_id is ignored. The validator
    passes when ≤1 distinct id is populated, fails otherwise.
    """
    spec_ok = (
        state.design_spec_status == "generated"
        and state.design_spec_acceptance_passed
        and not state.stale_design_spec_detected
    )
    spec_fid = (
        state.design_spec_feature_id
        or _to_feature_id(state.design_spec_feature)
    ) if spec_ok else ""
    ticket_fid = state.implementation_ticket_feature_id or _to_feature_id(
        state.implementation_ticket_selected_feature
    ) if state.implementation_ticket_status == "generated" else ""
    planner_fid = state.selected_feature_id or _to_feature_id(
        state.planner_revision_selected_feature
        or state.product_planner_selected_feature
    )
    populated = {
        ("design_spec", spec_fid),
        ("implementation_ticket", ticket_fid),
        ("planner", planner_fid),
    }
    populated = {(k, v) for (k, v) in populated if v}
    distinct_ids = {fid for _, fid in populated}
    if len(distinct_ids) <= 1:
        return _validator_result(
            "scope_contract", True, "passed",
            f"scope contract OK (id={next(iter(distinct_ids), '—')})",
            {"populated": sorted(populated)},
        )
    # When the design_spec is the source of truth and disagrees,
    # surface that as the canonical mismatch reason.
    if spec_fid and (
        (ticket_fid and ticket_fid != spec_fid)
        or (planner_fid and planner_fid != spec_fid)
    ):
        return _validator_result(
            "scope_contract", False, "scope_mismatch_preflight",
            f"design_spec feature_id='{spec_fid}' diverges from "
            f"ticket='{ticket_fid or '—'}' / planner='{planner_fid or '—'}'",
            {
                "design_spec_feature_id": spec_fid,
                "implementation_ticket_feature_id": ticket_fid,
                "planner_feature_id": planner_fid,
            },
        )
    return _validator_result(
        "scope_contract", False, "scope_mismatch_preflight",
        f"feature_ids disagree: {sorted(distinct_ids)}",
        {"populated": sorted(populated)},
    )


def validate_source_of_truth_contract(state: "CycleState") -> dict:
    """Enforce the Cycle Source-of-Truth Contract.

    The kernel rule: every feature_id-bearing stage in this cycle MUST
    emit `feature_id == state.source_of_truth_feature_id`. The check is
    skip-aware so a leftover artifact on disk does NOT cause a false
    failure — only stages whose status indicates an active artifact
    are gated:

      * implementation_ticket  → checked when status == "generated"
      * claude_proposal        → checked when status == "generated"
      * design_spec            → checked ONLY when
                                 status == "generated" AND
                                 design_spec_acceptance_passed.
                                 design_spec_status in {"skipped",
                                 "stale_isolated", "not_run", None}
                                 means the design_spec is INACTIVE for
                                 this cycle and the disk file is the
                                 runtime artifact sweep's responsibility
                                 to isolate, not this validator's to
                                 fail on.

    Returns a `_validator_result` dict. The failure code is the
    canonical `source_of_truth_mismatch`; `apply_preflight_status`
    inherits this code so `pipeline_decision.blocking_code` becomes
    `source_of_truth_mismatch` instead of the generic
    `apply_preflight_failed` / `scope_mismatch_preflight`.
    """
    sot_fid = (state.source_of_truth_feature_id or "").strip()
    if not sot_fid:
        # Soft fallback for legacy callers that hit this validator
        # before _lock_source_of_truth ran (apply-only retry pre-
        # rehydrate, legacy regression fixtures predating the SoT
        # contract). The validator is the LAST defense — if SoT
        # genuinely never got locked, defer to scope_check / legacy
        # contracts rather than fail here, so the strictly correct
        # production wiring (planner stage calls _lock_source_of_truth
        # explicitly) is what enforces the contract.
        return _validator_result(
            "source_of_truth_contract", True, "not_locked",
            "source_of_truth_feature_id not locked — defer to scope/legacy contracts",
            {
                "planner_revision_status": state.planner_revision_status,
                "product_planner_status": state.product_planner_status,
                "active_rework_feature": state.active_rework_feature,
            },
        )
    mismatches: list[dict] = []
    if state.implementation_ticket_status == "generated":
        tfid = (
            state.implementation_ticket_feature_id
            or _to_feature_id(state.implementation_ticket_selected_feature)
        )
        if tfid and tfid != sot_fid:
            mismatches.append({
                "stage": "implementation_ticket",
                "feature_id": tfid,
            })
    if state.claude_proposal_status == "generated":
        pfid = (
            state.claude_proposal_feature_id
            or _to_feature_id(state.selected_feature)
        )
        if pfid and pfid != sot_fid:
            mismatches.append({
                "stage": "claude_proposal",
                "feature_id": pfid,
            })
    spec_active = (
        state.design_spec_status == "generated"
        and bool(state.design_spec_acceptance_passed)
    )
    if spec_active:
        dfid = (
            state.design_spec_feature_id
            or _to_feature_id(state.design_spec_feature)
        )
        if dfid and dfid != sot_fid:
            mismatches.append({
                "stage": "design_spec",
                "feature_id": dfid,
            })
    if mismatches:
        first = mismatches[0]
        return _validator_result(
            "source_of_truth_contract", False, "source_of_truth_mismatch",
            f"{first['stage']} feature_id='{first['feature_id']}' "
            f"!= source_of_truth_feature_id='{sot_fid}'",
            {
                "source_of_truth_feature_id": sot_fid,
                "source_of_truth_stage": state.source_of_truth_stage,
                "mismatches": mismatches,
            },
        )
    return _validator_result(
        "source_of_truth_contract", True, "passed",
        f"source-of-truth contract OK (feature_id={sot_fid})",
        {
            "source_of_truth_feature_id": sot_fid,
            "source_of_truth_stage": state.source_of_truth_stage,
        },
    )


def validate_apply_preflight(state: "CycleState") -> dict:
    """Final gate before stage_claude_apply spends Claude budget.

    Aggregate of:
      * planner contract
      * source-of-truth contract (Cycle SoT — overrides per-stage scope)
      * design_spec contract (when applicable)
      * implementation_ticket contract
      * scope contract
      * active rework feature lock compatible with current run/feature
      * non-stale design_spec / ticket artifacts (run_id check on disk)
    """
    # 1. Ticket present + has files.
    #
    # validate_implementation_ticket_contract returns (ok=True,
    # code="skipped") when the ticket stage hasn't run, which would
    # otherwise let the `not ok` branch swallow the skipped case
    # silently. We treat the skipped code as a hard preflight failure
    # explicitly — apply must NEVER spend Claude budget without a
    # generated ticket on disk.
    ticket_check = validate_implementation_ticket_contract(state)
    if ticket_check["code"] == "skipped":
        return _validator_result(
            "apply_preflight", False, "missing_ticket_contract",
            "implementation_ticket not generated — apply blocked",
            {"ticket_status": state.implementation_ticket_status},
        )
    if not ticket_check["ok"]:
        return _validator_result(
            "apply_preflight", False, ticket_check["code"],
            ticket_check["message"], ticket_check["evidence"],
        )

    # 1b. Cycle Source-of-Truth Contract — checked BEFORE the legacy
    # design_spec / scope checks so the more specific
    # `source_of_truth_mismatch` blocking_code wins over generic
    # `scope_mismatch_preflight`. The old design_spec_stale code path
    # is intentionally kept downstream for the case where SoT is set
    # AND design_spec ran AND its acceptance failed.
    sot_check = validate_source_of_truth_contract(state)
    if not sot_check["ok"]:
        return _validator_result(
            "apply_preflight", False, sot_check["code"],
            sot_check["message"], sot_check["evidence"],
        )

    # 2. design_spec contract (only enforced when the spec ran).
    if state.design_spec_status in {"generated", "insufficient", "failed"}:
        spec_check = validate_design_spec_contract(state)
        if not spec_check["ok"]:
            return _validator_result(
                "apply_preflight", False, spec_check["code"],
                spec_check["message"], spec_check["evidence"],
            )

    # 3. Cross-stage scope contract.
    scope_check = validate_scope_contract(state)
    if not scope_check["ok"]:
        return _validator_result(
            "apply_preflight", False, scope_check["code"],
            scope_check["message"], scope_check["evidence"],
        )

    # 4. Active rework feature lock reconciliation. A lock from a
    # different run is stale by definition — the autopilot mints a
    # fresh run_id every loop start — so we clear it here and let the
    # preflight continue. Returning feature_lock_conflict here would
    # block legitimate rework cycles whose only crime is finding a
    # leftover lock file on disk.
    lock = _load_active_rework_feature()
    lock_feature = (lock.get("feature") or "").strip()
    lock_fid = (lock.get("feature_id") or _to_feature_id(lock_feature) or "").strip()
    lock_run = (lock.get("run_id") or "").strip()
    cur_run = state.run_id or _resolve_run_id()
    stale_lock_cleared = False
    if lock_run and cur_run and lock_run != cur_run:
        stale_lock_cleared = bool(_clear_active_rework_feature())
        # Reset locals so downstream feature-id check sees an empty lock.
        lock = {}
        lock_feature = ""
        lock_fid = ""
        lock_run = ""
    spec_ok = (
        state.design_spec_status == "generated"
        and state.design_spec_acceptance_passed
        and not state.stale_design_spec_detected
    )
    spec_fid = (
        state.design_spec_feature_id
        or _to_feature_id(state.design_spec_feature)
    ) if spec_ok else ""
    if spec_fid and lock_fid and lock_fid != spec_fid:
        return _validator_result(
            "apply_preflight", False, "feature_lock_conflict",
            "active rework lock feature_id != accepted design_spec "
            f"(lock={lock_fid}, design_spec={spec_fid})",
            {"lock_feature_id": lock_fid, "design_spec_feature_id": spec_fid},
        )

    # 5. Stale-artifact preflight — implementation_ticket.md MUST carry
    # the current run_id; design_spec.md is only gated when the spec is
    # ACTIVE this cycle (status==generated AND acceptance_passed). A
    # leftover design_spec.md from a previous cycle whose spec stage
    # was skipped/stale this cycle is the runtime artifact sweep's
    # responsibility — failing here would punish cycles that correctly
    # decided NOT to use the on-disk spec body. apply_preflight is the
    # last defense, not the stale-file janitor.
    spec_active_for_stale = (
        state.design_spec_status == "generated"
        and bool(state.design_spec_acceptance_passed)
    )
    stale_targets: list[tuple[Path, str]] = [
        (IMPLEMENTATION_TICKET_FILE, "implementation_ticket"),
    ]
    if spec_active_for_stale:
        stale_targets.append((DESIGN_SPEC_FILE, "design_spec"))
    stale = []
    for path, label in stale_targets:
        try:
            if not path.is_file():
                continue
            head = path.read_text(encoding="utf-8")[:512]
        except OSError:
            continue
        rid = _parse_artifact_run_id(head)
        if rid and cur_run and rid != cur_run:
            stale.append({"label": label, "artifact_run_id": rid})
    if stale:
        return _validator_result(
            "apply_preflight", False, "stale_artifact_preflight",
            f"stale artifact(s) carry a different run_id: "
            f"{[s['label'] for s in stale]}",
            {"stale": stale, "current_run_id": cur_run},
        )

    return _validator_result(
        "apply_preflight", True, "passed",
        f"apply preflight OK (feature_id={spec_fid or scope_check['evidence'].get('populated') or '—'})",
        {
            "design_spec_feature_id": spec_fid,
            "ticket_feature_id":
                state.implementation_ticket_feature_id
                or _to_feature_id(state.implementation_ticket_selected_feature),
            "current_run_id": cur_run,
            "stale_lock_cleared": stale_lock_cleared,
        },
    )


def build_pipeline_decision(state) -> dict:
    """Single source of truth for autopilot publish/commit/push gating.

    Pure function — accepts either a CycleState instance or a raw
    factory_state.json dict (so smoke / autopilot / observer can call
    it without depending on the live CycleState class) and returns a
    PipelineDecision contract:

        {
          "pipeline_status": "blocked"|"hold"|"ready_to_review"|
                             "ready_to_publish"|"published",
          "can_commit": bool,
          "can_push": bool,
          "can_publish": bool,
          "blocking_code": str | None,
          "blocking_reason": str | None,
          "checks": { "planner", "ticket", "apply", "qa", "scope",
                      "meaningful_change" → "passed"|"failed"|"skipped" },
          "evidence": {...},
        }

    can_publish is true iff ALL of the following hold:
      * implementation_ticket_status == "generated"
      * claude_apply_status == "applied"
      * changed_files_count > 0 (or claude_apply_changed_files non-empty)
      * qa_status == "passed"
      * apply_preflight_status == "passed"
      * failed_stage is empty
      * failed_reason is empty

    Legacy scope_consistency_status is surfaced under checks["scope"]
    for compatibility but is intentionally NOT a publish-blocker on its
    own — it would otherwise trip auto-publish whenever a cycle
    happened to leave the field null while every "real" pipeline gate
    passed.
    """
    if hasattr(state, "to_dict") and callable(getattr(state, "to_dict")):
        s = state.to_dict()
    elif isinstance(state, dict):
        s = state
    else:
        s = {}

    def _norm(v):
        if v is None:
            return ""
        return str(v).strip()

    ticket_status = _norm(s.get("implementation_ticket_status"))
    apply_status = _norm(s.get("claude_apply_status"))
    qa_status = _norm(s.get("qa_status"))
    preflight_status = _norm(s.get("apply_preflight_status"))
    failed_stage = _norm(s.get("failed_stage"))
    failed_reason = _norm(s.get("failed_reason"))
    scope_status = _norm(s.get("scope_consistency_status"))
    qa_failed_reason = _norm(s.get("qa_failed_reason"))
    planner_status = _norm(s.get("product_planner_status"))
    planner_revision_status = _norm(s.get("planner_revision_status"))
    # Claude Executor Contract — surfaced into pipeline_decision so a
    # CLI-level failure produces a precise blocking_code (e.g.
    # claude_cli_timeout) rather than the generic stage_failed.
    executor_status = _norm(s.get("claude_executor_status"))
    executor_failure_code = _norm(s.get("claude_executor_failure_code"))
    executor_failure_reason = _norm(s.get("claude_executor_failure_reason"))

    changed_files = list(s.get("claude_apply_changed_files") or [])
    raw_count = s.get("changed_files_count")
    try:
        changed_files_count = int(raw_count) if raw_count is not None else len(changed_files)
    except (TypeError, ValueError):
        changed_files_count = len(changed_files)
    has_changes = changed_files_count > 0 or len(changed_files) > 0

    checks: dict[str, str] = {}

    has_planner = (
        planner_status in {"generated", "fallback_generated"}
        or planner_revision_status in {"generated", "fallback_generated"}
    )
    if has_planner:
        checks["planner"] = "passed"
    elif planner_status in {"", "skipped"} and planner_revision_status in {"", "skipped"}:
        checks["planner"] = "skipped"
    else:
        checks["planner"] = "failed"

    if ticket_status == "generated":
        checks["ticket"] = "passed"
    elif ticket_status in {"", "skipped", "skipped_hold"}:
        checks["ticket"] = "skipped"
    else:
        checks["ticket"] = "failed"

    if preflight_status == "passed":
        checks["apply"] = "passed"
    elif preflight_status == "":
        checks["apply"] = "skipped"
    else:
        checks["apply"] = "failed"

    if qa_status == "passed":
        checks["qa"] = "passed"
    elif qa_status in {"", "skipped"}:
        checks["qa"] = "skipped"
    else:
        checks["qa"] = "failed"

    if scope_status == "passed":
        checks["scope"] = "passed"
    elif scope_status == "failed":
        checks["scope"] = "failed"
    else:
        checks["scope"] = "skipped"

    checks["meaningful_change"] = "passed" if has_changes else "failed"

    apply_ok = apply_status == "applied"

    blocking_code: str | None = None
    blocking_reason: str | None = None

    # Claude Executor failure beats every other classification — if the
    # CLI itself is missing / unauthenticated / timed out, downstream
    # checks (ticket / apply / qa) are meaningless, so we surface the
    # specific executor code as the blocking reason.
    executor_failed = (
        executor_status in {"failed", "timeout", "retryable_failed"}
        or (executor_failure_code and executor_failure_code != "")
    )
    if executor_failed:
        blocking_code = executor_failure_code or "claude_cli_failed"
        blocking_reason = (
            executor_failure_reason
            or f"claude executor status={executor_status or 'unknown'}"
        )
        checks["apply"] = "failed"

    # Source-of-Truth mismatch — surfaced as a precise blocking_code
    # ahead of the generic stage_failed cascade so the operator (and
    # the smoke report) can tell the cycle was blocked specifically by
    # a planner / design_spec / ticket / proposal feature_id divergence
    # rather than by some unrelated stage failure that happened to land
    # on claude_apply.
    if not blocking_code and (
        _norm(s.get("apply_preflight_status")) == "source_of_truth_mismatch"
    ):
        blocking_code = "source_of_truth_mismatch"
        blocking_reason = (
            _norm(s.get("apply_preflight_reason"))
            or "source_of_truth_feature_id mismatch detected at apply preflight"
        )
        checks["scope"] = "failed"
        checks["apply"] = "failed"

    if blocking_code:
        pass  # executor short-circuited; skip the cascading detection
    elif failed_stage:
        blocking_code = "stage_failed"
        blocking_reason = (
            f"failed_stage={failed_stage}"
            + (f": {failed_reason}" if failed_reason else "")
        )
    elif failed_reason:
        blocking_code = "stage_failed"
        blocking_reason = f"failed_reason={failed_reason}"
    elif checks["ticket"] != "passed":
        blocking_code = "missing_ticket_contract"
        blocking_reason = (
            f"implementation_ticket_status={ticket_status or 'missing'}"
        )
    elif not apply_ok:
        blocking_code = "apply_not_completed"
        blocking_reason = f"claude_apply_status={apply_status or 'missing'}"
    elif checks["apply"] != "passed":
        blocking_code = "apply_preflight_failed"
        blocking_reason = (
            f"apply_preflight_status={preflight_status or 'missing'}"
        )
    elif not has_changes:
        blocking_code = "no_meaningful_change"
        blocking_reason = (
            "claude_apply_changed_files empty (no meaningful diff)"
        )
    elif checks["qa"] != "passed":
        blocking_code = "qa_failed"
        reason_tail = qa_failed_reason or "no reason recorded"
        blocking_reason = (
            f"qa_status={qa_status or 'missing'} ({reason_tail})"
        )

    can_publish = (
        blocking_code is None
        and ticket_status == "generated"
        and apply_ok
        and has_changes
        and qa_status == "passed"
        and preflight_status == "passed"
        and not failed_stage
        and not failed_reason
    )
    can_commit = (
        blocking_code is None
        and apply_ok
        and has_changes
        and not failed_stage
        and not failed_reason
    )
    can_push = can_commit and qa_status == "passed"

    executor_blocked = bool(
        executor_failed
        or (blocking_code or "").startswith("claude_cli_")
        or (blocking_code or "").startswith("claude_apply_")
    )

    if can_publish:
        pipeline_status = "ready_to_publish"
    elif executor_blocked:
        pipeline_status = "blocked"
    elif blocking_code in {
        "stage_failed", "qa_failed", "apply_preflight_failed",
        "source_of_truth_mismatch",
    }:
        pipeline_status = "blocked"
    elif blocking_code in {
        "missing_ticket_contract",
        "apply_not_completed",
        "no_meaningful_change",
    }:
        pipeline_status = "hold"
    else:
        pipeline_status = "ready_to_review"

    evidence = {
        "implementation_ticket_status": ticket_status or None,
        "claude_apply_status": apply_status or None,
        "qa_status": qa_status or None,
        "qa_failed_reason": qa_failed_reason or None,
        "apply_preflight_status": preflight_status or None,
        "failed_stage": failed_stage or None,
        "failed_reason": failed_reason or None,
        "scope_consistency_status": s.get("scope_consistency_status"),
        "changed_files_count": changed_files_count,
        "changed_files_sample": changed_files[:6],
        "claude_executor_status": executor_status or None,
        "claude_executor_failure_code": executor_failure_code or None,
        "claude_executor_failure_reason": executor_failure_reason or None,
        "claude_executor_retryable": s.get("claude_executor_retryable"),
        "claude_executor_retry_count": s.get("claude_executor_retry_count"),
        "claude_executor_stdout_path": s.get("claude_executor_stdout_path"),
        "claude_executor_stderr_path": s.get("claude_executor_stderr_path"),
        "claude_executor_max_cost_usd": s.get("claude_executor_max_cost_usd"),
        "claude_executor_cost_budget_source": s.get("claude_executor_cost_budget_source"),
        "claude_executor_exceeded_budget": s.get("claude_executor_exceeded_budget"),
    }

    return {
        "pipeline_status": pipeline_status,
        "can_commit": bool(can_commit),
        "can_push": bool(can_push),
        "can_publish": bool(can_publish),
        "blocking_code": blocking_code,
        "blocking_reason": blocking_reason,
        "checks": checks,
        "evidence": evidence,
    }


def classify_freshness_by_run_id(
    *, current_run_id: str | None, artifact_run_id: str | None,
    artifact_cycle_id: int | None, current_cycle_id: int | None,
) -> str:
    """Pure function shared by smoke / observer / dashboard report
    builders. Returns one of:

      "current_run"     — same run_id, same cycle (or no cycle yet)
      "previous_cycle"  — same run_id, older cycle
      "stale_run"       — different run_id (regardless of cycle)
      "unknown"         — cannot prove freshness from inputs

    cycle_id comparisons are only consulted when run_id agrees, which
    is the kernel rule that prevents cross-run cycle-counter collisions
    from looking fresh.
    """
    cur = (current_run_id or "").strip()
    art = (artifact_run_id or "").strip()
    if cur and art:
        if cur != art:
            return "stale_run"
    elif art and not cur:
        return "unknown"
    elif cur and not art:
        # legacy artifact without run_id — fall through to cycle_id
        pass
    if (
        artifact_cycle_id is not None
        and current_cycle_id is not None
    ):
        if artifact_cycle_id == current_cycle_id:
            return "current_run"
        if artifact_cycle_id < current_cycle_id:
            return "previous_cycle"
        return "unknown"
    return "current_run"


def _classify_pm_hold_type(state: "CycleState") -> tuple[str, str]:
    """Decide whether a PM HOLD is `soft` (we still have enough info to
    build something on the same feature) or `hard` (no candidate / no
    target_files / scope mismatch / domain violation — must not run
    implementation).

    Returns (hold_type, reason). hold_type is one of "soft", "hard".
    Caller is responsible for short-circuiting only when status is
    actually a HOLD; this helper just classifies whatever inputs it is
    given.
    """
    # Hard signals — anything in this list forces hard.
    hard_reasons: list[str] = []
    if (state.scope_consistency_status or "") == "failed":
        hard_reasons.append(
            f"scope_mismatch: {state.scope_mismatch_reason or '(no reason)'}"
        )
    if state.publish_blocked:
        hard_reasons.append("publish_blocked (secret/conflict)")
    planner_status = state.planner_revision_status
    pp_status = state.product_planner_status
    if (
        planner_status not in {"generated", "fallback_generated"}
        and pp_status not in {"generated", "fallback_generated"}
    ):
        hard_reasons.append(
            f"planner output invalid (planner_revision_status={planner_status}, "
            f"product_planner_status={pp_status})"
        )

    # selected_feature evidence — the rework lock counts even if the
    # current cycle's planner produced nothing usable.
    feature_candidates = [
        state.planner_revision_selected_feature,
        state.product_planner_selected_feature,
        state.selected_feature,
        state.active_rework_feature,
    ]
    has_feature = any(
        bool((f or "").strip()) for f in feature_candidates
    )
    if not has_feature:
        hard_reasons.append("no candidate feature (planner produced nothing)")

    # target_files evidence — pull from any source we'd consult while
    # composing a ticket. If none exist, we cannot build code.
    target_evidence: list[str] = []
    spec_files = list(state.design_spec_target_files or [])
    if spec_files:
        target_evidence.append(f"design_spec_target_files={len(spec_files)}")
    pm_md = ""
    proposal_md = ""
    planner_md = ""
    try:
        if PM_DECISION_FILE.is_file():
            pm_md = PM_DECISION_FILE.read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        if PROPOSAL_FILE.is_file():
            proposal_md = PROPOSAL_FILE.read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        if PLANNER_REVISION_FILE.is_file():
            planner_md = PLANNER_REVISION_FILE.read_text(encoding="utf-8")
        elif PRODUCT_PLANNER_FILE.is_file():
            planner_md = PRODUCT_PLANNER_FILE.read_text(encoding="utf-8")
    except OSError:
        pass
    for label, src in (
        ("proposal", proposal_md),
        ("pm", pm_md),
        ("planner", planner_md),
    ):
        files = _parse_target_files_from_md(src) if src else []
        if files:
            target_evidence.append(f"{label}={len(files)}")
    # Frontend scope hint also counts — many soft HOLDs have a planner
    # report whose "프론트 변경 범위" lists the exact files.
    if state.product_planner_frontend_scope:
        target_evidence.append("planner_frontend_scope")

    if not target_evidence:
        hard_reasons.append("no target_files in any source")

    if hard_reasons:
        return "hard", "; ".join(hard_reasons[:3])
    soft_signals = list(state.pm_hold_soft_signals or [])
    soft_signals += list(state.desire_scorecard_rework or [])
    return (
        "soft",
        f"feature={'/'.join(t for t in target_evidence)} "
        f"signals={','.join(sorted(set(soft_signals)))[:80] or 'none'}",
    )


def stage_implementation_ticket(state: CycleState) -> StageResult:
    """Compose .runtime/implementation_ticket.md from the cycle's
    upstream artifacts. Marks the ticket "missing" when no concrete
    target files can be derived — that's the signal main() uses to
    classify the cycle as planning_only and refuse claude_apply."""
    label = next(lab for n, lab, _ in STAGES if n == "implementation_ticket")
    sr = StageResult(name="implementation_ticket", label=label, status="running")
    t0 = time.time()

    def _skip(reason: str) -> StageResult:
        sr.status = "skipped"
        sr.message = reason
        sr.duration_sec = round(time.time() - t0, 3)
        state.implementation_ticket_status = "skipped"
        state.implementation_ticket_skipped_reason = reason
        return sr

    # Don't write a ticket while the working tree is locked.
    if state.publish_blocked:
        return _skip("차단 사유로 ticket 작성 보류")

    # PM HOLD gate — implementation_ticket must NOT be generated when
    # the PM verdict is hold. The operator can opt out with
    # FACTORY_ALLOW_PM_HOLD_TO_IMPLEMENT=true.
    #
    # Spec-mode override: when design_spec.md was generated this cycle
    # AND its acceptance gate passed, we ALWAYS create the
    # implementation ticket — even on PM HOLD. The design_spec is a
    # signed-off implementation contract, so a low desire score on the
    # planner candidate must not block the FE/BE work. Without this
    # bypass, autopilot loops on HOLD forever (rework cycle never ships
    # because PM always HOLDs on the next iteration's planner output).
    spec_acceptance_bypass = bool(
        state.design_spec_status == "generated"
        and state.design_spec_acceptance_passed
        and not state.stale_design_spec_detected
    )
    # Classify the HOLD type so soft HOLD (we still have feature + target
    # files) can advance to implementation. Hard HOLD (no candidate, no
    # target_files, scope mismatch, planner invalid, publish blocker)
    # MUST keep the legacy skipped_hold behaviour.
    pm_hold = (
        state.pm_decision_status == "generated"
        and not state.pm_decision_ship_ready
    )
    if pm_hold:
        hold_type, hold_reason = _classify_pm_hold_type(state)
        state.pm_hold_type = hold_type
        state.pm_hold_type_reason = hold_reason
    soft_hold_bypass = bool(pm_hold and state.pm_hold_type == "soft")
    if (
        pm_hold
        and not spec_acceptance_bypass
        and not soft_hold_bypass
        and not _factory_flag_enabled(
            "FACTORY_ALLOW_PM_HOLD_TO_IMPLEMENT", default_on=False,
        )
    ):
        sr.status = "skipped"
        sr.message = (
            "PM HOLD (hard) — 이번 사이클은 재작업 (hold_for_rework) 입니다."
            f" 사유: {state.pm_hold_type_reason or '—'}"
        )
        sr.duration_sec = round(time.time() - t0, 3)
        state.implementation_ticket_status = "skipped_hold"
        state.implementation_ticket_skipped_reason = (
            f"pm_hold_for_rework_hard: {state.pm_hold_type_reason or 'no detail'}"
        )
        state.implementation_ticket_target_files = []
        state.implementation_ticket_target_screens = []
        state.implementation_ticket_message = sr.message
        # Stale-artifact cleanup — a previous SHIP cycle's
        # implementation_ticket.md must NOT remain readable as the current
        # cycle's output when this cycle ends in HOLD. Move it to .prev so
        # the operator can still inspect history but `cat` doesn't show
        # last week's Local Visa ticket while we're rework'ing something
        # else.
        moved = _move_stale_artifact_aside(IMPLEMENTATION_TICKET_FILE)
        if moved:
            _emit_cycle_log(
                state, "implementation_ticket_stale_moved",
                f"prior implementation_ticket.md moved aside → {moved}",
            )
        _emit_cycle_log(
            state, "implementation_ticket_skipped_hold",
            f"implementation_ticket skipped — PM HOLD (hard): {hold_reason}",
            hold_type="hard",
            hold_reason=hold_reason,
        )
        return sr
    if soft_hold_bypass:
        _emit_cycle_log(
            state, "implementation_ticket_soft_hold_bypass",
            "PM HOLD (soft) — selected_feature 와 target_files 가 살아 있어 "
            "implementation_ticket 을 계속 만든다.",
            hold_type="soft",
            hold_reason=state.pm_hold_type_reason or "",
        )

    pm_md = ""
    planner_md = ""
    proposal_md = ""
    design_spec_md = ""
    try:
        if PM_DECISION_FILE.is_file():
            pm_md = PM_DECISION_FILE.read_text(encoding="utf-8")
    except OSError:
        pm_md = ""
    try:
        if PLANNER_REVISION_FILE.is_file():
            planner_md = PLANNER_REVISION_FILE.read_text(encoding="utf-8")
        elif PRODUCT_PLANNER_FILE.is_file():
            planner_md = PRODUCT_PLANNER_FILE.read_text(encoding="utf-8")
    except OSError:
        planner_md = ""
    try:
        if PROPOSAL_FILE.is_file():
            proposal_md = PROPOSAL_FILE.read_text(encoding="utf-8")
    except OSError:
        proposal_md = ""
    try:
        if DESIGN_SPEC_FILE.is_file():
            design_spec_md = DESIGN_SPEC_FILE.read_text(encoding="utf-8")
    except OSError:
        design_spec_md = ""

    spec_bypass = bool(
        design_spec_md
        and state.design_spec_status == "generated"
        and state.design_spec_acceptance_passed
        and not state.stale_design_spec_detected
    )

    # Source priority: design_spec.md takes precedence whenever its
    # acceptance gate passed, since that's the spec PM accepted as the
    # ship signal. Otherwise fall through to proposal / pm / planner.
    target_files: list[str] = []
    if spec_bypass:
        target_files = _extract_design_spec_target_files(design_spec_md)
    if not target_files:
        for src in (proposal_md, pm_md, planner_md):
            if not src:
                continue
            target_files = _parse_target_files_from_md(src)
            if target_files:
                break
    target_screens = _parse_screens_from_md(planner_md) or _parse_screens_from_md(pm_md)

    if spec_bypass:
        # Design-spec single-source-of-truth: derive the feature name
        # from design_spec.md itself, NOT from prior planner state. This
        # is the gate that prevents an old "Local Visa" selected_feature
        # from contaminating a TitleSeal cycle's ticket.
        ds_feature = (
            state.design_spec_feature
            or _extract_design_spec_feature(design_spec_md)
        )
        feature = ds_feature or "(design_spec 기능명 미기재)"
        state.design_spec_feature = ds_feature
        state.design_spec_feature_id = (
            state.design_spec_feature_id or _to_feature_id(ds_feature)
        )
        # Reconcile the active rework feature lock with the accepted
        # design_spec. The lock can only point at one feature at a time;
        # a stale lock pointing at a previous feature would otherwise
        # leak back into the next cycle's planner prompt.
        lock = _load_active_rework_feature()
        lock_fid = (
            (lock.get("feature_id") or "").strip()
            or _to_feature_id(lock.get("feature"))
        )
        ds_fid = state.design_spec_feature_id or _to_feature_id(ds_feature)
        if lock and ds_fid and lock_fid and lock_fid != ds_fid:
            _save_active_rework_feature(
                feature=ds_feature,
                feature_id=ds_fid,
                hold_count=int(lock.get("hold_count") or 0),
                hold_type=lock.get("last_hold_type"),
                pm_message=lock.get("pm_message"),
                run_id=state.run_id,
            )
            state.active_rework_feature = ds_feature
            _emit_cycle_log(
                state, "active_rework_feature_realigned",
                f"active_rework_feature lock realigned to design_spec "
                f"feature_id (was='{lock_fid}', now='{ds_fid}')",
                lock_feature_id=lock_fid,
                design_spec_feature_id=ds_fid,
            )
    else:
        feature = _selected_feature_for_ticket(state)
    state.implementation_ticket_selected_feature = feature
    state.implementation_ticket_feature_id = _to_feature_id(feature)
    # Cycle Source-of-Truth Contract: when a SoT is locked, it OWNS the
    # ticket's feature/feature_id. design_spec body still supplies
    # target_files / SVG details when spec_bypass holds, but the
    # ticket's NAME comes from the planner-locked SoT — never from a
    # leftover design_spec_feature that drifted from the planner pick.
    sot_fid = (state.source_of_truth_feature_id or "").strip()
    sot_name = (state.source_of_truth_feature or "").strip()
    if sot_fid:
        feature = sot_name or feature
        state.implementation_ticket_selected_feature = feature
        state.implementation_ticket_feature_id = sot_fid

    if not target_files:
        # No concrete file targets → ticket is "missing". We still write
        # a stub so the operator can see what was attempted.
        body = _build_ticket_markdown(
            state,
            feature=feature,
            target_files=[],
            target_screens=target_screens,
            pm_md=pm_md,
            planner_md=planner_md,
            proposal_md=proposal_md,
        )
        ok_write = safe_write_artifact(
            IMPLEMENTATION_TICKET_FILE, body,
            cycle_id=state.cycle, stage="implementation_ticket",
            source_agent="pm",
            extra={"verdict": "missing"},
        )
        if not ok_write:
            sr.status = "failed"
            sr.message = "ticket write failed (see local_factory.log)"
            sr.duration_sec = round(time.time() - t0, 3)
            state.implementation_ticket_status = "failed"
            state.implementation_ticket_message = sr.message
            return sr
        sr.status = "skipped"
        sr.message = (
            "PM 결정에 수정 대상 파일이 명시되지 않음 — "
            "pm_scope_missing_target_files 로 분류, planning_only 로 종료."
        )
        sr.duration_sec = round(time.time() - t0, 3)
        # 'pm_scope_missing_target_files' separates "PM didn't list any
        # files" from "ticket generation crashed" — the observer / smoke
        # test treat these differently. The original 'missing' status
        # is retained as an alias so older consumers still see a
        # known-bad value.
        state.implementation_ticket_status = "pm_scope_missing_target_files"
        state.implementation_ticket_path = str(IMPLEMENTATION_TICKET_FILE)
        state.implementation_ticket_at = utc_now_iso()
        state.implementation_ticket_target_files = []
        state.implementation_ticket_target_screens = list(target_screens)
        state.implementation_ticket_message = sr.message
        _emit_cycle_log(
            state, "implementation_ticket_pm_scope_missing",
            "implementation ticket missing — PM 결정에 수정 대상 파일 없음, "
            "planning_only 로 종료 예정",
            feature=feature,
        )
        return sr

    if spec_bypass:
        body, ds_feature_for_body = _build_ticket_from_design_spec(
            design_spec_md,
            target_files=target_files,
            target_screens=target_screens,
        )
        ticket_source = "design_spec"
        feature_source = "design_spec"
        if ds_feature_for_body:
            feature = ds_feature_for_body
            state.implementation_ticket_selected_feature = ds_feature_for_body
            state.design_spec_feature = ds_feature_for_body
            state.design_spec_feature_id = (
                state.design_spec_feature_id
                or _to_feature_id(ds_feature_for_body)
            )
            state.implementation_ticket_feature_id = _to_feature_id(
                ds_feature_for_body
            )
        # Re-enforce the Cycle Source-of-Truth Contract: even when
        # design_spec has its own canonical feature name, SoT wins.
        # design_spec_feature already passed the SoT check in
        # stage_design_spec (or design_spec_status would be
        # failed_scope_mismatch and we wouldn't be on the spec_bypass
        # path), so this is normally a no-op — but it's the kernel
        # invariant the apply_preflight contract relies on.
        if sot_fid:
            feature = sot_name or feature
            state.implementation_ticket_selected_feature = feature
            state.implementation_ticket_feature_id = sot_fid
    else:
        body = _build_ticket_markdown(
            state,
            feature=feature,
            target_files=target_files,
            target_screens=target_screens,
            pm_md=pm_md,
            planner_md=planner_md,
            proposal_md=proposal_md,
        )
        ticket_source = "claude_proposal" if proposal_md else "planner"
        feature_source = (
            "planner"
            if state.product_planner_selected_feature
            else "implementation_ticket"
        )
    ok_write = safe_write_artifact(
        IMPLEMENTATION_TICKET_FILE, body,
        cycle_id=state.cycle, stage="implementation_ticket",
        source_agent="pm",
        feature_id=state.implementation_ticket_feature_id or None,
        extra={
            "target_files_count": len(target_files),
            "selected_feature": feature or "—",
            "ticket_source": ticket_source,
        },
    )
    if not ok_write:
        sr.status = "failed"
        sr.message = "ticket write failed (see local_factory.log)"
        sr.duration_sec = round(time.time() - t0, 3)
        state.implementation_ticket_status = "failed"
        state.implementation_ticket_message = sr.message
        return sr

    state.implementation_ticket_status = "generated"
    state.implementation_ticket_path = str(IMPLEMENTATION_TICKET_FILE)
    state.implementation_ticket_at = utc_now_iso()
    state.implementation_ticket_target_files = list(target_files)
    state.implementation_ticket_target_screens = list(target_screens)
    state.implementation_ticket_source = ticket_source
    state.selected_feature = feature
    state.selected_feature_id = (
        state.implementation_ticket_feature_id
        or _to_feature_id(feature)
    )
    state.selected_feature_source = feature_source
    state.implementation_ticket_message = (
        f"Implementation Ticket 작성됨 — 대상 파일 {len(target_files)}개"
        + (" (design_spec 기반)" if spec_bypass else "")
    )
    _emit_cycle_log(
        state, "implementation_ticket_created",
        f"implementation ticket created — 대상 파일 {len(target_files)}개 "
        f"(feature_id={state.selected_feature_id or '—'}, "
        f"source={feature_source})",
        feature=feature,
        feature_id=state.selected_feature_id,
        target_files=target_files[:20],
    )
    # Record the contract validator outcome on state so smoke / observer
    # / dashboard can render the table without re-running validation.
    contract = validate_implementation_ticket_contract(state)
    state.contract_results = [
        c for c in (state.contract_results or [])
        if c.get("name") != "implementation_ticket_contract"
    ] + [contract]
    sr.status = "passed"
    sr.message = state.implementation_ticket_message
    sr.duration_sec = round(time.time() - t0, 3)
    return sr


# ---------------------------------------------------------------------------
# Claude Executor Contract — independent CLI execution layer
#
# The executor isolates `claude` CLI failures from the rest of the cycle
# so that:
#   * a missing / unauthenticated / rate-limited CLI is detected BEFORE
#     planner/designer/spec stages spend tokens (preflight),
#   * every claude_apply subprocess call lands in a structured state
#     file with stdout/stderr/exit_code/timed_out, and
#   * each failure has a kernel classification code that the autopilot
#     retry policy can consult without re-grepping stderr itself.
#
# CLAUDE_FAILURE_CODES is the canonical enum. The order matters: the
# classifier walks them top-to-bottom and returns the first match.
# ---------------------------------------------------------------------------


CLAUDE_FAILURE_CODES = (
    "claude_cli_missing",
    "claude_cli_unavailable",
    "claude_cli_timeout",
    "claude_cli_auth_failed",
    "claude_cli_rate_limited",
    "claude_cli_budget_exceeded",
    "claude_cli_no_output",
    "claude_cli_exit_nonzero",
    "claude_apply_no_diff",
    "claude_apply_invalid_patch",
    "claude_cli_unknown_failure",
)

# Codes that should NOT trigger an immediate retry — fixing them
# requires operator action (install the binary, re-authenticate, raise
# the budget cap, etc.). budget_exceeded sits here because retrying
# the same prompt under the same cap produces the same error.
CLAUDE_NON_RETRYABLE_CODES = frozenset({
    "claude_cli_missing",
    "claude_cli_auth_failed",
    "claude_cli_budget_exceeded",
})


def classify_claude_failure(
    *,
    exit_code: int | None,
    timed_out: bool,
    missing_bin: bool,
    stdout: str,
    stderr: str,
    invalid_patch: bool = False,
    no_diff: bool = False,
) -> str:
    """Return the kernel failure code for a claude CLI invocation.

    Pure function so the smoke / autopilot self-tests can exercise the
    classifier without spawning a subprocess. The patterns are
    deliberately broad — claude CLI's error wording shifts release to
    release, so we look for substrings rather than exact matches.
    """
    if missing_bin:
        return "claude_cli_missing"
    if timed_out:
        return "claude_cli_timeout"
    blob = ((stderr or "") + "\n" + (stdout or "")).lower()
    auth_signals = (
        "not authenticated",
        "unauthorized",
        "401",
        "auth failed",
        "auth_failed",
        "invalid api key",
        "no api key",
        "log in",
        "claude login",
        "missing credentials",
    )
    if any(sig in blob for sig in auth_signals):
        return "claude_cli_auth_failed"
    # Budget exceeded must be checked BEFORE the rate-limit signals
    # because the CLI's wording ("Exceeded USD budget", "max-budget-usd")
    # overlaps and the budget code is non-retryable while rate_limited
    # is retryable. Misclassifying as rate_limited would loop forever.
    if _is_budget_exceeded(stdout or "", stderr or ""):
        return "claude_cli_budget_exceeded"
    rate_signals = (
        "rate limit",
        "rate-limited",
        "too many requests",
        "429",
        "quota",
    )
    if any(sig in blob for sig in rate_signals):
        return "claude_cli_rate_limited"
    if invalid_patch:
        return "claude_apply_invalid_patch"
    if no_diff:
        return "claude_apply_no_diff"
    if exit_code is not None and exit_code != 0:
        if not (stdout or "").strip() and not (stderr or "").strip():
            return "claude_cli_no_output"
        return "claude_cli_exit_nonzero"
    if exit_code is None:
        return "claude_cli_unavailable"
    return "claude_cli_unknown_failure"


def _is_retryable_claude_failure(code: str | None) -> bool:
    if not code:
        return False
    return code not in CLAUDE_NON_RETRYABLE_CODES


def _write_claude_executor_state(
    *,
    status: str,
    stage: str,
    command: list[str] | str | None,
    exit_code: int | None,
    timed_out: bool,
    duration_sec: float,
    failure_code: str | None,
    failure_reason: str | None,
    stdout_path: str | None,
    stderr_path: str | None,
    retryable: bool,
    retry_count: int,
    max_cost_usd: str | None = None,
    cost_budget_source: str | None = None,
    exceeded_budget: bool | None = None,
) -> dict:
    """Persist the executor verdict to claude_executor_state.json and
    claude_apply_command.json. Returns the dict that was written so
    callers can mirror it onto CycleState in a single place.

    `command` is normalized to argv for the command.json file (argv +
    raw_command + executable + extra_args), while the executor state
    keeps the shell-quoted single-line form for backwards compat."""
    if isinstance(command, list):
        argv = [str(a) for a in command]
        cmd_str = " ".join(shlex.quote(a) for a in argv)
    elif isinstance(command, str):
        argv = command.split() if command else []
        cmd_str = command
    else:
        argv = []
        cmd_str = ""
    raw_command = (
        os.environ.get("LOCAL_RUNNER_CLAUDE_COMMAND")
        or os.environ.get("CLAUDE_BIN")
        or ""
    )
    executable = argv[0] if argv else None
    extra_args = list(argv[1:]) if argv else []
    # Resolve budget metadata: callers may pass it explicitly; otherwise
    # we look it up from the stage so a `claude_apply` write that didn't
    # supply max_cost_usd still records the cap that was actually
    # applied to the subprocess.
    if max_cost_usd is None or cost_budget_source is None:
        try:
            inferred_amount, inferred_source = get_claude_budget_usd(stage)
        except Exception:  # noqa: BLE001
            inferred_amount, inferred_source = (
                CLAUDE_BUDGET_LEGACY_DEFAULT, "legacy_default",
            )
        if max_cost_usd is None:
            max_cost_usd = inferred_amount
        if cost_budget_source is None:
            cost_budget_source = inferred_source
    if exceeded_budget is None:
        exceeded_budget = (failure_code == "claude_cli_budget_exceeded")
    payload = {
        "status": status,
        "stage": stage,
        "command": cmd_str,
        "exit_code": exit_code,
        "timed_out": bool(timed_out),
        "duration_sec": round(float(duration_sec or 0.0), 3),
        "failure_code": failure_code,
        "failure_reason": (failure_reason or "")[-1500:] or None,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "retryable": bool(retryable),
        "retry_count": int(retry_count or 0),
        "max_cost_usd": max_cost_usd,
        "cost_budget_source": cost_budget_source,
        "exceeded_budget": bool(exceeded_budget),
        "updated_at": utc_now_iso(),
    }
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        CLAUDE_EXECUTOR_STATE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        CLAUDE_APPLY_COMMAND_FILE.write_text(
            json.dumps(
                {
                    "stage": stage,
                    "raw_command": raw_command,
                    "argv": argv,
                    "executable": executable,
                    "extra_args": extra_args,
                    "command": cmd_str,
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass
    return payload


def _apply_executor_state_to_cycle_state(state: "CycleState", payload: dict) -> None:
    """Mirror a written executor payload onto CycleState. Keeps the
    fields surfaced in factory_state.json in lockstep with the standalone
    claude_executor_state.json."""
    state.claude_executor_status = payload.get("status") or "not_run"
    state.claude_executor_stage = payload.get("stage")
    state.claude_executor_command = payload.get("command")
    state.claude_executor_exit_code = payload.get("exit_code")
    state.claude_executor_timed_out = bool(payload.get("timed_out"))
    state.claude_executor_duration_sec = payload.get("duration_sec")
    state.claude_executor_failure_code = payload.get("failure_code")
    state.claude_executor_failure_reason = payload.get("failure_reason")
    state.claude_executor_stdout_path = payload.get("stdout_path")
    state.claude_executor_stderr_path = payload.get("stderr_path")
    state.claude_executor_retryable = bool(payload.get("retryable"))
    state.claude_executor_retry_count = int(payload.get("retry_count") or 0)
    state.claude_executor_last_run_at = payload.get("updated_at")
    state.claude_executor_max_cost_usd = payload.get("max_cost_usd")
    state.claude_executor_cost_budget_source = payload.get("cost_budget_source")
    state.claude_executor_exceeded_budget = bool(payload.get("exceeded_budget"))


def _run_claude_capture(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 180.0,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> dict:
    """Run a claude subprocess with stdout/stderr captured to separate
    files. Returns a structured result dict consumed by the executor
    layer:

        {
          "ok": bool,
          "exit_code": int | None,
          "timed_out": bool,
          "missing_bin": bool,
          "stdout": str,
          "stderr": str,
          "stdout_path": str | None,
          "stderr_path": str | None,
          "duration_sec": float,
        }
    """
    t0 = time.time()
    out_text = ""
    err_text = ""
    exit_code: int | None = None
    timed_out = False
    missing_bin = False
    try:
        r = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out_text = r.stdout or ""
        err_text = r.stderr or ""
        exit_code = r.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        err_text = (
            f"timeout after {timeout}s: "
            f"{' '.join(shlex.quote(a) for a in argv)}\n"
            f"{(exc.stderr or b'').decode('utf-8', 'replace') if isinstance(exc.stderr, bytes) else (exc.stderr or '')}"
        )
        out_text = (
            (exc.stdout or b"").decode("utf-8", "replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
    except FileNotFoundError as exc:
        missing_bin = True
        err_text = f"missing tool: {exc}"
    except Exception as exc:  # noqa: BLE001
        err_text = f"error: {exc}"

    duration = time.time() - t0
    saved_stdout: str | None = None
    saved_stderr: str | None = None
    try:
        if stdout_path is not None:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_path.write_text(out_text or "", encoding="utf-8")
            saved_stdout = str(stdout_path)
        if stderr_path is not None:
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            stderr_path.write_text(err_text or "", encoding="utf-8")
            saved_stderr = str(stderr_path)
    except OSError:
        pass

    ok = (exit_code == 0) and not timed_out and not missing_bin
    return {
        "ok": bool(ok),
        "exit_code": exit_code,
        "timed_out": bool(timed_out),
        "missing_bin": bool(missing_bin),
        "stdout": out_text or "",
        "stderr": err_text or "",
        "stdout_path": saved_stdout,
        "stderr_path": saved_stderr,
        "duration_sec": float(duration),
    }


def _resolve_claude_command() -> dict:
    """Resolve the Claude CLI command spec from env vars.

    LOCAL_RUNNER_CLAUDE_COMMAND is treated as a SHELL-STYLE COMMAND
    STRING (e.g. "claude --dangerously-skip-permissions"), not a single
    executable path. We split it via shlex so flags like
    `--dangerously-skip-permissions` become argv entries instead of
    being mistaken for part of the binary name.

    Returns:
        {
          "raw_command": str        # original env value (or "" / None)
          "argv": list[str]         # shlex.split() result, [] if empty
          "executable": str | None  # argv[0] if any, resolved via PATH
          "extra_args": list[str]   # argv[1:]
          "missing": bool           # True when no executable can be found
          "source": str             # "LOCAL_RUNNER_CLAUDE_COMMAND" |
                                    #   "CLAUDE_BIN" | "PATH" | "none"
        }

    Resolution priority: LOCAL_RUNNER_CLAUDE_COMMAND → CLAUDE_BIN →
    `which claude`. CLAUDE_BIN is treated as a single binary path (no
    args), to preserve the legacy contract; only LOCAL_RUNNER_CLAUDE_
    COMMAND can carry trailing flags.
    """
    raw_local = (os.environ.get("LOCAL_RUNNER_CLAUDE_COMMAND") or "").strip()
    raw_bin = (os.environ.get("CLAUDE_BIN") or "").strip()

    raw_command = ""
    source = "none"
    argv: list[str] = []

    if raw_local:
        raw_command = raw_local
        source = "LOCAL_RUNNER_CLAUDE_COMMAND"
        try:
            argv = shlex.split(raw_local)
        except ValueError:
            # Unbalanced quotes — fall back to a whitespace split so
            # the operator still gets a clear "missing executable"
            # diagnostic instead of a Python crash.
            argv = raw_local.split()
    elif raw_bin:
        raw_command = raw_bin
        source = "CLAUDE_BIN"
        argv = [raw_bin]
    else:
        which_path = shutil.which("claude")
        if which_path:
            raw_command = which_path
            source = "PATH"
            argv = [which_path]

    executable: str | None = argv[0] if argv else None
    if executable:
        # If the executable isn't an absolute / relative path, try to
        # resolve it via PATH so the missing check below uses the
        # actual binary location.
        if not os.path.sep in executable and not executable.startswith("."):
            resolved = shutil.which(executable)
            if resolved:
                executable = resolved
                argv = [resolved] + argv[1:]
    extra_args = list(argv[1:]) if argv else []
    missing = bool(
        not executable
        or (
            os.path.sep in executable
            and not Path(executable).exists()
        )
        or (
            os.path.sep not in executable
            and shutil.which(executable) is None
        )
    )
    return {
        "raw_command": raw_command,
        "argv": list(argv),
        "executable": executable,
        "extra_args": extra_args,
        "missing": bool(missing),
        "source": source,
    }


# Stage-aware budget defaults (USD). Preflight runs a tiny smoke
# prompt — 0.25 leaves enough headroom for `claude --version` plus a
# single token reply without tripping the budget limit. Propose is a
# planning/critique pass (1.00 ≈ legacy default). Apply is the
# expensive code-edit pass (3.00 covers multi-file diffs).
CLAUDE_STAGE_BUDGET_DEFAULTS: dict[str, str] = {
    "claude_preflight": "0.25",
    "claude_propose": "1.00",
    "claude_apply": "3.00",
}

# Per-stage override env var. Operators set these to raise the cap
# for a specific stage without bumping every Claude call.
CLAUDE_STAGE_BUDGET_ENV: dict[str, str] = {
    "claude_preflight": "CLAUDE_PREFLIGHT_MAX_COST_USD",
    "claude_propose": "CLAUDE_PROPOSE_MAX_COST_USD",
    "claude_apply": "CLAUDE_APPLY_MAX_COST_USD",
}

# Legacy global cap. FACTORY_CLAUDE_BUDGET_USD still works as a
# fallback for stages other than preflight (preflight has its own tiny
# default — a 1.00 cap is overkill for `claude --version`).
CLAUDE_BUDGET_LEGACY_ENV = "FACTORY_CLAUDE_BUDGET_USD"
CLAUDE_BUDGET_LEGACY_DEFAULT = "1.00"


def get_claude_budget_usd(stage: str) -> tuple[str, str]:
    """Return (amount, source) for the given Claude stage.

    Resolution order:
      1. Stage-specific override env var (CLAUDE_<STAGE>_MAX_COST_USD).
      2. Legacy FACTORY_CLAUDE_BUDGET_USD — except for preflight, which
         deliberately ignores the global cap so a 0.25 smoke prompt
         doesn't inherit a 3.00 apply-stage budget by accident.
      3. Stage default from CLAUDE_STAGE_BUDGET_DEFAULTS.
      4. CLAUDE_BUDGET_LEGACY_DEFAULT for unrecognized stages.

    `source` is one of: stage_env / legacy_env / stage_default /
    legacy_default — surfaced into claude_executor_state.json so the
    operator can see why a particular cap was chosen.
    """
    stage = (stage or "").strip().lower()
    stage_env = CLAUDE_STAGE_BUDGET_ENV.get(stage)
    if stage_env:
        v = (os.environ.get(stage_env) or "").strip()
        if v:
            return v, "stage_env"
    # Preflight intentionally bypasses the legacy global cap. The
    # legacy cap is sized for full-cycle work; applying it to a 30s
    # smoke probe would waste tokens and obscure budget bugs.
    if stage != "claude_preflight":
        v = (os.environ.get(CLAUDE_BUDGET_LEGACY_ENV) or "").strip()
        if v:
            return v, "legacy_env"
    if stage in CLAUDE_STAGE_BUDGET_DEFAULTS:
        return CLAUDE_STAGE_BUDGET_DEFAULTS[stage], "stage_default"
    return CLAUDE_BUDGET_LEGACY_DEFAULT, "legacy_default"


_BUDGET_EXCEEDED_PATTERNS: tuple[str, ...] = (
    "exceeded usd budget",
    "exceeded budget",
    "max-budget-usd",
    "budget exceeded",
)


def _is_budget_exceeded(stdout: str, stderr: str) -> bool:
    """Detect Claude CLI's budget-exceeded error message.

    Matched substrings (case-insensitive): "Exceeded USD budget",
    "exceeded budget", "max-budget-usd", "budget exceeded". The first
    one is what the CLI actually emits today; the rest catch
    plausible reword variants so we don't misclassify the next
    release as claude_cli_exit_nonzero."""
    blob = ((stderr or "") + "\n" + (stdout or "")).lower()
    return any(p in blob for p in _BUDGET_EXCEEDED_PATTERNS)


_DSP_FLAG = "--dangerously-skip-permissions"


def _claude_argv_with(
    base_argv: list[str], extra: list[str], *, dedupe_dsp: bool = True,
) -> list[str]:
    """Compose `base_argv + extra` while never duplicating the
    --dangerously-skip-permissions flag (operators commonly include
    it in LOCAL_RUNNER_CLAUDE_COMMAND, and the flag is allowed once)."""
    if dedupe_dsp and _DSP_FLAG in base_argv:
        extra = [a for a in extra if a != _DSP_FLAG]
    return list(base_argv) + list(extra)


def stage_claude_preflight(state: CycleState) -> StageResult:
    """Verify the `claude` CLI is reachable, authenticated, and able to
    answer a tiny smoke prompt within 30s. Runs BEFORE product_planning
    so a broken executor does not waste planner/designer budget. On
    failure the cycle is short-circuited via state.cycle_log + main()'s
    early-exit branch — no downstream Claude stage runs.
    """
    label = next(lab for n, lab, _ in STAGES if n == "claude_preflight")
    sr = StageResult(name="claude_preflight", label=label, status="running")
    t0 = time.time()

    def _record(payload: dict, *, sr_status: str, message: str, detail: str = "") -> StageResult:
        _apply_executor_state_to_cycle_state(state, payload)
        sr.status = sr_status
        sr.message = message
        if detail:
            sr.detail = detail[-1500:]
        sr.duration_sec = round(time.time() - t0, 3)
        return sr

    timeout_sec = float(os.environ.get("FACTORY_CLAUDE_PREFLIGHT_TIMEOUT_SEC", "30"))
    spec = _resolve_claude_command()
    base_argv = list(spec["argv"])
    if spec["missing"] or not spec["executable"]:
        # Note: failure_reason carries argv[0] only — never the raw
        # command string with its trailing flags — because the Errno-2
        # report previously included things like
        # "/path/to/claude --dangerously-skip-permissions" as a single
        # path, which was the original confusing failure mode.
        missing_executable = spec["executable"] or "claude"
        payload = _write_claude_executor_state(
            status="failed",
            stage="claude_preflight",
            command=spec["argv"] or [missing_executable],
            exit_code=None,
            timed_out=False,
            duration_sec=time.time() - t0,
            failure_code="claude_cli_missing",
            failure_reason=(
                f"claude executable not found: {missing_executable!r} "
                f"(source={spec['source']}). Set LOCAL_RUNNER_CLAUDE_COMMAND "
                f"or CLAUDE_BIN to a valid path."
            ),
            stdout_path=None,
            stderr_path=None,
            retryable=False,
            retry_count=0,
        )
        state.failed_stage = "claude_preflight"
        state.failed_reason = payload["failure_reason"]
        return _record(payload, sr_status="failed", message="claude CLI 미설치 — preflight 실패")

    # Step 1: `claude --version` (or equivalent). Fast sanity check that
    # the binary at least exits cleanly. Some packages don't have
    # --version; we accept any zero exit.
    version_argv = _claude_argv_with(base_argv, ["--version"])
    version_result = _run_claude_capture(
        version_argv,
        cwd=REPO_ROOT,
        timeout=min(15.0, timeout_sec),
        stdout_path=None,
        stderr_path=None,
    )
    if version_result["missing_bin"]:
        payload = _write_claude_executor_state(
            status="failed",
            stage="claude_preflight",
            command=version_argv,
            exit_code=version_result["exit_code"],
            timed_out=version_result["timed_out"],
            duration_sec=time.time() - t0,
            failure_code="claude_cli_missing",
            failure_reason=(
                f"claude executable not runnable: {spec['executable']!r} "
                f"({(version_result['stderr'] or 'no error output').strip()[:200]})"
            ),
            stdout_path=None,
            stderr_path=None,
            retryable=False,
            retry_count=0,
        )
        state.failed_stage = "claude_preflight"
        state.failed_reason = payload["failure_reason"]
        return _record(payload, sr_status="failed", message="claude CLI 미설치 — preflight 실패")

    # Step 2: Smoke prompt. We ask claude to print a single token so a
    # broken auth / rate-limit / hung process is caught fast. Budget
    # is sourced from get_claude_budget_usd("claude_preflight") so a
    # too-tight legacy default (the original 0.05 hardcode that
    # produced "Exceeded USD budget (0.05)") never reappears.
    preflight_budget, preflight_budget_source = get_claude_budget_usd(
        "claude_preflight",
    )
    smoke_argv = _claude_argv_with(
        base_argv,
        [
            "-p", "Reply with exactly: STAMPPORT_OK",
            "--output-format", "text",
            "--max-budget-usd", preflight_budget,
        ],
    )
    smoke_result = _run_claude_capture(
        smoke_argv,
        cwd=REPO_ROOT,
        timeout=max(5.0, timeout_sec - (time.time() - t0)),
        stdout_path=None,
        stderr_path=None,
    )
    duration = time.time() - t0
    if smoke_result["ok"]:
        payload = _write_claude_executor_state(
            status="passed",
            stage="claude_preflight",
            command=smoke_argv,
            exit_code=smoke_result["exit_code"],
            timed_out=False,
            duration_sec=duration,
            failure_code=None,
            failure_reason=None,
            stdout_path=None,
            stderr_path=None,
            retryable=False,
            retry_count=0,
            max_cost_usd=preflight_budget,
            cost_budget_source=preflight_budget_source,
            exceeded_budget=False,
        )
        return _record(
            payload,
            sr_status="passed",
            message=f"claude CLI 사용 가능 ({duration:.1f}s)",
        )

    # Failure path — classify, surface, write repair prompt.
    code = classify_claude_failure(
        exit_code=smoke_result["exit_code"],
        timed_out=smoke_result["timed_out"],
        missing_bin=smoke_result["missing_bin"],
        stdout=smoke_result["stdout"],
        stderr=smoke_result["stderr"],
    )
    reason_tail = (smoke_result["stderr"] or smoke_result["stdout"] or "(no output)").strip()
    retryable = _is_retryable_claude_failure(code)
    payload = _write_claude_executor_state(
        status="timeout" if smoke_result["timed_out"] else "failed",
        stage="claude_preflight",
        command=smoke_argv,
        exit_code=smoke_result["exit_code"],
        timed_out=smoke_result["timed_out"],
        duration_sec=duration,
        failure_code=code,
        failure_reason=reason_tail,
        stdout_path=None,
        stderr_path=None,
        retryable=retryable,
        retry_count=0,
        max_cost_usd=preflight_budget,
        cost_budget_source=preflight_budget_source,
        exceeded_budget=(code == "claude_cli_budget_exceeded"),
    )
    state.failed_stage = "claude_preflight"
    state.failed_reason = (
        f"claude_preflight failed ({code}): {reason_tail[:300]}"
    )
    _emit_cycle_log(
        state, "claude_preflight_failed",
        f"claude_preflight failed — {code}: {reason_tail[:200]}",
        failure_code=code,
        retryable=retryable,
    )
    return _record(
        payload,
        sr_status="failed",
        message=f"claude CLI preflight 실패 — {code}",
        detail=reason_tail,
    )


# ---------------------------------------------------------------------------
# Claude apply stage (opt-in via FACTORY_APPLY_CLAUDE)
# ---------------------------------------------------------------------------


CLAUDE_APPLY_PROMPT_TEMPLATE = """\
당신은 Stampport 프로젝트에 다음 제안을 그대로 적용하는 Claude Code 입니다.

Stampport는 카페·빵집·맛집·디저트 방문을 여권 도장처럼 모으는 로컬 취향 RPG 서비스입니다.
어떤 변경도 Stampport의 정체성(passport / stamp / RPG / 감성 공유 카드)을 흐트러뜨려서는 안 됩니다.
지도/리뷰/관리자 대시보드/할 일 앱 톤으로의 변경은 거부하세요.

이번 사이클의 제안 본문 (자동 생성됨):
=== START ===
{proposal}
=== END ===

규칙:
- 위 제안의 "수정 제안"과 "변경 대상 파일"에 명시된 변경을 그대로 적용하세요.
- 다음 디렉터리 아래의 파일만 수정 가능: app/, control_tower/, scripts/
- 다음 패턴은 어떤 경우에도 만들거나 수정하거나 삭제하지 마세요:
  .env, .key, .pem, .db, .runtime/, node_modules/, dist/, .venv/,
  deploy/nginx-stampport.conf, .github/workflows/, systemd 관련 파일.
- 어떤 셸 명령도 실행하지 마세요. git commit/push, npm install, deploy 모두 금지.
- secret/private key/token 값을 출력에 포함하지 마세요.
- 사용 가능한 도구는 Read, Glob, Grep, Edit, Write 다섯 개뿐입니다. 그 외 호출 금지.
- 제안이 모호하거나 위험하다고 판단되면 변경 없이 종료하세요. 강제 적용 금지.

작업이 끝나면 마지막 응답은 다음 Markdown 형식만 출력하세요. preamble/설명 금지:

# 적용 결과
- `path/to/file1.py` — 한 줄 변경 요약
- `path/to/file2.jsx` — 한 줄 변경 요약

(파일을 변경하지 않았다면 위 형식 대신 "변경 없음" 한 줄만 출력하세요.)
"""


def _build_claude_apply_prompt(proposal: str) -> str:
    return CLAUDE_APPLY_PROMPT_TEMPLATE.format(proposal=proposal.strip())


def _violates_apply_policy(path: str) -> bool:
    """Return True if `path` is outside ALLOWED_APPLY_DIRS or matches a
    forbidden substring. Uses pure string comparison — never resolves
    symlinks — so path-normalization tricks can't widen the sandbox."""
    if not path:
        return True
    # Reject absolute paths and parent-traversal entirely.
    if path.startswith("/") or ".." in path.split("/"):
        return True
    for pat in FORBIDDEN_APPLY_PATTERNS:
        if pat in path:
            return True
    return not any(path.startswith(d) for d in ALLOWED_APPLY_DIRS)


def _hash_tracked_under_allowed() -> dict[str, str]:
    """Return {relpath: sha1} for tracked files inside ALLOWED_APPLY_DIRS.

    Used to detect *which* files Claude touched after the apply: any
    file whose hash differs from this snapshot is something we have to
    consider when rolling back."""
    result: dict[str, str] = {}
    cmd = ["git", "-C", str(REPO_ROOT), "ls-files", "--", *ALLOWED_APPLY_DIRS]
    ok, out = _run(cmd, timeout=30)
    if not ok:
        return result
    for line in out.splitlines():
        relpath = line.strip()
        if not relpath:
            continue
        full = REPO_ROOT / relpath
        if full.is_file():
            try:
                result[relpath] = hashlib.sha1(full.read_bytes()).hexdigest()
            except OSError:
                # Unreadable — treat as missing. Safer than crashing
                # the whole cycle.
                pass
    return result


def _untracked_under_allowed() -> set[str]:
    """Return paths of untracked files inside ALLOWED_APPLY_DIRS."""
    cmd = [
        "git", "-C", str(REPO_ROOT), "ls-files",
        "--others", "--exclude-standard", "--", *ALLOWED_APPLY_DIRS,
    ]
    ok, out = _run(cmd, timeout=30)
    if not ok:
        return set()
    return {p.strip() for p in out.splitlines() if p.strip()}


def _diff_apply_changes(
    before_hashes: dict[str, str],
    before_untracked: set[str],
) -> tuple[list[str], list[str]]:
    """Compute (changed_tracked, new_untracked) since the snapshot."""
    after_hashes = _hash_tracked_under_allowed()
    after_untracked = _untracked_under_allowed()

    # Tracked files that have a different hash now (new content), OR
    # tracked files that vanished (Claude removed them — rare but
    # possible via Edit). We treat both as "modified" for rollback.
    changed_tracked: list[str] = []
    keys = set(before_hashes.keys()) | set(after_hashes.keys())
    for k in keys:
        if before_hashes.get(k) != after_hashes.get(k):
            changed_tracked.append(k)

    new_untracked = sorted(after_untracked - before_untracked)
    return sorted(changed_tracked), new_untracked


def _rollback_apply(
    changed_tracked: list[str],
    new_untracked: list[str],
) -> tuple[bool, str]:
    """Reset modified files to HEAD and remove newly created files.

    `git checkout HEAD -- <file>` returns the file to the committed
    version. This loses any pre-existing dirty state in those files —
    a documented MVP trade-off. Newly untracked files (created by
    Claude this run) are unlinked outright.
    """
    notes: list[str] = []
    if changed_tracked:
        ok, out = _run(
            ["git", "-C", str(REPO_ROOT), "checkout", "HEAD", "--", *changed_tracked],
            timeout=60,
        )
        notes.append(
            f"git checkout HEAD -- {len(changed_tracked)}건: {'OK' if ok else 'FAIL'}"
        )
        if not ok:
            notes.append(out[-400:])
    removed = 0
    remove_failures: list[str] = []
    for relpath in new_untracked:
        full = REPO_ROOT / relpath
        try:
            if full.is_file():
                full.unlink()
                removed += 1
        except OSError as e:
            remove_failures.append(f"{relpath}: {e}")
    if new_untracked:
        notes.append(f"새 파일 삭제: {removed}/{len(new_untracked)}건")
    if remove_failures:
        notes.append("실패: " + "; ".join(remove_failures[:5]))
    return (not remove_failures, " | ".join(notes))


def _revalidate_after_apply() -> tuple[bool, list[str]]:
    """Re-run the same correctness gates as the main cycle, but compact:
    we only care PASS/FAIL, not per-stage metrics. Returns
    (all_passed, list_of_failed_check_names).

    Side effect: when build_app fails, the captured stdout+stderr is
    written to .runtime/app_build_after_apply.log so the smoke runner /
    repair prompt can show the operator the actual vite/webpack error
    instead of just "build_app". When the build passes (or no npm),
    any stale log from a previous cycle is cleared so we never serve a
    log that doesn't match the current rolled-back diff.
    """
    failures: list[str] = []
    npm = shutil.which("npm")

    # 1. app/web build
    web = REPO_ROOT / "app" / "web"
    if web.is_dir() and npm:
        ok, build_out = _run(
            [npm, "run", "build"], cwd=web, timeout=300,
            env_override={"CI": "1"},
        )
        if not ok:
            failures.append("build_app")
            try:
                APP_BUILD_AFTER_APPLY_LOG.parent.mkdir(parents=True, exist_ok=True)
                APP_BUILD_AFTER_APPLY_LOG.write_text(
                    build_out or "(no output captured)",
                    encoding="utf-8",
                )
            except OSError:
                pass
        else:
            # Drop the stale log so a downstream consumer never confuses
            # last cycle's failure with this cycle's success.
            try:
                if APP_BUILD_AFTER_APPLY_LOG.is_file():
                    APP_BUILD_AFTER_APPLY_LOG.unlink()
            except OSError:
                pass

    # 2. control_tower/web build
    cweb = REPO_ROOT / "control_tower" / "web"
    if cweb.is_dir() and npm:
        ok, _ = _run([npm, "run", "build"], cwd=cweb, timeout=300, env_override={"CI": "1"})
        if not ok:
            failures.append("build_control")

    # 3. python py_compile
    skip_dirs = {".venv", "venv", "__pycache__", "node_modules", "dist", "build", ".runtime"}
    py_files = _collect_py_files(
        [
            REPO_ROOT / "app" / "api",
            REPO_ROOT / "control_tower" / "api",
            REPO_ROOT / "control_tower" / "local_runner",
        ],
        skip_dirs,
    )
    venv_py = REPO_ROOT / "control_tower" / "api" / ".venv" / "bin" / "python"
    py_bin = str(venv_py) if venv_py.is_file() else sys.executable
    py_failed = False
    for f in py_files:
        ok, _ = _run([py_bin, "-m", "py_compile", str(f)], timeout=20)
        if not ok:
            py_failed = True
            break
    if py_failed:
        failures.append("syntax_check_py")

    # 4. shell bash -n
    sh_files = [
        REPO_ROOT / "scripts" / "local_factory_start.sh",
        REPO_ROOT / "scripts" / "local_factory_stop.sh",
        REPO_ROOT / "scripts" / "local_factory_status.sh",
    ]
    for f in sh_files:
        if not f.is_file():
            continue
        ok, _ = _run(["bash", "-n", str(f)], timeout=10)
        if not ok:
            failures.append("syntax_check_sh")
            break

    # 5. Risky-file scan (post-apply view of git status — Claude must
    # not have created secrets). Cache/build artifact patterns are no
    # longer in RISKY_PATTERNS, so this fires only on actual secrets.
    ok, out = _run(["git", "-C", str(REPO_ROOT), "status", "--short"], timeout=15)
    if ok:
        for line in out.splitlines():
            path = line[3:].strip() if len(line) > 3 else line
            if "->" in path:
                path = path.split("->", 1)[1].strip()
            if any(p in path for p in RISKY_PATTERNS):
                failures.append("risky_files")
                break

    return (not failures, failures)


def _evaluate_apply_meaningfulness(
    diff_text: str,
    changed_files: list[str],
    selected_feature: str | None,
) -> tuple[bool, list[str], str]:
    """Heuristic Feature Build Guard.

    Returns (is_meaningful, criteria_met, reason_if_not).

    Per the Product Planner Mode spec, ≥2 of the following 7 criteria
    must fire for the apply to count as a real feature build. The
    check is intentionally lenient on each individual signal so
    legitimate small features still pass, but strict in aggregate so
    a comment-only or label-swap diff fails fast.
    """
    criteria_met: list[str] = []

    # Look only at ADDED lines so existing source noise doesn't count.
    added_lines = [
        l[1:] for l in diff_text.splitlines()
        if l.startswith("+") and not l.startswith("+++")
    ]
    added_text = "\n".join(added_lines)

    # 1. New React component OR new UI section/card.
    has_new_component = bool(
        re.search(r"^\s*export\s+default\s+function\s+[A-Z]\w*\s*\(", added_text, re.MULTILINE)
        or re.search(r"^\s*export\s+function\s+[A-Z]\w*\s*\(", added_text, re.MULTILINE)
        or re.search(r"^\s*function\s+[A-Z]\w+\s*\(", added_text, re.MULTILINE)
    )
    has_new_section = bool(
        re.search(r"<section\b", added_text)
        or re.search(r'class(?:Name)?=["\'][^"\']*\bcard\b[^"\']*["\']', added_text, re.IGNORECASE)
    )
    if has_new_component or has_new_section:
        criteria_met.append("새 React component 또는 UI section 추가")

    # 2. analyze 응답 구조 확장 — specifically AnalyzeResponse / new
    # pydantic fields in the schema, or a new analyze-related endpoint.
    schema_paths = (
        "app/api/app/schemas/",
        "app/api/main.py",
    )
    if any(f.startswith(p) for p in schema_paths for f in []) or any(
        f.startswith("app/api/") for f in changed_files
    ):
        if (
            re.search(r"AnalyzeResponse|analyze\b", added_text)
            or re.search(
                r"^\s+\w+\s*:\s*(?:str|int|float|bool|list\[|tuple\[|dict\[|Optional\[|Field)",
                added_text, re.MULTILINE,
            )
            or re.search(r"@(?:app|router)\.(?:get|post|put|delete|patch)\b", added_text)
        ):
            criteria_met.append("analyze 응답 구조 확장")

    # 3. app/api 또는 control_tower/api schema 변경.
    schema_file_changed = any(
        f.endswith("schemas.py")
        or "/schemas/" in f
        or f.endswith("models.py")
        or "project_schema" in f
        for f in changed_files
        if f.startswith("app/api/") or f.startswith("control_tower/api/")
    )
    if schema_file_changed:
        criteria_met.append("app/api 또는 control_tower/api schema 변경")

    # 4. app/web 화면 변경 (any .jsx/.tsx/.js change in app/web/).
    if any(
        f.startswith("app/web/") and f.endswith((".jsx", ".tsx", ".js"))
        for f in changed_files
    ):
        criteria_met.append("app/web 화면 변경")

    # 5. localStorage / 상태관리 추가.
    state_signals = [
        r"\blocalStorage\b",
        r"\bsessionStorage\b",
        r"\buseReducer\s*\(",
        r"\bcreateContext\s*\(",
        r"\buseContext\s*\(",
        r"\bzustand\b",
        r"\bcreateStore\s*\(",
    ]
    if any(re.search(p, added_text) for p in state_signals):
        criteria_met.append("localStorage 또는 상태관리 추가")

    # 6. selected feature name appears in diff (code OR added comments
    # both count — the report file itself isn't part of the diff so
    # this is a clean signal that claude wired the feature in by name).
    if selected_feature:
        keywords = [
            w for w in re.findall(r"[\w가-힣]+", selected_feature)
            if len(w) >= 3 and w.lower() not in {"the", "and", "기능", "추가", "위한"}
        ]
        for kw in keywords:
            if kw in added_text:
                criteria_met.append(f"선정 기능 이름이 코드에 반영됨 ({kw})")
                break

    # 7. 테스트/검증 코드 또는 fallback 추가.
    test_or_fallback = bool(
        any(
            "/tests/" in f or f.startswith("tests/") or "_test." in f or "test_" in f.split("/")[-1]
            for f in changed_files
        )
        or re.search(r"\bfallback\b", added_text, re.IGNORECASE)
        or re.search(r"^\s*try\s*:\s*$", added_text, re.MULTILINE)
        or re.search(r"^\s*except\s+\w+", added_text, re.MULTILINE)
        or re.search(r"^\s*assert\s+\w", added_text, re.MULTILINE)
    )
    if test_or_fallback:
        criteria_met.append("테스트/검증 코드 또는 fallback 추가")

    # Dedup by prefix so multi-match flavors of the same signal collapse.
    seen: set[str] = set()
    deduped: list[str] = []
    for c in criteria_met:
        prefix = c.split(" (", 1)[0]
        if prefix in seen:
            continue
        seen.add(prefix)
        deduped.append(c)

    if len(deduped) >= 2:
        return True, deduped, ""

    # Build a "why we rejected" reason — useful for the report.
    if not added_text.strip():
        return False, deduped, "변경 내용 없음"

    nontrivial = [l for l in added_lines if l.strip()]
    only_comments = nontrivial and all(
        l.lstrip().startswith(("#", "//", "/*", "*", "<!--"))
        for l in nontrivial
    )
    if only_comments:
        return False, deduped, "주석만 변경됨"

    only_strings = nontrivial and all(
        re.fullmatch(r'\s*[\'"][^\'"]*[\'"]\s*,?\s*', l)
        or re.search(r"['\"][^'\"]+['\"]", l) and len(l.strip()) < 80
        for l in nontrivial
    )
    if len(changed_files) <= 1 and only_strings:
        return False, deduped, "한 파일에서 문자열 literal만 변경됨"

    return (
        False,
        deduped,
        f"의미 있는 기능 변경 기준 미충족 (충족 {len(deduped)}/2)",
    )


def stage_claude_apply(state: CycleState) -> StageResult:
    label = next(lab for n, lab, _ in STAGES if n == "claude_apply")
    sr = StageResult(name="claude_apply", label=label, status="running")
    t0 = time.time()
    _emit_cycle_log(
        state, "claude_apply_started",
        "claude apply started — 제안서를 working tree에 적용 시도",
    )

    def _skip(reason: str) -> StageResult:
        sr.status = "skipped"
        sr.message = reason
        sr.duration_sec = round(time.time() - t0, 3)
        state.claude_apply_status = "skipped"
        state.claude_apply_skipped_reason = reason
        _emit_cycle_log(
            state, "claude_apply_no_changes",
            f"claude apply no changes (skipped): {reason}",
        )
        return sr

    # Pre-condition 0: publish blocker policy. Even more important here
    # than at propose-time — an apply on top of a blocker would create
    # an unpushable mixed change set.
    if state.publish_blocked:
        return _skip(
            "차단 사유(secret/conflict)가 남아 있어 신규 개발을 중단했습니다."
        )

    # Pre-condition 1: opt-out. Default ON — Stampport's automation
    # factory must reach claude_apply by default so the cycle can
    # actually ship code. Operators running cycle.py manually for
    # diagnostic purposes can disable with FACTORY_APPLY_CLAUDE=false.
    if not _factory_flag_enabled("FACTORY_APPLY_CLAUDE", default_on=True):
        return _skip("FACTORY_APPLY_CLAUDE=false — Claude 적용 명시적 비활성")

    # spec_bypass: when design_spec_acceptance has passed, the design
    # spec is the cycle's single source of truth. claude_proposal.md is
    # ignored even if present, and claude_propose's status is allowed to
    # be skipped (the ticket + design_spec carry enough detail).
    apply_spec_bypass = bool(
        state.design_spec_status == "generated"
        and state.design_spec_acceptance_passed
        and DESIGN_SPEC_FILE.is_file()
        and IMPLEMENTATION_TICKET_FILE.is_file()
    )

    # Pre-condition 2: this cycle must have produced a fresh proposal.
    # We refuse to apply a stale proposal from a previous run because
    # the working tree may have shifted underneath it.
    if not apply_spec_bypass and state.claude_proposal_status != "generated":
        return _skip(
            f"이번 사이클의 claude_propose가 generated 아님 ({state.claude_proposal_status}) — 적용 건너뜀"
        )

    # Pre-condition 2b: Implementation Ticket must be present with
    # concrete target files. Without a ticket, we don't know what the
    # cycle is supposed to write — so we refuse to let claude_apply
    # touch the working tree on speculation. The ticket stage already
    # logged implementation_ticket_missing so the operator can see why.
    if state.implementation_ticket_status != "generated":
        return _skip(
            "Implementation Ticket이 없어 claude_apply 건너뜀 — "
            "이번 사이클은 planning_only 로 종료됩니다."
        )
    if not state.implementation_ticket_target_files:
        return _skip(
            "Implementation Ticket 의 수정 대상 파일이 비어 있어 claude_apply 건너뜀"
        )

    # Pre-condition 3: don't apply on top of leaking files.
    if state.risky_files:
        return _skip(f"위험 파일 {len(state.risky_files)}건 감지 — 적용 건너뜀")

    # Pre-condition 4: don't apply on top of a broken build/syntax.
    failed_prior = [
        s for s in state.stages
        if s.status == "failed"
        and s.name in {"build_app", "build_control", "syntax_check", "git_check"}
    ]
    if failed_prior:
        names = ", ".join(s.name for s in failed_prior)
        return _skip(f"이전 단계 실패({names}) — 적용 건너뜀")

    # Pre-condition 4b: kernel-contract preflight. Run pure validators
    # over the cycle state BEFORE spending Claude budget. Failures here
    # short-circuit the stage with a specific code (scope_mismatch_
    # preflight / stale_artifact_preflight / missing_ticket_contract /
    # feature_lock_conflict) so the smoke / observer / dashboard report
    # can pinpoint which contract was violated. Without this, mismatches
    # were only detected AFTER the Claude apply when scope_consistency
    # rolled back the diff — wasting budget on every cycle.
    preflight = validate_apply_preflight(state)
    state.contract_results = [
        c for c in (state.contract_results or [])
        if c.get("name") != "apply_preflight"
    ] + [preflight]
    state.apply_preflight_status = preflight["code"]
    state.apply_preflight_reason = preflight["message"]
    if not preflight["ok"]:
        sr.status = "failed"
        sr.message = preflight["message"]
        sr.detail = json.dumps(preflight.get("evidence") or {}, ensure_ascii=False)
        sr.duration_sec = round(time.time() - t0, 3)
        state.claude_apply_status = preflight["code"]
        state.claude_apply_skipped_reason = preflight["message"]
        state.claude_apply_message = sr.message
        state.scope_consistency_status = "failed"
        state.scope_mismatch_reason = preflight["message"]
        state.failed_stage = "claude_apply"
        state.failed_reason = preflight["message"]
        _emit_cycle_log(
            state, "claude_apply_preflight_failed",
            f"claude_apply preflight failed — {preflight['code']}: "
            f"{preflight['message']}",
            preflight_code=preflight["code"],
            evidence=preflight.get("evidence") or {},
        )
        return sr

    # Pre-condition 5: tools + input source. Use the same parser as
    # stage_claude_preflight so a LOCAL_RUNNER_CLAUDE_COMMAND like
    # `claude --dangerously-skip-permissions` is split into argv —
    # treating it as a single executable path was the original bug
    # behind the Errno-2 "no such file" report.
    spec = _resolve_claude_command()
    if spec["missing"] or not spec["executable"]:
        missing_executable = spec["executable"] or "claude"
        payload = _write_claude_executor_state(
            status="failed",
            stage="claude_apply",
            command=spec["argv"] or [missing_executable],
            exit_code=None,
            timed_out=False,
            duration_sec=time.time() - t0,
            failure_code="claude_cli_missing",
            failure_reason=(
                f"claude executable not found at apply time: {missing_executable!r} "
                f"(source={spec['source']})."
            ),
            stdout_path=None,
            stderr_path=None,
            retryable=False,
            retry_count=int(state.claude_executor_retry_count or 0),
        )
        _apply_executor_state_to_cycle_state(state, payload)
        sr.status = "failed"
        sr.message = "claude CLI 미설치 — claude_apply 실패"
        state.claude_apply_status = "cli_failed"
        state.claude_apply_changed_files = []
        state.claude_apply_message = sr.message
        state.failed_stage = "claude_apply"
        state.failed_reason = payload["failure_reason"]
        sr.duration_sec = round(time.time() - t0, 3)
        return sr
    base_argv = list(spec["argv"])
    if apply_spec_bypass:
        try:
            design_spec_md = DESIGN_SPEC_FILE.read_text(encoding="utf-8")
            ticket_md = IMPLEMENTATION_TICKET_FILE.read_text(encoding="utf-8")
        except OSError as e:
            return _skip(f"design_spec / ticket 읽기 실패: {e}")
        proposal_text = _build_apply_input_from_design_spec(
            design_spec_md=design_spec_md,
            ticket_md=ticket_md,
            target_files=list(state.implementation_ticket_target_files),
        )
        state.claude_apply_source = "design_spec"
    else:
        if not PROPOSAL_FILE.is_file():
            return _skip("claude_proposal.md 없음 — 스킵")
        proposal_text = PROPOSAL_FILE.read_text(encoding="utf-8").strip()
        if not proposal_text:
            return _skip("제안 본문이 비어있음 — 스킵")
        state.claude_apply_source = "claude_proposal"

    # Snapshot before — we'll diff against this to know what to roll back.
    before_hashes = _hash_tracked_under_allowed()
    before_untracked = _untracked_under_allowed()

    prompt = _build_claude_apply_prompt(proposal_text)
    model = os.environ.get("FACTORY_CLAUDE_MODEL", "sonnet").strip() or "sonnet"
    # Stage-aware budget — claude_apply gets the largest cap (3.00 by
    # default) because multi-file diffs cost the most. CLAUDE_APPLY_
    # MAX_COST_USD overrides; FACTORY_CLAUDE_BUDGET_USD remains a
    # fallback for backwards compatibility.
    budget_usd, budget_source = get_claude_budget_usd("claude_apply")
    timeout_sec = float(os.environ.get("FACTORY_CLAUDE_APPLY_TIMEOUT_SEC", "900"))

    argv = _claude_argv_with(
        base_argv,
        [
            "-p", prompt,
            "--allowed-tools", "Read,Glob,Grep,Edit,Write",
            "--output-format", "text",
            "--model", model,
            "--max-budget-usd", budget_usd,
        ],
    )
    # Persist the parsed command spec — operators (and self-tests)
    # need to see raw_command vs argv vs executable separately to
    # diagnose preflight bugs without re-parsing env vars themselves.
    try:
        CLAUDE_APPLY_COMMAND_FILE.parent.mkdir(parents=True, exist_ok=True)
        CLAUDE_APPLY_COMMAND_FILE.write_text(
            json.dumps(
                {
                    "stage": "claude_apply",
                    "raw_command": spec["raw_command"],
                    "argv": list(argv),
                    "executable": spec["executable"],
                    "extra_args": list(argv[1:]),
                    "source": spec["source"],
                    "command": " ".join(shlex.quote(a) for a in argv),
                    "max_cost_usd": budget_usd,
                    "cost_budget_source": budget_source,
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass
    capture = _run_claude_capture(
        argv,
        cwd=REPO_ROOT,
        timeout=timeout_sec,
        stdout_path=CLAUDE_APPLY_STDOUT_FILE,
        stderr_path=CLAUDE_APPLY_STDERR_FILE,
    )
    apply_ok = bool(capture["ok"])
    apply_out = (capture["stdout"] or "") + (
        ("\n--stderr--\n" + capture["stderr"]) if capture["stderr"] else ""
    )

    # Whether or not the CLI succeeded, snapshot the diff: Claude may
    # have written partial changes before erroring out.
    changed_tracked, new_untracked = _diff_apply_changes(before_hashes, before_untracked)

    # Defense in depth: even if the CLI succeeded, scan for any path
    # that violates the apply policy. If found → forced rollback.
    forbidden_hits = [
        p for p in (changed_tracked + new_untracked)
        if _violates_apply_policy(p)
    ]
    if forbidden_hits:
        ok_rb, rb_msg = _rollback_apply(changed_tracked, new_untracked)
        sr.status = "failed"
        sr.message = (
            f"금지 경로 변경 감지 ({len(forbidden_hits)}건) — 강제 롤백"
            + ("" if ok_rb else " (일부 실패)")
        )
        sr.detail = "위반 경로:\n" + "\n".join(forbidden_hits[:10]) + "\n\n" + rb_msg
        state.claude_apply_status = "rolled_back"
        state.claude_apply_rollback = True
        state.claude_apply_changed_files = []
        state.claude_apply_message = sr.message
        sr.duration_sec = round(time.time() - t0, 3)
        return sr

    if not apply_ok:
        # CLI failed and didn't violate paths. Still roll back any
        # half-written changes so the next cycle starts clean.
        if changed_tracked or new_untracked:
            _rollback_apply(changed_tracked, new_untracked)
            state.claude_apply_rollback = True
        failure_code = classify_claude_failure(
            exit_code=capture["exit_code"],
            timed_out=capture["timed_out"],
            missing_bin=capture["missing_bin"],
            stdout=capture["stdout"],
            stderr=capture["stderr"],
        )
        retryable = _is_retryable_claude_failure(failure_code)
        reason_tail = (
            (capture["stderr"] or capture["stdout"] or "(no output)")
        ).strip()
        executor_status = (
            "timeout" if capture["timed_out"]
            else ("retryable_failed" if retryable else "failed")
        )
        payload = _write_claude_executor_state(
            status=executor_status,
            stage="claude_apply",
            command=argv,
            exit_code=capture["exit_code"],
            timed_out=capture["timed_out"],
            duration_sec=capture["duration_sec"],
            failure_code=failure_code,
            failure_reason=reason_tail,
            stdout_path=capture["stdout_path"],
            stderr_path=capture["stderr_path"],
            retryable=retryable,
            retry_count=int(state.claude_executor_retry_count or 0),
            max_cost_usd=budget_usd,
            cost_budget_source=budget_source,
            exceeded_budget=(failure_code == "claude_cli_budget_exceeded"),
        )
        _apply_executor_state_to_cycle_state(state, payload)
        sr.status = "failed"
        sr.message = f"claude CLI 실행 실패 — {failure_code}"
        sr.detail = (apply_out or "")[-1500:]
        state.claude_apply_status = "cli_failed"
        state.claude_apply_changed_files = []
        state.claude_apply_message = sr.message
        state.failed_stage = "claude_apply"
        state.failed_reason = (
            f"claude_apply CLI failure ({failure_code}): {reason_tail[:300]}"
        )
        _emit_cycle_log(
            state, "claude_apply_cli_failed",
            f"claude_apply CLI failed — {failure_code} (retryable={retryable})",
            failure_code=failure_code,
            retryable=retryable,
            exit_code=capture["exit_code"],
            timed_out=capture["timed_out"],
        )
        sr.duration_sec = round(time.time() - t0, 3)
        return sr

    # Claude succeeded but didn't actually touch anything. Previously
    # we recorded this as `noop` (a passing terminal state) — that
    # masked a real failure mode where soft HOLD cycles produce
    # design_spec → ticket → propose but never an actual diff. We now
    # mark the apply as `retry_required` so the next cycle picks this
    # up as unfinished work. Stage status stays `skipped` so we don't
    # cascade into a hard cycle failure on a self-recoverable condition.
    if not changed_tracked and not new_untracked:
        sr.status = "skipped"
        sr.message = (
            "claude가 어떤 파일도 변경하지 않음 — retry_required (soft HOLD에서는 "
            "다음 사이클이 같은 ticket 으로 재시도)"
        )
        sr.detail = (apply_out or "")[-800:]
        state.claude_apply_status = "retry_required"
        state.claude_apply_skipped_reason = "claude_apply produced no diff"
        state.claude_apply_message = sr.message
        _emit_cycle_log(
            state, "claude_apply_retry_required",
            "claude apply produced no diff — marking retry_required",
        )
        sr.duration_sec = round(time.time() - t0, 3)
        return sr

    # Re-validate — the heart of the sandbox. If anything broke,
    # rollback the entire change set.
    _emit_cycle_log(
        state, "validation_started",
        "validation started — _revalidate_after_apply (build / py_compile / 위험파일 스캔)",
        files=(changed_tracked + new_untracked)[:30],
    )
    revalidate_ok, failures = _revalidate_after_apply()
    if not revalidate_ok:
        # Snapshot the diff that's about to vanish — without this, the
        # operator (and the next cycle's repair prompt) has no record of
        # which patch broke the build_app revalidation.
        diff_files_pre_rb = changed_tracked + new_untracked
        try:
            ok_diff, diff_pre_rb = _run(
                ["git", "-C", str(REPO_ROOT), "diff", "HEAD", "--",
                 *diff_files_pre_rb],
                timeout=60,
            )
            if ok_diff:
                APPLY_ROLLED_BACK_DIFF_FILE.parent.mkdir(
                    parents=True, exist_ok=True,
                )
                APPLY_ROLLED_BACK_DIFF_FILE.write_text(
                    diff_pre_rb or "(empty diff)", encoding="utf-8",
                )
        except OSError:
            pass

        ok_rb, rb_msg = _rollback_apply(changed_tracked, new_untracked)
        sr.status = "failed"
        sr.message = (
            f"재검증 실패 ({', '.join(failures)}) — 롤백"
            + ("" if ok_rb else " (일부 실패)")
        )
        sr.detail = rb_msg
        state.claude_apply_status = "rolled_back"
        state.claude_apply_rollback = True
        state.claude_apply_changed_files = []
        state.claude_apply_message = sr.message
        # Surface the rolled-back diff path so factory_smoke / observer
        # can link to the forensic artifact in their reports.
        if APPLY_ROLLED_BACK_DIFF_FILE.is_file():
            state.claude_apply_diff_path = str(APPLY_ROLLED_BACK_DIFF_FILE)
        sr.duration_sec = round(time.time() - t0, 3)
        return sr

    diff_files = changed_tracked + new_untracked
    ok, diff_out = _run(
        ["git", "-C", str(REPO_ROOT), "diff", "HEAD", "--", *diff_files],
        timeout=60,
    )

    # Planner-mode check: did claude *actually* build the selected
    # feature, or did it sneak in a label tweak? Only enforced when
    # this cycle's product_planning stage produced a fresh validated
    # report — in plain mode (no planner) we keep the looser legacy
    # behavior so existing flows still work.
    if state.product_planner_status in {"generated", "fallback_generated"} and ok:
        meaningful, criteria_met, why = _evaluate_apply_meaningfulness(
            diff_out or "",
            diff_files,
            state.product_planner_selected_feature,
        )
        if not meaningful:
            ok_rb, rb_msg = _rollback_apply(changed_tracked, new_untracked)
            sr.status = "failed"
            sr.message = "Product Planner Mode에서 단순 문구 수정으로 판단되어 롤백"
            sr.detail = (
                f"사유: {why}\n"
                f"충족된 기준: {criteria_met if criteria_met else '없음'}\n"
                f"{rb_msg}"
            )
            state.claude_apply_status = "rolled_back"
            state.claude_apply_rollback = True
            state.claude_apply_changed_files = []
            state.claude_apply_message = sr.message
            sr.duration_sec = round(time.time() - t0, 3)
            return sr

    # spec_bypass scope-consistency QA gate. Verifies the diff actually
    # builds what design_spec said: changed_files ∩ design_spec.target
    # files, selected_feature alignment, and ≥3 design_spec keywords in
    # the diff body. Failure rolls back the apply and routes verdict to
    # FAIL/scope_mismatch — the operator must reconcile the spec / proposal
    # mismatch before another cycle runs.
    if apply_spec_bypass:
        try:
            ds_md = DESIGN_SPEC_FILE.read_text(encoding="utf-8")
        except OSError:
            ds_md = ""
        ds_targets = _extract_design_spec_target_files(ds_md)
        ds_feature = _extract_design_spec_feature(ds_md)
        sel_feature = (
            state.implementation_ticket_selected_feature
            or state.selected_feature
        )
        scope_ok, scope_reason, kw_matched, kw_total = _check_scope_consistency(
            design_spec_md=ds_md,
            design_spec_target_files=ds_targets,
            design_spec_feature=ds_feature,
            diff_text=(diff_out or ""),
            changed_files=diff_files,
            selected_feature=sel_feature,
        )
        state.scope_consistency_keywords_matched = list(kw_matched)
        state.scope_consistency_keywords_total = kw_total
        if not scope_ok:
            ok_rb, rb_msg = _rollback_apply(changed_tracked, new_untracked)
            sr.status = "failed"
            sr.message = (
                "스코프 일관성 검증 실패 — design_spec 과 무관한 변경으로 "
                "판단되어 롤백"
                + ("" if ok_rb else " (일부 실패)")
            )
            sr.detail = f"사유: {scope_reason}\n{rb_msg}"
            state.claude_apply_status = "rolled_back"
            state.claude_apply_rollback = True
            state.claude_apply_changed_files = []
            state.claude_apply_message = sr.message
            state.scope_consistency_status = "failed"
            state.scope_mismatch_reason = scope_reason
            state.failed_stage = "claude_apply"
            state.failed_reason = scope_reason
            _emit_cycle_log(
                state, "claude_apply_scope_mismatch",
                "claude apply rolled back — scope mismatch",
                reason=scope_reason,
                changed_files=diff_files[:20],
                target_files=list(ds_targets[:20]),
                keywords_matched=kw_matched[:10],
                keywords_total=kw_total,
            )
            sr.duration_sec = round(time.time() - t0, 3)
            return sr
        state.scope_consistency_status = "passed"
        state.scope_mismatch_reason = None

    # Success path. Save the diff so the human reviewer has the full
    # patch in one place. NOT committed, NOT pushed.
    if ok:
        APPLY_DIFF_FILE.write_text(diff_out or "", encoding="utf-8")
        state.claude_apply_diff_path = str(APPLY_DIFF_FILE)
    else:
        state.claude_apply_diff_path = None

    state.claude_apply_status = "applied"
    state.claude_apply_at = utc_now_iso()
    state.claude_apply_changed_files = diff_files
    state.claude_apply_rollback = False
    state.claude_apply_message = (
        f"{len(diff_files)}개 파일 변경, 빌드/문법/위험 파일 재검증 통과"
        + (
            f" + scope_consistency=passed ("
            f"{len(state.scope_consistency_keywords_matched)}/"
            f"{state.scope_consistency_keywords_total} keywords)"
            if apply_spec_bypass else ""
        )
    )
    # Mirror the successful CLI run onto the executor contract so the
    # autopilot retry policy + dashboard see status=passed (clearing any
    # prior retryable_failed verdict from an earlier cycle).
    success_payload = _write_claude_executor_state(
        status="passed",
        stage="claude_apply",
        command=argv,
        exit_code=capture["exit_code"],
        timed_out=False,
        duration_sec=capture["duration_sec"],
        failure_code=None,
        failure_reason=None,
        stdout_path=capture["stdout_path"],
        stderr_path=capture["stderr_path"],
        retryable=False,
        retry_count=int(state.claude_executor_retry_count or 0),
        max_cost_usd=budget_usd,
        cost_budget_source=budget_source,
        exceeded_budget=False,
    )
    _apply_executor_state_to_cycle_state(state, success_payload)

    _emit_cycle_log(
        state, "validation_passed",
        f"validation passed — {len(diff_files)}개 파일에 대한 빌드/문법 재검증 통과",
        files=diff_files[:30],
    )
    _emit_cycle_log(
        state, "claude_apply_changed_files",
        f"claude apply changed files — {len(diff_files)}개 파일 (model={model})",
        files=diff_files[:30],
    )

    sr.status = "passed"
    sr.message = (
        f"적용 성공 — {len(diff_files)}개 파일, 재검증 통과 (model={model})"
    )
    sr.detail = "\n".join(f"- {p}" for p in diff_files[:20])
    sr.duration_sec = round(time.time() - t0, 3)
    return sr


# ---------------------------------------------------------------------------
# Stampport QA Gatekeeper stage
#
# Verifies that the cycle's output is actually shippable as a Stampport
# build, not just "compiles + builds". Five sub-checks run inside one
# stage:
#
#   1. build_artifact   — index.html + asset files exist + non-zero
#                         in app/web/dist and control_tower/web/dist.
#   2. api_health       — py_compile app/api/app/main.py and confirm a
#                         /health route is declared in source.
#   3. screen_presence  — Stampport 8 MVP screens (Landing/Login/
#                         StampForm/StampResult/MyPassport/Badges/
#                         Quests/Share) exist under app/web/src/screens.
#   4. flow_presence    — mock login / stamp creation / passport /
#                         badges / quests / share card flows have
#                         keyword evidence under app/web/src.
#   5. domain_profile   — config/domain_profiles/stampport.json +
#                         docs/agent-collaboration.md exist and the
#                         profile carries the expected agent roster.
#
# On any failure the stage writes qa_feedback.md with a precise
# repro and remediation list, and signals downstream stages
# (qa_fix_propose / qa_fix_apply / qa_recheck) to attempt repair.
# The publish path in runner.py refuses to ship when this stage
# isn't passed.
#
# Note: full browser-driven E2E (Playwright/Selenium) is intentionally
# NOT wired in (would require an unattended browser install + extra
# port juggling). The Stampport MVP gates rely on static analysis +
# py_compile because that is enough to verify the bundle is structurally
# Stampport.
# ---------------------------------------------------------------------------


# Stampport MVP screens that must exist as files in app/web/src/screens/
STAMPPORT_REQUIRED_SCREENS: tuple[str, ...] = (
    "Landing.jsx",
    "Login.jsx",
    "StampForm.jsx",
    "StampResult.jsx",
    "MyPassport.jsx",
    "Badges.jsx",
    "Quests.jsx",
    "Share.jsx",
)

# Core flow keywords. Each entry is (flow_label, list of substrings any
# of which must appear somewhere under app/web/src). Keeps the check
# resilient to file moves — we only require evidence in the bundle.
STAMPPORT_FLOW_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mock_login",     ("login", "nickname", "email")),
    ("stamp_creation", ("addStamp", "place_name", "kick_points")),
    ("stamp_result",   ("StampResult", "stamp-card", "kick-list")),
    ("passport",       ("MyPassport", "passport-summary", "ps-stats")),
    ("badges",         ("computeBadges", "badge", "earned")),
    ("quests",         ("computeQuests", "quest", "weekly")),
    ("share_card",     ("Share", "share-canvas", "share-stamp")),
)


def _qa_check_api_health() -> tuple[str, str, dict]:
    """Stampport API health gate.

    1. py_compile app/api/app/main.py (catches obvious syntax breakage).
    2. Verify FastAPI source declares a /health route — no live HTTP
       call needed, the gate trusts the source contract.

    Returns (status, message, detail_dict)."""
    api_main = REPO_ROOT / "app" / "api" / "app" / "main.py"
    detail: dict = {
        "main_py_path": str(api_main),
        "compiles": False,
        "health_route": False,
    }
    if not api_main.is_file():
        return "failed", "app/api/app/main.py 없음", detail

    try:
        r = subprocess.run(
            [sys.executable, "-m", "py_compile", str(api_main)],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception as e:  # noqa: BLE001
        return "failed", f"py_compile 실행 실패: {e}", detail
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()[-1:]
        return ("failed",
                f"app/api/app/main.py py_compile 실패: {(msg[0] if msg else '')[:160]}",
                detail)
    detail["compiles"] = True

    try:
        src = api_main.read_text(encoding="utf-8")
    except OSError as e:
        return ("failed",
                f"app/api/app/main.py 읽기 실패: {e}",
                detail)
    has_health_route = bool(
        re.search(r'@(?:app|router)\.get\(\s*["\']/health["\']', src)
        or re.search(r'add_api_route\(\s*["\']/health["\']', src)
        or re.search(r'@(?:app|router)\.api_route\(\s*["\']/health["\']', src)
    )
    detail["health_route"] = has_health_route
    if not has_health_route:
        return ("failed",
                "/health 엔드포인트가 app/api/app/main.py 소스에 없음",
                detail)
    return "passed", "py_compile 통과 + /health 라우트 확인", detail


def _qa_check_screen_presence() -> tuple[str, str, dict]:
    """Verify the Stampport MVP screens exist."""
    screens_dir = REPO_ROOT / "app" / "web" / "src" / "screens"
    detail: dict = {
        "screens_dir": str(screens_dir),
        "missing": [],
        "found": [],
    }
    if not screens_dir.is_dir():
        detail["missing"] = list(STAMPPORT_REQUIRED_SCREENS)
        return "failed", "app/web/src/screens 디렉터리 없음", detail
    for name in STAMPPORT_REQUIRED_SCREENS:
        path = screens_dir / name
        if path.is_file() and path.stat().st_size > 0:
            detail["found"].append(name)
        else:
            detail["missing"].append(name)
    if detail["missing"]:
        return ("failed",
                f"필수 화면 누락 {len(detail['missing'])}건: {detail['missing'][:3]}",
                detail)
    return ("passed",
            f"Stampport 화면 {len(STAMPPORT_REQUIRED_SCREENS)}개 모두 존재",
            detail)


def _qa_check_flow_presence() -> tuple[str, str, dict]:
    """Make sure the Stampport core loops are wired in code."""
    web_root = REPO_ROOT / "app" / "web" / "src"
    detail: dict = {"flows": {}}
    if not web_root.is_dir():
        return "failed", "app/web/src 없음", detail
    chunks: list[str] = []
    for p in web_root.rglob("*"):
        if not p.is_file() or p.suffix not in {".js", ".jsx"}:
            continue
        try:
            chunks.append(p.read_text(encoding="utf-8"))
        except OSError:
            continue
    corpus = "\n".join(chunks)
    failures: list[str] = []
    for label, needles in STAMPPORT_FLOW_KEYWORDS:
        hits = sum(1 for needle in needles if needle in corpus)
        detail["flows"][label] = hits
        if hits == 0:
            failures.append(label)
    if failures:
        detail["missing_flows"] = failures
        return ("failed",
                f"코드상 누락된 핵심 흐름: {failures}",
                detail)
    return ("passed",
            "mock login / stamp / passport / badges / quests / share 흐름이 코드상 존재",
            detail)


def _qa_check_domain_profile() -> tuple[str, str, dict]:
    """Stampport identity guard — the planner/designer agents rely on
    config/domain_profiles/stampport.json + docs/agent-collaboration.md
    being in place. If either is missing or malformed, the factory loses
    its product context and produces drifty output."""
    profile = STAMPPORT_DOMAIN_PROFILE_PATH
    collab = STAMPPORT_AGENT_COLLAB_PATH
    detail: dict = {
        "profile_exists": profile.is_file(),
        "collab_exists": collab.is_file(),
    }
    if not profile.is_file():
        return "failed", "config/domain_profiles/stampport.json 없음", detail
    if not collab.is_file():
        return "failed", "docs/agent-collaboration.md 없음", detail
    try:
        data = json.loads(profile.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return "failed", f"stampport.json 파싱 실패: {e}", detail

    required_keys = (
        "core_loop",
        "mvp_screens",
        "agents",
        "qa_emotional_checks",
        "factory_pipeline_stages",
    )
    missing_keys = [k for k in required_keys if k not in data]
    if missing_keys:
        detail["missing_keys"] = missing_keys
        return ("failed",
                f"stampport.json 필수 키 누락: {missing_keys}",
                detail)
    # Spot-check that the agent roster in profile matches the Stampport 8.
    expected_agent_ids = {
        "pm", "planner", "designer", "frontend",
        "backend", "ai_architect", "qa", "deploy",
    }
    actual_agent_ids = {
        a.get("id") for a in (data.get("agents") or [])
        if isinstance(a, dict)
    }
    missing_agents = sorted(expected_agent_ids - actual_agent_ids)
    if missing_agents:
        detail["missing_agents"] = missing_agents
        return ("failed",
                f"stampport.json agents 항목에 {missing_agents} 누락",
                detail)
    detail["agent_count"] = len(actual_agent_ids)
    return "passed", "domain profile + agent collaboration 문서 정상", detail


def _qa_check_build_artifacts() -> tuple[str, str, list[str]]:
    """Verify dist/ contents for both web apps. Returns
    (status, message, missing_paths)."""
    missing: list[str] = []

    def _check_dist(dist_dir: Path, label: str) -> None:
        # If the directory doesn't exist at all, treat as failure —
        # `npm run build` wasn't run or failed without leaving a dist.
        if not dist_dir.is_dir():
            missing.append(f"{label}: dist 디렉터리 없음 ({dist_dir})")
            return
        index_html = dist_dir / "index.html"
        if not index_html.is_file() or index_html.stat().st_size == 0:
            missing.append(f"{label}: index.html 없음 또는 빈 파일")
            return
        # Pull asset references out of index.html to make sure they
        # actually point to existing files.
        try:
            html = index_html.read_text(encoding="utf-8")
        except OSError as e:
            missing.append(f"{label}: index.html 읽기 실패 — {e}")
            return
        refs = re.findall(r'(?:src|href)\s*=\s*["\']([^"\']+)["\']', html)
        # Filter for local asset paths under /assets/ — drop external
        # links and absolute URLs.
        local_refs = [r for r in refs if "/assets/" in r and "://" not in r]
        if not local_refs:
            # An empty assets list is suspicious for a non-trivial app.
            missing.append(f"{label}: index.html이 /assets/ 자원을 참조하지 않음")
            return
        for ref in local_refs:
            rel = ref.lstrip("/")
            target = dist_dir / rel if (dist_dir / rel).exists() else dist_dir / Path(rel).name
            # Vite emits absolute /assets/foo.js style refs that resolve
            # against the dist root.
            asset_path = dist_dir / "assets" / Path(ref).name
            if asset_path.is_file() and asset_path.stat().st_size > 0:
                continue
            if target.is_file() and target.stat().st_size > 0:
                continue
            missing.append(f"{label}: 참조 자원 없음 → {ref}")
        # Also require that at least one .js and one .css asset exists.
        assets = list((dist_dir / "assets").glob("*")) if (dist_dir / "assets").is_dir() else []
        has_js = any(p.suffix == ".js" and p.stat().st_size > 0 for p in assets)
        has_css = any(p.suffix == ".css" and p.stat().st_size > 0 for p in assets)
        if not has_js:
            missing.append(f"{label}: dist/assets 안에 비어있지 않은 .js 없음")
        if not has_css:
            missing.append(f"{label}: dist/assets 안에 비어있지 않은 .css 없음")

    _check_dist(REPO_ROOT / "app" / "web" / "dist", "app/web")
    _check_dist(REPO_ROOT / "control_tower" / "web" / "dist", "control_tower/web")

    if missing:
        return "failed", "; ".join(missing[:3]), missing
    return "passed", "app/web + control_tower/web dist 검증 통과", []




def _read_qa_fix_state() -> dict:
    if not QA_FIX_STATE_FILE.is_file():
        return {}
    try:
        return json.loads(QA_FIX_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_qa_fix_state(data: dict) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    try:
        QA_FIX_STATE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _write_qa_report(state: CycleState, *, detail: dict) -> None:
    """Render qa_report.md from the captured state + detail dict.

    Stampport QA report: build / api_health / screen_presence /
    flow_presence / domain_profile."""
    lines: list[str] = [
        "# Stampport QA Report",
        "",
        f"_사이클 #{state.cycle} · {utc_now_iso()}_",
        "",
        "## 최종 판정",
        f"- status: {state.qa_status}",
        f"- publish_allowed: {'true' if state.qa_publish_allowed else 'false'}",
        f"- failed_reason: {state.qa_failed_reason or '없음'}",
        "",
        "## 변경 파일",
    ]
    changed = detail.get("changed_files") or []
    if changed:
        for f in changed[:30]:
            lines.append(f"- `{f}`")
    else:
        lines.append("- (없음)")

    ba = detail.get("build_artifact") or {}
    lines += [
        "",
        "## Build Artifact",
        f"- app/web dist: {ba.get('app_web', state.qa_build_artifact)}",
        f"- control_tower/web dist: {ba.get('control_tower_web', state.qa_build_artifact)}",
        f"- asset references: {ba.get('asset_refs', state.qa_build_artifact)}",
    ]
    if ba.get("missing"):
        lines.append("- 누락:")
        for m in ba["missing"][:10]:
            lines.append(f"  - {m}")

    api = detail.get("api_health") or {}
    lines += [
        "",
        "## API Health",
        f"- result: {state.qa_api_health}",
        f"- main.py compiles: {'true' if api.get('compiles') else 'false'}",
        f"- /health route: {'true' if api.get('health_route') else 'false'}",
        f"- main.py path: {api.get('main_py_path', '(unknown)')}",
    ]

    sp = detail.get("screen_presence") or {}
    lines += [
        "",
        "## Screen Presence",
        f"- result: {state.qa_screen_presence}",
        f"- found: {sp.get('found') or []}",
        f"- missing: {sp.get('missing') or []}",
    ]

    fl = detail.get("flow_presence") or {}
    lines += [
        "",
        "## Flow Presence",
        f"- result: {state.qa_flow_presence}",
    ]
    flows = (fl.get("flows") or {})
    for label, hits in flows.items():
        lines.append(f"  - {label}: {hits}건 매칭")
    if fl.get("missing_flows"):
        lines.append(f"- 누락된 흐름: {fl['missing_flows']}")

    dp = detail.get("domain_profile") or {}
    lines += [
        "",
        "## Domain Profile",
        f"- result: {state.qa_domain_profile}",
        f"- stampport.json 존재: {'true' if dp.get('profile_exists') else 'false'}",
        f"- agent-collaboration.md 존재: {'true' if dp.get('collab_exists') else 'false'}",
    ]
    if dp.get("missing_keys"):
        lines.append(f"- stampport.json 누락 키: {dp['missing_keys']}")
    if dp.get("missing_agents"):
        lines.append(f"- 누락된 에이전트: {dp['missing_agents']}")

    lines += ["", "## 실패 상세"]
    if state.qa_failed_categories:
        for cat in state.qa_failed_categories:
            lines.append(f"- {cat}")
    else:
        lines.append("- 없음")

    lines += ["", "## 다음 조치"]
    if state.qa_status == "passed":
        lines.append("- QA 통과 — 배포 가능 상태입니다.")
    else:
        lines.append("- qa_feedback.md 를 참고해 누락된 화면/흐름/엔드포인트를 보강하세요.")
        lines.append(
            f"- QA 수정 시도: {state.qa_fix_attempt}/{state.qa_fix_max_attempts}"
        )

    QA_REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_qa_feedback(state: CycleState, *, detail: dict) -> None:
    """Write qa_feedback.md — the doc qa_fix_propose consumes.

    Stampport-flavoured: surface the missing screens/flows/endpoints/
    domain-profile gaps so the next claude proposal works on the right
    file set."""
    api = detail.get("api_health") or {}
    sp = detail.get("screen_presence") or {}
    fl = detail.get("flow_presence") or {}
    dp = detail.get("domain_profile") or {}
    ba = detail.get("build_artifact") or {}

    failure_types: list[str] = []
    if state.qa_build_artifact == "failed":   failure_types.append("Build Artifact")
    if state.qa_api_health == "failed":       failure_types.append("API Health")
    if state.qa_screen_presence == "failed":  failure_types.append("Screen Presence")
    if state.qa_flow_presence == "failed":    failure_types.append("Flow Presence")
    if state.qa_domain_profile == "failed":   failure_types.append("Domain Profile")

    lines: list[str] = [
        "# Stampport QA Feedback",
        "",
        "## 최종 판정",
        "failed",
        "",
        "## 실패 유형",
    ]
    if failure_types:
        lines += [f"- {t}" for t in failure_types]
    else:
        lines.append("- (분류 없음)")

    lines += ["", "## 실패 원인", state.qa_failed_reason or "(상세 없음)"]

    lines += [
        "",
        "## 재현 방법",
        "1. `cd app/web && npm run build` — Stampport 프론트 빌드가 통과하는지 확인.",
        "2. `cd app/api && python -m py_compile app/main.py` — FastAPI main 모듈 컴파일.",
        "3. `app/api/app/main.py` 안에 `@app.get('/health')` 또는 동등한 라우트가 있는지 확인.",
        "4. `app/web/src/screens/` 아래 Stampport 8개 화면 파일이 존재하는지 확인.",
        "5. mock login / addStamp / computeBadges / computeQuests / Share 카드 코드가 `app/web/src` 어딘가에 있는지 확인.",
        "6. `config/domain_profiles/stampport.json` 과 `docs/agent-collaboration.md` 가 존재하는지 확인.",
    ]

    lines += ["", "## 프론트 수정 요청"]
    if sp.get("missing"):
        lines.append(f"- 누락된 화면: {sp['missing']}")
        for name in sp["missing"]:
            lines.append(f"  - `app/web/src/screens/{name}` 를 추가하세요.")
    if fl.get("missing_flows"):
        lines.append(f"- 누락된 핵심 흐름: {fl['missing_flows']}")
        lines.append(
            "  - 각 흐름은 mock login / 스탬프 생성 / 결과 카드 / 내 여권 / 뱃지 / 퀘스트 / 공유카드 중 하나입니다."
        )
    if not sp.get("missing") and not fl.get("missing_flows"):
        lines.append("- (프론트 추가 작업 없음)")

    lines += ["", "## 백엔드 수정 요청"]
    if api.get("compiles") is False:
        lines.append("- `app/api/app/main.py` 가 py_compile 단계에서 실패합니다. 우선 컴파일을 통과시키세요.")
    if api.get("health_route") is False:
        lines.append("- FastAPI 앱에 `@app.get('/health')` 엔드포인트를 추가하세요. (응답 예: `{\"ok\": true}`)")
    if api.get("compiles") and api.get("health_route"):
        lines.append("- (백엔드 추가 작업 없음)")

    lines += ["", "## 빌드/도메인 프로파일 수정 요청"]
    if ba.get("missing"):
        for m in ba["missing"]:
            lines.append(f"- {m}")
    if dp.get("profile_exists") is False:
        lines.append("- `config/domain_profiles/stampport.json` 을 생성하세요.")
    if dp.get("collab_exists") is False:
        lines.append("- `docs/agent-collaboration.md` 를 생성하세요.")
    if dp.get("missing_keys"):
        lines.append(
            f"- stampport.json 에 `{dp['missing_keys']}` 키들을 추가하세요."
        )
    if dp.get("missing_agents"):
        lines.append(
            f"- stampport.json `agents` 항목에 `{dp['missing_agents']}` 추가."
        )
    if not (ba.get("missing") or dp.get("profile_exists") is False or dp.get("collab_exists") is False or dp.get("missing_keys") or dp.get("missing_agents")):
        lines.append("- (빌드/도메인 추가 작업 없음)")

    lines += [
        "",
        "## 금지할 수정",
        "- Stampport 정체성을 흐트러뜨리는 변경 (지도/리뷰/관리자/할 일 앱 톤).",
        "- 기획자/디자이너 ping-pong 흐름을 우회하는 단순 라벨 변경.",
        "- 자동 commit/push.",
    ]

    lines += [
        "",
        "## 재검증 조건",
        "- app/web build 통과 + dist/index.html에 .js/.css 자원 매칭.",
        "- app/api/app/main.py py_compile 통과 + /health 라우트 존재.",
        "- Stampport 8개 화면 (Landing/Login/StampForm/StampResult/MyPassport/Badges/Quests/Share) 존재.",
        "- mock login / stamp / passport / badges / quests / share 흐름 코드 존재.",
        "- config/domain_profiles/stampport.json + docs/agent-collaboration.md 정상.",
    ]

    QA_FEEDBACK_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def stage_qa_gate(state: CycleState) -> StageResult:
    """Run all Stampport QA Gatekeeper sub-checks and write qa_report.md +
    (on failure) qa_feedback.md.

    Sub-checks (Stampport MVP):
        1. build_artifact   — app/web + control_tower/web dist 산출물
        2. api_health       — app/api/app/main.py py_compile + /health 라우트
        3. screen_presence  — Stampport 8개 화면 파일 존재
        4. flow_presence    — mock login / stamp / passport / badges / quests / share 코드 존재
        5. domain_profile   — config/domain_profiles/stampport.json + docs/agent-collaboration.md
    """
    label = next(lab for n, lab, _ in STAGES if n == "qa_gate")
    sr = StageResult(name="qa_gate", label=label, status="running")
    t0 = time.time()

    detail: dict = {}

    # Changed files snapshot — surfaced in the report header.
    ok, ch = _run(["git", "-C", str(REPO_ROOT), "diff", "HEAD", "--name-only"], timeout=15)
    detail["changed_files"] = (
        [p.strip() for p in ch.splitlines() if p.strip()] if ok else []
    )

    # 1. Build artifact (unchanged from previous behaviour — verifies
    #    both app/web and control_tower/web dist trees).
    ba_status, ba_msg, missing = _qa_check_build_artifacts()
    state.qa_build_artifact = ba_status
    detail["build_artifact"] = {
        "status": ba_status, "message": ba_msg, "missing": missing,
        "app_web": ba_status, "control_tower_web": ba_status,
        "asset_refs": ba_status,
    }

    # 2. API health — py_compile main.py + /health route presence.
    api_status, api_msg, api_detail = _qa_check_api_health()
    state.qa_api_health = api_status
    detail["api_health"] = {**api_detail, "message": api_msg}

    # 3. Screen presence — Stampport MVP 8 screens.
    sp_status, sp_msg, sp_detail = _qa_check_screen_presence()
    state.qa_screen_presence = sp_status
    detail["screen_presence"] = {**sp_detail, "message": sp_msg}

    # 4. Flow presence — mock login / stamp / passport / badges / quests / share.
    fl_status, fl_msg, fl_detail = _qa_check_flow_presence()
    state.qa_flow_presence = fl_status
    detail["flow_presence"] = {**fl_detail, "message": fl_msg}

    # 5. Domain profile — Stampport identity guard.
    dp_status, dp_msg, dp_detail = _qa_check_domain_profile()
    state.qa_domain_profile = dp_status
    detail["domain_profile"] = {**dp_detail, "message": dp_msg}

    # Aggregate
    failed_categories: list[str] = []
    if ba_status == "failed":  failed_categories.append("Build Artifact")
    if api_status == "failed": failed_categories.append("API Health")
    if sp_status == "failed":  failed_categories.append("Screen Presence")
    if fl_status == "failed":  failed_categories.append("Flow Presence")
    if dp_status == "failed":  failed_categories.append("Domain Profile")

    state.qa_failed_categories = failed_categories
    if failed_categories:
        state.qa_status = "failed"
        state.qa_publish_allowed = False
        first_msg = (
            (api_msg if api_status == "failed" else None)
            or (sp_msg if sp_status == "failed" else None)
            or (fl_msg if fl_status == "failed" else None)
            or (dp_msg if dp_status == "failed" else None)
            or (ba_msg if ba_status == "failed" else None)
            or "QA 검사 실패"
        )
        state.qa_failed_reason = first_msg
    else:
        state.qa_status = "passed"
        state.qa_publish_allowed = True
        state.qa_failed_reason = None

    state.qa_report_path = str(QA_REPORT_FILE)
    _write_qa_report(state, detail=detail)
    if state.qa_status == "failed":
        _write_qa_feedback(state, detail=detail)
        state.qa_feedback_path = str(QA_FEEDBACK_FILE)
    else:
        # Success this cycle — clear any prior feedback file path so
        # the dashboard doesn't keep showing stale text.
        state.qa_feedback_path = None

    sr.duration_sec = round(time.time() - t0, 3)
    if state.qa_status == "passed":
        sr.status = "passed"
        sr.message = (
            "Stampport QA 통과 — build / api_health / screens / flows / domain 모두 OK"
        )
    else:
        sr.status = "failed"
        sr.message = state.qa_failed_reason or "Stampport QA 실패"
    sr.detail = "\n".join(
        f"- {k}: {v}"
        for k, v in {
            "build_artifact": ba_status,
            "api_health": api_status,
            "screen_presence": sp_status,
            "flow_presence": fl_status,
            "domain_profile": dp_status,
        }.items()
    )
    return sr


def stage_qa_feedback(state: CycleState) -> StageResult:
    """Pass-through stage that confirms qa_feedback.md was produced
    when qa_gate failed. Skipped on QA success."""
    label = next(lab for n, lab, _ in STAGES if n == "qa_feedback")
    sr = StageResult(name="qa_feedback", label=label, status="running")
    t0 = time.time()
    sr.duration_sec = round(time.time() - t0, 3)

    if state.qa_status != "failed":
        sr.status = "skipped"
        sr.message = "QA 실패 아님 — feedback 미작성"
        return sr
    if not QA_FEEDBACK_FILE.is_file():
        sr.status = "failed"
        sr.message = "qa_feedback.md 미생성 — qa_gate 단계가 비정상 종료됨"
        return sr
    sr.status = "passed"
    sr.message = (
        f"qa_feedback.md 작성됨 — 실패 분류: {', '.join(state.qa_failed_categories) or '?'}"
    )
    sr.detail = f"파일: {QA_FEEDBACK_FILE}"
    state.qa_feedback_path = str(QA_FEEDBACK_FILE)
    return sr


# Prompt template the qa_fix_propose stage feeds claude. It places
# qa_feedback.md FIRST in the context so claude's plan is anchored
# to the verified failure, not whatever the planner suggested.
QA_FIX_PROPOSE_PROMPT_TEMPLATE = """\
당신은 Stampport 프로젝트의 QA 수정 계획을 작성하는 Claude Code 입니다.

Stampport는 카페·빵집·맛집·디저트 방문을 여권 도장처럼 모으는 로컬 취향 RPG 서비스입니다.
어떤 수정 제안도 Stampport 정체성(passport / stamp / RPG / 감성 공유 카드)을 흐트러뜨려서는 안 됩니다.

⚠️ QA Gate가 실패했습니다. 다음 QA Feedback이 최우선 입력입니다.
이 Feedback에 적힌 실패 유형/원인/금지 수정 항목을 반드시 따르세요.

=== START qa_feedback.md ===
{feedback}
=== END qa_feedback.md ===

규칙:
- 어떤 파일도 수정하지 마세요. 사용 가능한 도구는 Read, Glob, Grep 뿐입니다.
- Stampport QA 실패 유형별 변경 범위:
  - API Health 실패 → app/api/app/main.py (py_compile / /health 라우트)
  - Screen Presence 실패 → app/web/src/screens 아래에 누락 화면 추가
  - Flow Presence 실패 → app/web/src 아래에 mock login / stamp / passport / badges / quests / share 흐름 보강
  - Domain Profile 실패 → config/domain_profiles/stampport.json / docs/agent-collaboration.md 보강
  - Build Artifact 실패 → 관련 build 설정 또는 자원 참조
- 200줄 이하의 코드 변경으로 구현 가능해야 합니다.
- 기존 응답 필드 타입 변경/제거 금지. 새 구조가 필요하면 기존 필드 유지 + 신규 필드 추가.

다음 정확한 구조의 Markdown만 출력하세요. preamble/설명 금지:

# Stampport QA Fix 제안

## 실패 요약
(qa_feedback의 실패 유형 + 원인 한 문단)

## 수정 제안
(어떻게 고칠지. 의사코드 가능. 한 가지 안만)

## 변경 대상 파일
- `path/to/file.py` — 무엇을 바꿀지 한 줄
- ...

## 호환성 보장 방법
(기존 필드/엔드포인트가 그대로 동작한다는 근거)

## 검증 방법
(API smoke 재실행 + frontend 정적 검사 통과 기준)

## 적용 여부 판단 기준
(자동 적용 OK 조건과 reject 조건)
"""


def stage_qa_fix_propose(state: CycleState) -> StageResult:
    """Claude proposal for the QA failure. Writes claude_proposal.md
    so the existing qa_fix_apply stage can consume it without a new
    file plumbing surface."""
    label = next(lab for n, lab, _ in STAGES if n == "qa_fix_propose")
    sr = StageResult(name="qa_fix_propose", label=label, status="running")
    t0 = time.time()

    def _skip(reason: str) -> StageResult:
        sr.status = "skipped"
        sr.message = reason
        sr.duration_sec = round(time.time() - t0, 3)
        state.qa_fix_propose_status = "skipped"
        return sr

    if state.qa_status != "failed":
        return _skip("QA 실패 아님 — 수정 제안 불필요")
    if state.publish_blocked:
        return _skip("차단 사유(secret/conflict) 잔존 — QA 수정 제안 미실행")
    if state.qa_fix_attempt >= state.qa_fix_max_attempts:
        return _skip(
            f"QA 수정 재시도 한도 초과 ({state.qa_fix_attempt}/{state.qa_fix_max_attempts})"
        )

    # Hard gates that mirror claude_propose's preconditions.
    if not _factory_flag_enabled("FACTORY_RUN_CLAUDE", default_on=True):
        return _skip("FACTORY_RUN_CLAUDE=false — QA 수정 제안 명시적 비활성")
    if state.risky_files:
        return _skip(
            f"위험 파일 {len(state.risky_files)}건 — QA 수정 제안 스킵"
        )
    claude_bin = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not claude_bin:
        return _skip("claude CLI 미설치 — 스킵")
    if not QA_FEEDBACK_FILE.is_file():
        return _skip("qa_feedback.md 없음 — 제안 입력 부재")

    feedback = QA_FEEDBACK_FILE.read_text(encoding="utf-8")
    prompt = QA_FIX_PROPOSE_PROMPT_TEMPLATE.format(feedback=feedback.strip())
    model = os.environ.get("FACTORY_CLAUDE_MODEL", "sonnet").strip() or "sonnet"
    budget_usd = os.environ.get("FACTORY_CLAUDE_BUDGET_USD", "1.0").strip() or "1.0"
    timeout_sec = float(os.environ.get("FACTORY_CLAUDE_TIMEOUT_SEC", "600"))

    argv = [
        claude_bin,
        "-p", prompt,
        "--allowed-tools", "Read,Glob,Grep",
        "--output-format", "text",
        "--model", model,
        "--max-budget-usd", budget_usd,
    ]
    ok, out = _run(argv, cwd=REPO_ROOT, timeout=timeout_sec)
    sr.duration_sec = round(time.time() - t0, 3)

    if not ok:
        sr.status = "failed"
        sr.message = "claude CLI 실행 실패 (qa_fix_propose)"
        sr.detail = (out or "")[-1500:]
        state.qa_fix_propose_status = "failed"
        return sr

    body = (out or "").strip()
    HEADER = "# Stampport QA Fix 제안"
    idx = body.find(HEADER)
    if idx == -1:
        sr.status = "failed"
        sr.message = "응답에 예상 헤더가 없음 (qa_fix_propose)"
        sr.detail = body[:600]
        state.qa_fix_propose_status = "failed"
        return sr
    body = body[idx:].rstrip()

    # Reuse PROPOSAL_FILE so qa_fix_apply can lean on the same plumbing
    # as claude_apply. Mark proposal status accordingly.
    PROPOSAL_FILE.write_text(body + "\n", encoding="utf-8")
    state.claude_proposal_status = "generated"
    state.claude_proposal_path = str(PROPOSAL_FILE)
    state.claude_proposal_at = utc_now_iso()
    state.claude_proposal_skipped_reason = None
    state.qa_fix_propose_status = "passed"
    sr.status = "passed"
    sr.message = f"QA 수정 제안 생성 ({len(body)} chars, model={model})"
    return sr


def stage_qa_fix_apply(state: CycleState) -> StageResult:
    """Apply the QA fix proposal using the same sandbox as claude_apply.

    Reuses stage_claude_apply's internals — we just override the
    opt-in env check so a single FACTORY_RUN_CLAUDE flag is enough to
    enable the QA loop (FACTORY_APPLY_CLAUDE remains separate for
    plain claude_apply)."""
    label = next(lab for n, lab, _ in STAGES if n == "qa_fix_apply")
    sr = StageResult(name="qa_fix_apply", label=label, status="running")
    t0 = time.time()

    def _skip(reason: str) -> StageResult:
        sr.status = "skipped"
        sr.message = reason
        sr.duration_sec = round(time.time() - t0, 3)
        state.qa_fix_apply_status = "skipped"
        return sr

    if state.qa_status != "failed":
        return _skip("QA 실패 아님 — 수정 적용 불필요")
    if state.qa_fix_propose_status != "passed":
        return _skip("qa_fix_propose가 통과하지 않음 — 적용 스킵")
    if state.publish_blocked:
        return _skip("차단 사유(secret/conflict) 잔존 — QA 수정 적용 미실행")

    # Force-enable apply for this single call by setting the env var
    # in the child env override. We reuse stage_claude_apply but
    # don't want to permanently flip FACTORY_APPLY_CLAUDE in the
    # shell — that would also enable the regular claude_apply path
    # next cycle. The cleanest way: temporarily push the env var,
    # call the existing function, then restore.
    prev_apply = os.environ.get("FACTORY_APPLY_CLAUDE")
    os.environ["FACTORY_APPLY_CLAUDE"] = "true"
    try:
        inner = stage_claude_apply(state)
    finally:
        if prev_apply is None:
            os.environ.pop("FACTORY_APPLY_CLAUDE", None)
        else:
            os.environ["FACTORY_APPLY_CLAUDE"] = prev_apply

    # Mirror inner status but rebrand the stage name/label so the
    # report shows it as "QA Fix 적용" rather than the regular apply.
    sr.status = inner.status
    sr.message = f"[QA Fix] {inner.message}"
    sr.detail = inner.detail
    sr.duration_sec = round(time.time() - t0, 3)

    state.qa_fix_apply_status = (
        "passed" if inner.status == "passed" else
        "failed" if inner.status == "failed" else "skipped"
    )
    return sr


def stage_qa_recheck(state: CycleState) -> StageResult:
    """Re-run the QA gate after a fix attempt. If the recheck passes,
    qa_status flips to passed (and publish_allowed flips to true)."""
    label = next(lab for n, lab, _ in STAGES if n == "qa_recheck")
    sr = StageResult(name="qa_recheck", label=label, status="running")
    t0 = time.time()

    if state.qa_status != "failed":
        sr.status = "skipped"
        sr.message = "직전 QA가 실패 상태가 아님 — 재검사 불필요"
        sr.duration_sec = round(time.time() - t0, 3)
        return sr
    if state.qa_fix_apply_status != "passed":
        sr.status = "skipped"
        sr.message = "QA 수정 적용이 통과하지 않음 — 재검사 스킵"
        sr.duration_sec = round(time.time() - t0, 3)
        return sr

    state.qa_fix_attempt += 1
    inner = stage_qa_gate(state)
    sr.status = inner.status
    sr.message = f"[QA Recheck #{state.qa_fix_attempt}] {inner.message}"
    sr.detail = inner.detail
    sr.duration_sec = round(time.time() - t0, 3)

    # Persist attempt counters so a future cycle knows we've already
    # tried — even if the script is killed between cycles.
    _save_qa_fix_state({
        "attempt": state.qa_fix_attempt,
        "max_attempts": state.qa_fix_max_attempts,
        "last_failed_reason": state.qa_failed_reason,
        "last_feedback_path": state.qa_feedback_path,
    })
    return sr


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------


def _stage_status_line(stages: list[StageResult], name: str) -> str:
    sr = next((s for s in stages if s.name == name), None)
    if sr is None:
        return "(미실행)"
    icon = {
        "passed": "✅ success",
        "failed": "❌ failed",
        "skipped": "⏭️  skipped",
        "running": "⏳ running",
        "pending": "⏸ pending",
    }.get(sr.status, sr.status)
    return f"{icon}  ({sr.duration_sec}s) — {sr.message}".strip()


def _write_report(state: CycleState) -> None:
    failures = [s for s in state.stages if s.status == "failed"]
    summary_lines = [
        "# Stampport Local Factory Report",
        "",
        f"_사이클 #{state.cycle} · {state.status}_",
        "",
        "## 목표",
        state.goal or DEFAULT_GOAL,
        "",
        "## 실행 시간",
        f"- 시작: {state.started_at}",
        f"- 종료: {state.finished_at or '(진행 중)'}",
        f"- 갱신: {state.updated_at}",
        "",
        "## Git 상태",
    ]
    git_sr = next((s for s in state.stages if s.name == "git_check"), None)
    if git_sr and git_sr.detail:
        summary_lines.append("```")
        summary_lines.append(git_sr.detail)
        summary_lines.append("```")
    else:
        summary_lines.append("(git 정보 없음)")

    summary_lines += [
        "",
        "## 위험 파일 검사 (secret 패턴)",
    ]
    if state.risky_files:
        summary_lines.append("⚠️  다음 파일이 secret 패턴과 일치합니다:")
        for f in state.risky_files:
            summary_lines.append(f"- `{f}`")
        summary_lines.append("")
        summary_lines.append("→ 자동 commit/push가 비활성화됩니다.")
    else:
        summary_lines.append("위험 파일 없음.")

    # Release Safety Gate section. Always emitted so the dashboard /
    # human reviewer can see whether the gate passed cleanly, passed
    # with warnings, or actually blocked.
    summary_lines += [
        "",
        "## Release Safety Gate",
        f"- 상태: {state.publish_blocker_status}",
    ]
    if state.publish_blocker_message:
        summary_lines.append(f"- 메시지: {state.publish_blocker_message}")
    if state.warning_reasons:
        summary_lines.append(
            f"- Warning 사유 ({len(state.warning_reasons)}건):"
        )
        for r in state.warning_reasons[:10]:
            summary_lines.append(f"  - {r}")
    if state.auto_resolved_files:
        summary_lines.append(
            f"- 자동 정리 파일 ({len(state.auto_resolved_files)}건):"
        )
        for f in state.auto_resolved_files[:20]:
            summary_lines.append(f"  - `{f}`")
    if state.manual_required_files:
        summary_lines.append(
            f"- Warning 관련 파일 ({len(state.manual_required_files)}건):"
        )
        for f in state.manual_required_files[:20]:
            summary_lines.append(f"  - `{f}`")
    if state.conflict_marker_files:
        summary_lines.append(
            f"- Conflict marker 파일 ({len(state.conflict_marker_files)}건):"
        )
        for f in state.conflict_marker_files[:10]:
            summary_lines.append(f"  - `{f}`")
    if state.publish_blocked:
        summary_lines.append(
            "- 결과: ❌ 차단 — secret/conflict 등 배포 차단 사유가 있습니다."
        )
        summary_lines.append(
            "  이번 사이클은 신규 기능 개발을 수행하지 않았습니다."
        )
    elif state.publish_blocker_status == "warning":
        summary_lines.append(
            "- 결과: ✅ Release Safety Gate: passed with warnings — "
            "build/health 통과로 배포 허용."
        )
    else:
        summary_lines.append("- 결과: ✅ 진행 가능 (build/health/secret 통과 시 배포).")

    summary_lines += [
        "",
        "## 제품 기획 (Product Planner)",
        f"- {_stage_status_line(state.stages, 'product_planning')}",
        f"- 상태: {state.product_planner_status}",
    ]
    if state.product_planner_status in {"generated", "fallback_generated"}:
        if state.product_planner_bottleneck:
            summary_lines.append(
                f"- 가장 큰 병목: {state.product_planner_bottleneck}"
            )
        if state.product_planner_selected_feature:
            summary_lines.append(
                f"- 선정 기능: {state.product_planner_selected_feature}"
            )
        if state.product_planner_solution_pattern:
            summary_lines.append(
                f"- 해결 패턴: {state.product_planner_solution_pattern}"
            )
        if state.product_planner_value_summary:
            summary_lines.append(
                f"- 사용자 가치: {state.product_planner_value_summary}"
            )
        if state.product_planner_llm_needed:
            summary_lines.append(
                f"- LLM 필요 여부: {state.product_planner_llm_needed}"
            )
        if state.product_planner_data_storage_needed:
            summary_lines.append(
                f"- 데이터 저장: {state.product_planner_data_storage_needed}"
            )
        if state.product_planner_external_integration_needed:
            summary_lines.append(
                f"- 외부 연동: {state.product_planner_external_integration_needed}"
            )
        if state.product_planner_frontend_scope:
            summary_lines.append(
                f"- 프론트 범위: {state.product_planner_frontend_scope}"
            )
        if state.product_planner_backend_scope:
            summary_lines.append(
                f"- 백엔드 범위: {state.product_planner_backend_scope}"
            )
        if state.product_planner_success_criteria:
            summary_lines.append(
                f"- 성공 기준: {state.product_planner_success_criteria}"
            )
        if state.product_planner_path:
            summary_lines.append(f"- 리포트: `{state.product_planner_path}`")
        if state.product_planner_at:
            summary_lines.append(f"- 생성 시각: {state.product_planner_at}")
    elif state.product_planner_status == "failed" and state.product_planner_gate_failures:
        summary_lines.append(
            f"- 기획 품질 가드 실패 ({len(state.product_planner_gate_failures)}건):"
        )
        for r in state.product_planner_gate_failures[:5]:
            summary_lines.append(f"  - {r}")
    elif state.product_planner_skipped_reason:
        summary_lines.append(f"- 사유: {state.product_planner_skipped_reason}")

    summary_lines += [
        "",
        "## 빌드 결과",
        f"- app/web: {_stage_status_line(state.stages, 'build_app')}",
        f"- control_tower/web: {_stage_status_line(state.stages, 'build_control')}",
        "",
        "## 문법 검사 결과",
        f"- {_stage_status_line(state.stages, 'syntax_check')}",
        "",
        "## Claude 패치 제안",
        f"- {_stage_status_line(state.stages, 'claude_propose')}",
    ]
    if state.claude_proposal_status == "generated" and state.claude_proposal_path:
        summary_lines.append(f"- 제안 파일: `{state.claude_proposal_path}`")
        if state.claude_proposal_at:
            summary_lines.append(f"- 생성 시각: {state.claude_proposal_at}")
    elif state.claude_proposal_skipped_reason:
        summary_lines.append(f"- 사유: {state.claude_proposal_skipped_reason}")

    summary_lines += [
        "",
        "## QA Gate",
        f"- {_stage_status_line(state.stages, 'qa_gate')}",
        f"- 최종 판정: {state.qa_status}",
        f"- publish 허용: {'예' if state.qa_publish_allowed else '아니오'}",
    ]
    if state.qa_failed_reason:
        summary_lines.append(f"- 실패 원인: {state.qa_failed_reason}")
    summary_lines += [
        f"- Build Artifact: {state.qa_build_artifact}",
        f"- API Health (py_compile + /health): {state.qa_api_health}",
        f"- Screen Presence (Stampport 8 screens): {state.qa_screen_presence}",
        f"- Flow Presence (login/stamp/passport/badges/quests/share): {state.qa_flow_presence}",
        f"- Domain Profile (stampport.json + agent-collaboration.md): {state.qa_domain_profile}",
        f"- QA 수정 시도: {state.qa_fix_attempt}/{state.qa_fix_max_attempts}",
    ]
    if state.qa_report_path:
        summary_lines.append(f"- 리포트: `{state.qa_report_path}`")
    if state.qa_feedback_path:
        summary_lines.append(f"- Feedback: `{state.qa_feedback_path}`")

    summary_lines += [
        "",
        "## Claude 제안 적용",
        f"- {_stage_status_line(state.stages, 'claude_apply')}",
        f"- 상태: {state.claude_apply_status}",
    ]
    if state.claude_apply_status == "applied":
        summary_lines.append(
            f"- 변경 파일 {len(state.claude_apply_changed_files)}개:"
        )
        for p in state.claude_apply_changed_files[:30]:
            summary_lines.append(f"  - `{p}`")
        if state.claude_apply_at:
            summary_lines.append(f"- 적용 시각: {state.claude_apply_at}")
        if state.claude_apply_diff_path:
            summary_lines.append(f"- diff 파일: `{state.claude_apply_diff_path}`")
        summary_lines.append("- 자동 commit/push/deploy: 수행하지 않음 (사람 검토 대기)")
    elif state.claude_apply_status in {"rolled_back", "failed"}:
        summary_lines.append(f"- 메시지: {state.claude_apply_message or '(no detail)'}")
        summary_lines.append(f"- 롤백 수행: {'예' if state.claude_apply_rollback else '아니오'}")
    elif state.claude_apply_skipped_reason:
        summary_lines.append(f"- 사유: {state.claude_apply_skipped_reason}")
    summary_lines.append("")
    syntax_sr = next((s for s in state.stages if s.name == "syntax_check"), None)
    if syntax_sr and syntax_sr.detail:
        summary_lines.append("```")
        summary_lines.append(syntax_sr.detail)
        summary_lines.append("```")
        summary_lines.append("")

    summary_lines += ["## 실패 원인"]
    if failures:
        for f in failures:
            summary_lines.append(f"### {f.label} ({f.name})")
            summary_lines.append(f"- 메시지: {f.message}")
            if f.detail:
                summary_lines.append("```")
                summary_lines.append(f.detail)
                summary_lines.append("```")
    else:
        summary_lines.append("실패 없음.")

    summary_lines += [
        "",
        "## 다음 추천 작업",
    ]
    summary_lines.extend(_recommend_next(state))

    REPORT_FILE.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")


def _recommend_next(state: CycleState) -> list[str]:
    recs: list[str] = []
    failed = [s for s in state.stages if s.status == "failed"]
    if state.publish_blocked and state.manual_required_files:
        recs.append(
            "- 배포 차단 파일이 남아 있습니다. 수동 확인이 필요한 파일:"
        )
        for f in state.manual_required_files[:10]:
            recs.append(f"  - `{f}`")
        recs.append(
            "  → 위 파일을 직접 검토/정리한 뒤 다음 사이클을 시작하세요. 신규 기능 개발은 차단된 상태입니다."
        )
    if state.risky_files:
        recs.append(
            "- 위험 파일 (.env / .pem / .key / .db / .runtime 등)이 git에 떠 있습니다 — `.gitignore`를 점검하세요."
        )
    for s in failed:
        if s.name == "build_app":
            recs.append("- app/web 빌드 실패 — `cd app/web && npm install && npm run build` 수동 확인.")
        elif s.name == "build_control":
            recs.append("- control_tower/web 빌드 실패 — 동일 명령으로 확인.")
        elif s.name == "syntax_check":
            recs.append("- Python/shell 문법 오류 — 위 에러 메시지에서 파일명을 확인하세요.")
        elif s.name == "git_check":
            recs.append("- `git status` 자체가 실패. git 저장소 상태를 확인하세요.")
    if not recs:
        if state.claude_proposal_status == "generated" and state.claude_proposal_path:
            recs.append(
                f"- 이번 사이클은 통과. Claude 제안이 `{state.claude_proposal_path}`에 생성되었습니다 — 사람이 직접 검토 후 적용 여부를 결정하세요."
            )
            recs.append(
                "- 제안이 안전하다면, 다음 단계는 cycle.py에 `claude_apply` stage를 추가해서 정해진 가드(위험 파일 없음 + 빌드/문법 통과 + 제안의 변경 대상 파일 화이트리스트)에서만 patch를 적용하는 것입니다."
            )
        else:
            recs.append(
                "- 이번 사이클은 통과입니다. `FACTORY_RUN_CLAUDE=true`를 켜면 다음 사이클부터 Claude 제안이 생성됩니다."
            )
    return recs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    # Always make sure RUNTIME exists before anything else — even the
    # PAUSE_FILE branch needs it for _log.
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.stderr.write(f"[cycle] could not create RUNTIME dir: {e}\n")
        return 2

    if PAUSE_FILE.exists():
        _log("paused marker present — skipping cycle")
        # Even on a paused tick we want the dashboard to know "the
        # cycle process did wake up". Write a minimal valid state so
        # heartbeat readers never see a missing factory_state.json.
        if not STATE_FILE.exists():
            paused_state = CycleState(cycle=_load_cycle_number())
            paused_state.status = "paused"
            paused_state.current_stage = "paused"
            paused_state.current_task = "factory.paused 마커 — 다음 cycle 대기"
            paused_state.last_message = "paused marker present"
            _write_state(paused_state)
        return 0

    state = CycleState(cycle=_load_cycle_number())
    state.goal = _read_goal()
    state.current_stage = "prepare"
    state.current_task = "사이클 준비"
    state.last_message = "자동 점검 사이클 시작"
    state.started_at = utc_now_iso()
    # Resolve the active run_id and surface it on state. Every artifact
    # this cycle writes inherits the same id (via safe_write_artifact),
    # so freshness consumers (UI / smoke / observer) can compare against
    # autopilot_state.current_run_id without re-deriving.
    state.run_id = _resolve_run_id()
    # Hydrate the rework lock at start so downstream stages see the
    # locked feature even before stage_product_planning runs. If the
    # lock predates this run (different run_id), drop it — stale locks
    # are exactly what produced the cross-run feature drift the kernel
    # contract is designed to prevent.
    _rl_init = _load_active_rework_feature()
    locked_run_id = (_rl_init.get("run_id") or "").strip()
    if _rl_init.get("feature") and locked_run_id and locked_run_id != state.run_id:
        _clear_active_rework_feature()
        _emit_cycle_log(
            state, "active_rework_feature_stale_run",
            "active rework feature lock cleared — stale run_id "
            f"(lock={locked_run_id}, current={state.run_id})",
            lock_run_id=locked_run_id,
            current_run_id=state.run_id,
        )
        _rl_init = {}
    if _rl_init.get("feature"):
        state.active_rework_feature = (_rl_init.get("feature") or "").strip() or None
        state.active_rework_hold_count = int(_rl_init.get("hold_count") or 0)
    _log(f"cycle #{state.cycle} start (goal={state.goal[:40]}…)")
    # Persist the initial state immediately so even an instant crash
    # below leaves the dashboard with a valid factory_state.json.
    _write_state(state)

    # Track per-stage progress contribution.
    weights = {n: w for n, _, w in STAGES}

    # Stages that count as "validation" for the System Log. Emitting
    # validation_started / passed / failed events around these gives
    # the operator a per-stage Build/QA chip without us teaching every
    # stage to log itself.
    VALIDATION_STAGES = {
        "build_app", "build_control", "syntax_check", "qa_gate", "qa_recheck",
    }

    def run_stage(name: str, fn) -> StageResult:
        state.current_stage = name
        state.current_task = next(lab for n, lab, _ in STAGES if n == name)
        state.last_message = f"{state.current_task} 진행 중"
        _write_state(state)
        if name in VALIDATION_STAGES:
            _emit_cycle_log(
                state, "validation_started",
                f"validation started — {state.current_task}",
                stage=name,
            )
        sr = fn()
        state.stages.append(sr)
        # Bump progress by stage weight whether passed/failed/skipped — the
        # cycle moved forward either way.
        state.progress = min(100, state.progress + weights.get(name, 0))
        state.last_message = f"{sr.label}: {sr.message or sr.status}"
        _log(f"stage {name} -> {sr.status} ({sr.duration_sec}s) {sr.message}")
        if name in VALIDATION_STAGES:
            if sr.status == "passed":
                _emit_cycle_log(
                    state, "validation_passed",
                    f"validation passed — {sr.label}",
                    stage=name,
                )
            elif sr.status == "failed":
                _emit_cycle_log(
                    state, "validation_failed",
                    f"validation failed — {sr.label}: {sr.message[:200]}",
                    stage=name, reason=sr.message,
                )
        _write_state(state)
        return sr

    # prepare
    state.current_stage = "prepare"
    state.progress = weights["prepare"]
    state.last_message = "준비 완료"
    _write_state(state)

    run_stage("git_check", lambda: stage_git_check(state))
    run_stage("publish_blocker_check", lambda: stage_publish_blocker_check(state))
    run_stage("publish_blocker_resolve", lambda: stage_publish_blocker_resolve(state))

    # Claude Executor preflight — refuse to enter the planning track at
    # all if the CLI is missing / unauthenticated / hung. Allow operators
    # to disable for diagnostic cycles via FACTORY_CLAUDE_PREFLIGHT=false.
    preflight_enabled = _factory_flag_enabled(
        "FACTORY_CLAUDE_PREFLIGHT", default_on=True,
    )
    if preflight_enabled:
        preflight_sr = run_stage(
            "claude_preflight", lambda: stage_claude_preflight(state),
        )
    else:
        preflight_sr = StageResult(
            name="claude_preflight",
            label=next(lab for n, lab, _ in STAGES if n == "claude_preflight"),
            status="skipped",
            message="FACTORY_CLAUDE_PREFLIGHT=false — preflight 명시적 비활성",
        )
        state.stages.append(preflight_sr)

    # Short-circuit: if preflight failed, every downstream Claude stage
    # is unsafe (planner / designer / spec / propose / apply all spawn
    # claude). Mark them skipped with a precise reason and route the
    # cycle to report so the operator gets a normal summary instead of
    # a half-finished log. The publish-blocker chain stays untouched —
    # it does not call claude.
    if preflight_enabled and preflight_sr.status == "failed":
        skip_reason = (
            f"claude_preflight failed ({state.claude_executor_failure_code}) — "
            "downstream Claude stages skipped"
        )
        for skip_name in (
            "runtime_artifact_sweep",
            "product_planning", "designer_critique", "planner_revision",
            "designer_final_review", "design_spec", "pm_decision",
            "build_app", "build_control", "syntax_check",
            "claude_propose", "implementation_ticket", "claude_apply",
            "qa_gate", "qa_feedback", "qa_fix_propose", "qa_fix_apply",
            "qa_recheck",
        ):
            sr_skip = StageResult(
                name=skip_name,
                label=next(lab for n, lab, _ in STAGES if n == skip_name),
                status="skipped",
                message=skip_reason,
            )
            state.stages.append(sr_skip)
        # Cycle is FAILED, not "planning_only" — autopilot retry policy
        # depends on failed_stage being claude_preflight to take the
        # apply-only retry path on the NEXT cycle (after operator fixes
        # the CLI).
        state.status = "failed"
        state.failed_stage = state.failed_stage or "claude_preflight"
        state.failed_reason = (
            state.failed_reason
            or f"claude_preflight failed: {state.claude_executor_failure_code}"
        )
        state.last_message = (
            f"Claude CLI preflight 실패 — {state.claude_executor_failure_code}"
        )
        state.finished_at = utc_now_iso()
        state.current_stage = "report"
        state.progress = 100
        _write_state(state)
        return 1

    # Runtime artifact sweep — runs ONCE per cycle right after Claude
    # CLI preflight succeeds, BEFORE the apply-only retry hydrator
    # (which would otherwise pick up a stale-run implementation_ticket
    # from disk) and BEFORE product_planning. The sweep moves any
    # cross-run .runtime/ artifact into .runtime/stale_artifacts/ so
    # later stages — design_spec, implementation_ticket,
    # claude_propose — cannot read a previous cycle's output as
    # current. Same-run artifacts are kept so the apply-only retry
    # path keeps working.
    run_stage(
        "runtime_artifact_sweep",
        lambda: stage_runtime_artifact_sweep(state),
    )

    # Apply-only retry path. Triggered by autopilot when the previous
    # cycle's claude_apply hit a retryable CLI failure but the planner /
    # design_spec / implementation_ticket / claude_proposal artifacts
    # are intact. We hydrate state from the prior factory_state.json and
    # mark every planner/design stage as skipped so the retry doesn't
    # burn another 20+ minutes rebuilding the same plan. The remainder
    # of main() — build_app / build_control / syntax_check / claude_apply /
    # qa_gate / finalization — runs unchanged.
    apply_retry_only_active = False
    if _factory_flag_enabled("FACTORY_APPLY_RETRY_ONLY", default_on=False):
        prior = {}
        try:
            if STATE_FILE.is_file():
                prior = json.loads(STATE_FILE.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            prior = {}
        eligible = (
            (prior.get("implementation_ticket_status") == "generated")
            and (prior.get("claude_proposal_status") == "generated")
            and IMPLEMENTATION_TICKET_FILE.is_file()
            and PROPOSAL_FILE.is_file()
        )
        if eligible:
            apply_retry_only_active = True
            state.implementation_ticket_status = "generated"
            state.implementation_ticket_path = prior.get(
                "implementation_ticket_path"
            ) or str(IMPLEMENTATION_TICKET_FILE)
            state.implementation_ticket_target_files = list(
                prior.get("implementation_ticket_target_files") or []
            )
            state.implementation_ticket_target_screens = list(
                prior.get("implementation_ticket_target_screens") or []
            )
            state.implementation_ticket_selected_feature = prior.get(
                "implementation_ticket_selected_feature"
            )
            state.implementation_ticket_feature_id = prior.get(
                "implementation_ticket_feature_id"
            )
            state.implementation_ticket_source = prior.get(
                "implementation_ticket_source"
            )
            state.claude_proposal_status = "generated"
            state.claude_proposal_path = prior.get(
                "claude_proposal_path"
            ) or str(PROPOSAL_FILE)
            state.claude_proposal_at = prior.get("claude_proposal_at")
            state.design_spec_status = (
                prior.get("design_spec_status") or "skipped"
            )
            state.design_spec_acceptance_passed = bool(
                prior.get("design_spec_acceptance_passed")
            )
            state.design_spec_feature = prior.get("design_spec_feature")
            state.design_spec_feature_id = prior.get("design_spec_feature_id")
            state.design_spec_target_files = list(
                prior.get("design_spec_target_files") or []
            )
            state.product_planner_status = (
                prior.get("product_planner_status") or "skipped"
            )
            state.product_planner_selected_feature = prior.get(
                "product_planner_selected_feature"
            )
            state.selected_feature = prior.get("selected_feature")
            state.selected_feature_id = prior.get("selected_feature_id")
            state.selected_feature_source = prior.get("selected_feature_source")
            # Rehydrate the canonical source-of-truth so the apply-only
            # retry path can run validate_source_of_truth_contract
            # against the prior cycle's locked feature_id without
            # re-running planner_revision.
            state.source_of_truth_feature = prior.get(
                "source_of_truth_feature"
            )
            state.source_of_truth_feature_id = prior.get(
                "source_of_truth_feature_id"
            )
            state.source_of_truth_stage = prior.get(
                "source_of_truth_stage"
            )
            state.source_of_truth_locked_at = prior.get(
                "source_of_truth_locked_at"
            )
            state.source_of_truth_contract_status = prior.get(
                "source_of_truth_contract_status"
            )
            state.source_of_truth_contract_reason = prior.get(
                "source_of_truth_contract_reason"
            )
            state.claude_proposal_feature_id = prior.get(
                "claude_proposal_feature_id"
            )
            try:
                state.claude_executor_retry_count = int(
                    prior.get("claude_executor_retry_count") or 0
                ) + 1
            except (TypeError, ValueError):
                state.claude_executor_retry_count = 1

            for skip_name in (
                "product_planning", "designer_critique", "planner_revision",
                "designer_final_review", "design_spec", "pm_decision",
                "claude_propose", "implementation_ticket",
            ):
                sr_skip = StageResult(
                    name=skip_name,
                    label=next(lab for n, lab, _ in STAGES if n == skip_name),
                    status="skipped",
                    message="apply_retry_only — 기존 산출물 재사용",
                )
                state.stages.append(sr_skip)
            _emit_cycle_log(
                state, "apply_retry_only_active",
                "apply_retry_only — planning 단계 모두 skip, claude_apply 재시도 경로",
                retry_count=state.claude_executor_retry_count,
            )

    if not apply_retry_only_active:
        run_stage("product_planning", lambda: stage_product_planning(state))
        # Planner ↔ Designer ping-pong. Each stage no-ops (skipped) when
        # FACTORY_PLANNER_DESIGNER_PINGPONG is unset, so existing flows
        # are unaffected. When enabled, the four stages produce the
        # designer_critique / planner_revision / designer_final_review /
        # pm_decision artifacts and populate the desire scorecard.
        run_stage("designer_critique",     lambda: stage_designer_critique(state))
        run_stage("planner_revision",      lambda: stage_planner_revision(state))
        run_stage("designer_final_review", lambda: stage_designer_final_review(state))
        run_stage("design_spec",           lambda: stage_design_spec(state))
        run_stage("pm_decision",           lambda: stage_pm_decision(state))
    run_stage(
        "build_app",
        lambda: stage_web_build(state, web_dir=REPO_ROOT / "app" / "web", name="build_app"),
    )
    run_stage(
        "build_control",
        lambda: stage_web_build(
            state, web_dir=REPO_ROOT / "control_tower" / "web", name="build_control"
        ),
    )
    run_stage("syntax_check", lambda: stage_syntax_check(state))
    if not apply_retry_only_active:
        run_stage("claude_propose", lambda: stage_claude_propose(state))
        # Implementation Ticket — composed deterministically from PM 결정 +
        # planner revision + claude proposal. claude_apply gates on this:
        # missing ticket means the cycle stays planning_only.
        run_stage(
            "implementation_ticket",
            lambda: stage_implementation_ticket(state),
        )
    run_stage("claude_apply", lambda: stage_claude_apply(state))

    # QA Gate — final verification that what we built is shippable.
    # Runs after any code change, and gates publish_changes via
    # qa_publish_allowed.
    state.qa_fix_max_attempts = int(
        os.environ.get("FACTORY_QA_FIX_MAX_ATTEMPTS", "2") or "2"
    )
    # Restore prior attempt count from disk so the cap is honored
    # across cycles (not just within this single run).
    prior = _read_qa_fix_state()
    if isinstance(prior.get("attempt"), int):
        state.qa_fix_attempt = prior["attempt"]

    qa_result = run_stage("qa_gate", lambda: stage_qa_gate(state))

    # Persist post-gate state immediately so even if the fix loop
    # bails, the next cycle sees the correct counters.
    _save_qa_fix_state({
        "attempt": state.qa_fix_attempt,
        "max_attempts": state.qa_fix_max_attempts,
        "last_failed_reason": state.qa_failed_reason,
        "last_feedback_path": state.qa_feedback_path,
    })

    if qa_result.status == "failed" and state.qa_fix_attempt < state.qa_fix_max_attempts:
        run_stage("qa_feedback", lambda: stage_qa_feedback(state))
        run_stage("qa_fix_propose", lambda: stage_qa_fix_propose(state))
        run_stage("qa_fix_apply", lambda: stage_qa_fix_apply(state))
        run_stage("qa_recheck", lambda: stage_qa_recheck(state))
    elif qa_result.status == "failed":
        # Hit the cap — log a skipped row for each downstream stage so
        # the report shows what was elided and why.
        for n in ("qa_feedback", "qa_fix_propose", "qa_fix_apply", "qa_recheck"):
            sr_skipped = StageResult(
                name=n,
                label=next(lab for k, lab, _ in STAGES if k == n),
                status="skipped",
                message=f"QA 수정 재시도 한도 초과 ({state.qa_fix_attempt}/{state.qa_fix_max_attempts})",
            )
            state.stages.append(sr_skipped)
        state.last_message = "QA 수정 재시도 한도를 초과했습니다."

    # Decide overall status BEFORE writing the report so the report
    # header reflects the final outcome (succeeded/failed/no_code_change/
    # planning_only), not "running". If a qa_recheck recovered after an
    # initial qa_gate failure, filter the original qa_gate failure out
    # of the failure list — otherwise we'd show "cycle failed" even
    # though the recheck passed and publish is allowed.
    if state.qa_status == "passed":
        failed = [
            s for s in state.stages
            if s.status == "failed" and s.name != "qa_gate"
        ]
    else:
        failed = [s for s in state.stages if s.status == "failed"]

    apply_changed = list(state.claude_apply_changed_files or [])
    apply_status = state.claude_apply_status

    if state.publish_blocked:
        # Publish blocker takes priority over all other failure reasons:
        # the user needs to clear the blocker before any other diagnosis
        # is even useful. We use 'failed' for the JSON status (the
        # heartbeat machinery only knows succeeded/failed/running) but
        # set last_message to the explicit blocker copy so the
        # dashboard surfaces the "신규 개발 중단" reason.
        state.status = "failed"
        state.last_message = (
            "차단 사유(secret/conflict)가 남아 있어 신규 개발을 중단했습니다."
        )
        state.failed_stage = "publish_blocker_resolve"
        state.failed_reason = (
            state.publish_blocker_message
            or "Release Safety Gate 차단 — secret 또는 conflict marker 잔존"
        )
        state.suggested_action = (
            "blocker_resolve_report.md 확인 후 hard_risky / conflict marker 파일을 직접 처리"
        )
        state.code_changed = False
        state.no_code_change_reason = "publish_blocker_active"
        _emit_cycle_log(
            state, "cycle_failed",
            f"cycle #{state.cycle} failed: publish blocker active",
            stage=state.failed_stage, reason=state.failed_reason,
        )
    elif failed:
        first = failed[0]
        state.status = "failed"
        state.last_message = (
            "자동 점검 사이클 실패: "
            + ", ".join(s.label for s in failed)
        )
        state.failed_stage = first.name
        state.failed_reason = first.message or "원인 메시지 없음"
        state.suggested_action = _suggest_action_for_stage(first.name)
        state.code_changed = False
        state.no_code_change_reason = f"stage_failed:{first.name}"
        _emit_cycle_log(
            state, "cycle_failed",
            f"cycle #{state.cycle} failed at {first.name}: {first.message}",
            stage=first.name, reason=first.message or "",
        )
    elif apply_status == "applied" and apply_changed:
        cats = _categorize_changed_files(apply_changed)
        state.frontend_changed = bool(cats["frontend"])
        state.backend_changed = bool(cats["backend"])
        state.control_tower_changed = bool(cats["control_tower"])
        state.docs_only = bool(cats["docs_only"])
        # Real code change shipped — clear the active rework feature
        # lock so the next cycle is free to ideate again. We clear here
        # rather than at git_push because cycle.py never pushes; the
        # apply event itself is the success boundary.
        if not cats["docs_only"]:
            cleared = _clear_active_rework_feature()
            state.active_rework_feature = None
            state.active_rework_hold_count = 0
            if cleared:
                _emit_cycle_log(
                    state, "active_rework_feature_cleared",
                    "active rework feature lock cleared — code change applied",
                    changed_files_count=len(apply_changed),
                )
        if cats["docs_only"]:
            # Files changed, but none of them are product code — treat
            # this as docs_only so the dashboard doesn't claim a
            # successful feature ship.
            state.status = "docs_only"
            state.code_changed = False
            state.no_code_change_reason = "docs_only"
            state.last_message = (
                f"이번 사이클 변경 {len(apply_changed)}개 — 모두 docs/config "
                "이라 사용자 영향 없음"
            )
            _emit_cycle_log(
                state, "cycle_produced_docs_only",
                f"cycle produced docs only: {len(apply_changed)}개 파일",
                files=apply_changed[:30],
            )
        else:
            state.status = "succeeded"
            state.code_changed = True
            state.last_message = (
                f"이번 사이클 코드 변경 {len(apply_changed)}개 — 검증 통과"
            )
            _emit_cycle_log(
                state, "cycle_produced_code_change",
                f"cycle produced code change: {len(apply_changed)}개 파일",
                files=apply_changed[:30],
                frontend_changed=state.frontend_changed,
                backend_changed=state.backend_changed,
                control_tower_changed=state.control_tower_changed,
            )
            if state.frontend_changed:
                _emit_cycle_log(
                    state, "frontend_files_changed",
                    f"frontend files changed — {sum(1 for f in apply_changed if f.startswith('app/web/src/'))}개",
                    files=[f for f in apply_changed if f.startswith("app/web/src/")][:20],
                )
            if state.backend_changed:
                _emit_cycle_log(
                    state, "backend_files_changed",
                    f"backend files changed — {sum(1 for f in apply_changed if f.startswith('app/api/'))}개",
                    files=[f for f in apply_changed if f.startswith("app/api/")][:20],
                )
            if state.control_tower_changed:
                ct_count = sum(
                    1 for f in apply_changed
                    if any(f.startswith(p) for p in CONTROL_TOWER_PATH_PREFIXES)
                )
                _emit_cycle_log(
                    state, "control_tower_files_changed",
                    f"control_tower files changed — {ct_count}개",
                    files=[
                        f for f in apply_changed
                        if any(f.startswith(p) for p in CONTROL_TOWER_PATH_PREFIXES)
                    ][:20],
                )
    else:
        # No failed stages, but no actual code change either. Distinguish:
        #   hold_for_rework   — PM verdict was hold (재작업)
        #   planning_only     — planner/designer/pm artifacts were generated,
        #                       but development phase didn't run for some
        #                       other reason (e.g. apply skipped, no diff)
        #   no_code_change    — everything skipped
        planner_generated = (
            state.product_planner_status in {"generated", "fallback_generated"}
            or state.designer_critique_status == "generated"
            or state.planner_revision_status == "generated"
            or state.designer_final_review_status == "generated"
            or state.pm_decision_status == "generated"
        )
        reason = (
            state.claude_apply_skipped_reason
            or state.claude_apply_message
            or apply_status
            or "claude_apply 미실행"
        )
        state.code_changed = False
        pm_hold = (
            state.pm_decision_status == "generated"
            and not state.pm_decision_ship_ready
        )
        if pm_hold:
            state.status = "hold_for_rework"
            state.no_code_change_reason = (
                f"hold_for_rework:{state.pm_decision_message or 'PM HOLD'}"
            )
            state.last_message = (
                "PM HOLD — 이번 사이클은 재작업 사이클입니다 "
                f"(pm_decision={state.pm_decision_message or 'HOLD'})."
            )
            state.suggested_action = (
                "기획자/디자이너 단계의 rework 항목을 반영해 다음 사이클을 진행하세요."
            )
            # Active rework feature lock — save the canonical feature so
            # the next cycle's planner cannot drift to a brand-new
            # candidate. This is the file that breaks the HOLD loop:
            # without it, every HOLD cycle proposes 3 new ideas, the
            # design_spec is always stale, and implementation never runs.
            if not state.pm_hold_type:
                ht_final, hr_final = _classify_pm_hold_type(state)
                state.pm_hold_type = ht_final
                state.pm_hold_type_reason = hr_final
            canonical_feature = (
                state.planner_revision_selected_feature
                or state.product_planner_selected_feature
                or state.selected_feature
                or state.active_rework_feature
                or ""
            )
            new_hold_count = int(state.active_rework_hold_count or 0) + 1
            _save_active_rework_feature(
                feature=canonical_feature,
                hold_count=new_hold_count,
                hold_type=state.pm_hold_type,
                pm_message=state.pm_decision_message,
            )
            state.active_rework_feature = (canonical_feature or "").strip() or None
            state.active_rework_hold_count = new_hold_count
            _emit_cycle_log(
                state, "cycle_hold_for_rework",
                "cycle hold_for_rework — PM 결정 HOLD (재작업)",
                pm_decision=state.pm_decision_message or "HOLD",
                hold_type=state.pm_hold_type,
                hold_reason=state.pm_hold_type_reason,
                active_rework_feature=state.active_rework_feature,
                active_rework_hold_count=state.active_rework_hold_count,
            )
            # Skip the planning_only / no_code_change branches below.
            planner_generated = False  # short-circuit
        state.no_code_change_reason = state.no_code_change_reason or (
            f"planning_only:{reason}" if planner_generated
            else f"no_code_change:{reason}"
        )
        if pm_hold:
            pass  # handled above
        elif planner_generated:
            state.status = "planning_only"
            state.last_message = (
                "기획/디자인 산출물만 생성됨 — 코드 변경 없음 ("
                f"claude_apply={apply_status}, 사유={reason})"
            )
            _emit_cycle_log(
                state, "cycle_planning_only",
                f"cycle planning only: {reason}",
                claude_apply_status=apply_status,
            )
        else:
            state.status = "no_code_change"
            state.last_message = (
                "이번 사이클은 코드 변경 없음 ("
                f"claude_apply={apply_status}, 사유={reason})"
            )
            _emit_cycle_log(
                state, "cycle_produced_no_code_change",
                f"cycle produced no code change: {reason}",
                claude_apply_status=apply_status,
            )
        if not pm_hold:
            state.suggested_action = (
                "FACTORY_APPLY_CLAUDE=true 로 켠 뒤 다시 실행하거나, "
                "operator_request 로 수동 변경 지시를 내리세요."
            )

    # Agent Supervisor gate — last chance to refuse a "succeeded" verdict
    # when the agents produced artifacts but no real code change. Lives
    # in agent_supervisor.py (stdlib-only, no import on runner.py).
    #
    # We persist factory_state.json BEFORE running the supervisor so it
    # has the latest claude_apply_changed_files / qa_status / etc. The
    # supervisor reads that file directly.
    _write_state(state)
    try:
        from . import agent_supervisor as _supervisor
        sup_report = _supervisor.run_supervisor()
    except Exception as e:  # noqa: BLE001
        _log(f"agent_supervisor failed: {e}")
        sup_report = None

    if sup_report:
        sup_overall = sup_report.get("overall_status")
        sup_blocking = sup_report.get("blocking_agent")
        sup_meaningful = bool(sup_report.get("meaningful_change"))
        sup_ticket_ok = bool(sup_report.get("implementation_ticket_exists"))

        # Refuse to call this cycle "succeeded" when the supervisor
        # didn't pass — but ONLY when the supervisor's verdict
        # actually represents a quality problem.
        #
        # Critical exclusion: `ready_to_publish` is NOT a failure. It
        # means "code shipped + QA passed, just waiting for the next
        # publish_changes / deploy_to_server command". Treating that
        # like a planning_only downgrade is the exact bug that turned
        # cycle #2 from succeeded into planning_only despite 3 changed
        # files + qa passed.
        sup_apply_ok = (
            state.claude_apply_status == "applied"
            and len(state.claude_apply_changed_files or []) > 0
        )
        sup_qa_ok = state.qa_status == "passed"
        is_ready_to_publish = (
            sup_overall == "ready_to_publish"
            or (sup_meaningful and sup_ticket_ok and sup_apply_ok and sup_qa_ok
                and sup_blocking == "deploy")
        )

        if is_ready_to_publish:
            # Keep status="succeeded" — the cycle did its job. Just
            # surface that publish/commit/push hasn't run yet so the
            # operator (or deploy_to_server) can pick up from here.
            state.status = "succeeded"
            state.code_changed = True
            state.no_code_change_reason = None
            state.last_message = (
                "사이클 succeeded — commit/push (publish_changes / "
                "deploy_to_server) 명령 대기 중"
            )
            state.suggested_action = (
                "deploy_to_server 또는 publish_changes 명령으로 commit/push 진행"
            )
            _emit_cycle_log(
                state, "supervisor_ready_to_publish",
                "supervisor: code shipped + qa passed — publish/commit/push required",
                blocking_agent=sup_blocking,
                changed_files_count=len(state.claude_apply_changed_files or []),
                qa_status=state.qa_status,
            )
        elif state.status == "succeeded" and sup_overall != "pass":
            prior_status = state.status
            if not sup_meaningful or not sup_ticket_ok:
                state.status = "planning_only"
                state.code_changed = False
            else:
                # meaningful + ticket but agent quality lacking — force
                # planning_only label so completed isn't claimed.
                state.status = "planning_only"
            state.no_code_change_reason = (
                f"supervisor:{sup_overall} blocking={sup_blocking or '—'}"
            )
            state.last_message = (
                f"Agent Supervisor가 succeeded 판정을 거부했습니다 "
                f"(overall={sup_overall}, blocking={sup_blocking or '—'}). "
                f"prior={prior_status}"
            )
            state.suggested_action = (
                sup_report.get("next_action")
                or "Agent Supervisor 의 retry_prompt 에 따라 해당 에이전트 재실행"
            )
            _emit_cycle_log(
                state, "supervisor_rejected",
                f"supervisor rejected succeeded → planning_only: "
                f"overall={sup_overall} blocking={sup_blocking}",
                blocking_agent=sup_blocking,
                overall_status=sup_overall,
            )

        # Always emit a cycle_log marker so the System Log shows that
        # the supervisor ran for this cycle.
        _emit_cycle_log(
            state, "supervisor_review_completed",
            f"Agent Supervisor review completed — overall={sup_overall} "
            f"meaningful={sup_meaningful} ticket={sup_ticket_ok}",
            overall_status=sup_overall,
            blocking_agent=sup_blocking,
            meaningful_change=sup_meaningful,
        )

    # Active rework feature lock — extended clear conditions. Beyond
    # the apply-success path that already cleared at line ~8121, ANY
    # terminal status that isn't HOLD or hard failure should also
    # clear the lock. The smoke verdict for these states resolves to
    # READY_TO_REVIEW / READY_TO_PUBLISH (succeeded path) or PASS
    # (planning_only / no_code_change / docs_only) — none of those
    # represent an active rework loop, so a stale lock would only
    # prevent the next cycle from picking a fresh candidate. Without
    # this, the lock outlives its useful lifetime whenever a rework
    # cycle drops to planning_only instead of either shipping code or
    # explicitly HOLDing again.
    NON_REWORK_TERMINAL_STATES = {
        "succeeded", "planning_only", "no_code_change", "docs_only",
    }
    if state.status in NON_REWORK_TERMINAL_STATES and (
        state.active_rework_feature
        or ACTIVE_REWORK_FEATURE_FILE.is_file()
    ):
        if _clear_active_rework_feature():
            _emit_cycle_log(
                state, "active_rework_feature_cleared",
                f"active rework feature lock cleared — terminal status="
                f"{state.status}",
                terminal_status=state.status,
            )
        state.active_rework_feature = None
        state.active_rework_hold_count = 0

    # Unattended e2e closure — write the auto-publish marker when the
    # cycle finished with real shipped code + qa passed + a ticket. The
    # runner's main loop (not cycle.py) owns the actual git commit/push
    # so this stays a marker file rather than an inline subprocess —
    # that keeps the import graph acyclic and lets the operator's
    # LOCAL_RUNNER_ALLOW_PUBLISH / LOCAL_RUNNER_PUBLISH_DRY_RUN env
    # gates remain authoritative.
    if (
        state.status == "succeeded"
        and state.claude_apply_status == "applied"
        and len(state.claude_apply_changed_files or []) > 0
        and state.qa_status == "passed"
        and state.implementation_ticket_status == "generated"
    ):
        selected_feature = (
            state.implementation_ticket_selected_feature
            or state.product_planner_selected_feature
            or "Stampport cycle"
        )
        commit_subject = f"Factory cycle #{state.cycle}: {selected_feature}"[:72]
        marker = {
            "schema_version": 1,
            "cycle_id": state.cycle,
            "requested_at": utc_now_iso(),
            "consumed": False,
            "consumed_at": None,
            "consume_attempts": 0,
            "selected_feature": selected_feature,
            "changed_files": list(state.claude_apply_changed_files or [])[:30],
            "qa_status": state.qa_status,
            "implementation_ticket_path": state.implementation_ticket_path,
            "commit_subject": commit_subject,
            "commit_body": (
                f"Cycle #{state.cycle} 자동 공장 결과:\n"
                f"- 선정 기능: {selected_feature}\n"
                f"- 변경 파일 수: {len(state.claude_apply_changed_files or [])}\n"
                f"- QA: {state.qa_status}\n"
                f"\n자동 commit by control_tower runner — supervisor=ready_to_publish"
            ),
        }
        marker_path = RUNTIME / "auto_publish_request.json"
        if safe_write_json(marker_path, marker):
            _emit_cycle_log(
                state, "auto_publish_marker_written",
                f"auto-publish marker requested — runner picks up next tick "
                f"({len(marker['changed_files'])}개 파일, "
                f"feature={selected_feature[:40]})",
                cycle_id=state.cycle,
                changed_files_count=len(marker["changed_files"]),
            )

    state.current_stage = "report"
    state.current_task = "리포트 작성"
    state.progress = 100
    state.finished_at = utc_now_iso()
    _write_state(state)
    _write_report(state)

    state.current_stage = "waiting"
    state.current_task = "다음 사이클 대기 중"
    _write_state(state)
    _log(
        f"cycle #{state.cycle} {state.status} — "
        f"failed_stages={[s.name for s in failed]} · "
        f"code_changed={state.code_changed} · "
        f"changed={len(state.claude_apply_changed_files or [])}건"
    )
    # Exit 0 covers any non-failed terminal state — succeeded /
    # planning_only / no_code_change / hold_for_rework / docs_only all
    # leave the working tree clean. The bash factory loop reads the
    # exit code as "did the cycle crash?", not "did it ship code", so
    # a clean planning_only / hold_for_rework run should not look like
    # a crash.
    CLEAN_TERMINAL_STATES = {
        "succeeded", "planning_only", "no_code_change",
        "hold_for_rework", "docs_only",
    }
    return 0 if state.status in CLEAN_TERMINAL_STATES else 1


def _safe_main() -> int:
    """Wrapper around main() that guarantees an on-disk factory_state.json
    even when main() raises. Without this, a crash early in the cycle
    would leave the dashboard with no state file and every consumer
    falling back to "no cycle ran" — exactly the symptom we're fixing.
    """
    try:
        return main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        # Persist a minimal failed state so the dashboard sees the
        # crash instead of an empty file. We deliberately do not
        # use _write_state because CycleState construction itself
        # might be the failure source.
        try:
            RUNTIME.mkdir(parents=True, exist_ok=True)
            crash_payload = {
                "cycle": _load_cycle_number(),
                "status": "failed",
                "current_stage": "main",
                "current_task": "cycle main() crashed",
                "last_message": f"cycle main raised: {e}",
                "started_at": utc_now_iso(),
                "finished_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
                "failed_stage": "cycle_bootstrap",
                "failed_reason": str(e)[:600],
                "suggested_action": (
                    "local_factory.log 의 traceback 확인 후 cycle.py 코드 점검"
                ),
            }
            safe_write_json(STATE_FILE, crash_payload)
            _log(f"cycle main crashed: {e}")
        except Exception as e2:  # noqa: BLE001
            sys.stderr.write(
                f"[cycle] failed to persist crash state: {e2}\n"
            )
        return 1


if __name__ == "__main__":
    sys.exit(_safe_main())
