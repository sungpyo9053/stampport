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
# Implementation Ticket — single source of truth for "what code does
# this cycle actually intend to write?". Lives between PM 결정 and
# claude_apply. claude_apply refuses to run if the ticket is missing
# or has no concrete target files. See stage_implementation_ticket.
IMPLEMENTATION_TICKET_FILE  = RUNTIME / "implementation_ticket.md"
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
    # Per-tier file-change flags — set in main() after claude_apply by
    # _categorize_changed_files. The dashboard shows "FE 변경 / BE 변경 /
    # 관제실 변경" badges off these so the operator can tell at a glance
    # whether the cycle hit the user-facing surface.
    frontend_changed: bool = False
    backend_changed: bool = False
    control_tower_changed: bool = False
    docs_only: bool = False

    def to_dict(self) -> dict:
        return {
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
            "desire_scorecard": dict(self.desire_scorecard),
            "desire_scorecard_total": self.desire_scorecard_total,
            "desire_scorecard_path": self.desire_scorecard_path,
            "desire_scorecard_ship_ready": self.desire_scorecard_ship_ready,
            "desire_scorecard_rework": list(self.desire_scorecard_rework),
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
            "frontend_changed": self.frontend_changed,
            "backend_changed": self.backend_changed,
            "control_tower_changed": self.control_tower_changed,
            "docs_only": self.docs_only,
        }


def _load_cycle_number() -> int:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return int(data.get("cycle", 0)) + 1
        except (json.JSONDecodeError, ValueError, OSError):
            return 1
    return 1


def _write_state(state: CycleState) -> None:
    state.updated_at = utc_now_iso()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _log(line: str) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{utc_now_iso()}] {line}\n")


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

## 신규 장치 아이디어 후보

| 장치 | 자극하는 욕구(2개 이상) | 사용자 가치 | 구현 난이도 | 제품 임팩트 | 리스크 |
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

## 이번 사이클 선정 장치
선정한 장치명 한 줄. Stampport 톤의 고유 이름.

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


def _build_product_planner_prompt(goal: str) -> str:
    profile = _load_stampport_profile_text()
    collab = _load_agent_collab_text()
    return PRODUCT_PLANNER_PROMPT_TEMPLATE.format(
        goal=goal.strip() or DEFAULT_GOAL,
        domain_profile=profile or "(stampport.json 미존재)",
        collab_doc=collab or "(agent-collaboration.md 미존재)",
    )


def _extract_md_section(md: str, heading: str) -> str:
    """Return the body under '## heading' until the next ## or end-of-doc."""
    pat = (
        r"^##\s+" + re.escape(heading)
        + r"\s*\n(.*?)(?=\n##\s|\Z)"
    )
    m = re.search(pat, md, re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


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
    return max(table_rows, list_items)


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

    enabled = os.environ.get("FACTORY_PRODUCT_PLANNER_MODE", "").strip().lower()
    if enabled not in {"true", "1", "yes", "on"}:
        return _skip("FACTORY_PRODUCT_PLANNER_MODE 미설정 — 기본 OFF (스킵)")

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
        sr.status = "failed"
        sr.message = "claude CLI 실행 실패"
        sr.detail = (out or "")[-1500:]
        state.product_planner_status = "failed"
        state.product_planner_message = sr.message
        return sr

    body = (out or "").strip()
    if not body:
        sr.status = "failed"
        sr.message = "claude 응답이 비어있음"
        state.product_planner_status = "failed"
        state.product_planner_message = sr.message
        return sr

    HEADER = "# Stampport Product Planner Report"
    idx = body.find(HEADER)
    if idx == -1:
        sr.status = "failed"
        sr.message = "응답에 예상 헤더가 없음"
        sr.detail = body[:600]
        state.product_planner_status = "failed"
        state.product_planner_message = sr.message
        return sr
    body = body[idx:].rstrip()

    # Quality gate — refuse to advance with a half-baked plan, since
    # claude_propose downstream blindly consumes whatever lives in the
    # report file.
    gate_failures = _validate_planner_report(body)
    if gate_failures:
        sr.status = "failed"
        sr.message = (
            f"기획 품질 가드 실패 ({len(gate_failures)}건): "
            + "; ".join(gate_failures[:3])
        )
        sr.detail = "\n".join(f"- {r}" for r in gate_failures)
        state.product_planner_status = "failed"
        state.product_planner_gate_failures = gate_failures
        state.product_planner_message = sr.message
        # Persist the report anyway so the user can inspect what claude
        # produced and improve the prompt.
        try:
            (PRODUCT_PLANNER_FILE.with_suffix(".rejected.md")).write_text(
                body + "\n", encoding="utf-8",
            )
        except OSError:
            pass
        return sr

    PRODUCT_PLANNER_FILE.write_text(body + "\n", encoding="utf-8")
    # Ping-pong protocol uses planner_proposal.md as the canonical
    # name for the planner's "원안". Keep the legacy
    # product_planner_report.md write so existing readers (claude_propose,
    # heartbeat metadata) keep working, and mirror the same content
    # under the new name so the dashboard / designer stages can read
    # a stable file path.
    try:
        PLANNER_PROPOSAL_FILE.write_text(body + "\n", encoding="utf-8")
    except OSError:
        pass

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

    state.product_planner_status = "generated"
    state.product_planner_path = str(PRODUCT_PLANNER_FILE)
    state.product_planner_at = utc_now_iso()
    state.product_planner_bottleneck = bottleneck or None
    state.product_planner_selected_feature = selected or None
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


def _pingpong_enabled() -> bool:
    """Honor the same true/1/yes/on convention the rest of cycle.py uses."""
    return os.environ.get(PINGPONG_ENV_FLAG, "").strip().lower() in {
        "true", "1", "yes", "on",
    }


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
    if state.product_planner_status != "generated":
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

    DESIGNER_CRITIQUE_FILE.write_text(body + "\n", encoding="utf-8")
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

    PLANNER_REVISION_FILE.write_text(body + "\n", encoding="utf-8")
    state.planner_revision_status = "generated"
    state.planner_revision_path = str(PLANNER_REVISION_FILE)
    state.planner_revision_at = utc_now_iso()
    state.planner_revision_selected_feature = _first_meaningful_line(
        _extract_md_section(body, "선정 후보"), max_chars=120,
    ) or None
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

    DESIGNER_FINAL_REVIEW_FILE.write_text(body + "\n", encoding="utf-8")
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

출하 기준 (반드시 준수):
- 총점 ≥ 24 → ship 후보
- Visual Desire Score ≥ 4 → 통과 (미달 시 디자이너 재작업)
- Share Score ≥ 4 → 통과 (3 이하면 공유 카드 개선 필요)
- Revisit Score ≥ 4 → 통과 (3 이하면 기획자 재작업)

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
    revision_md: str, final_review_md: str, scorecard: dict
) -> str:
    return PM_DECISION_PROMPT_TEMPLATE.format(
        planner_revision=revision_md.strip(),
        designer_final_review=final_review_md.strip(),
        scorecard_json=json.dumps(scorecard, ensure_ascii=False, indent=2),
    )


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
    scorecard = {
        "scores": dict(state.desire_scorecard),
        "total": state.desire_scorecard_total,
        "ship_ready": state.desire_scorecard_ship_ready,
        "rework": list(state.desire_scorecard_rework),
        "verdict": state.designer_final_review_verdict,
    }
    prompt = _build_pm_decision_prompt(revision_md, final_review_md, scorecard)
    ok, body = _run_pingpong_claude(prompt, "# Stampport PM Decision")
    sr.duration_sec = round(time.time() - t0, 3)
    if not ok:
        sr.status = "failed"
        sr.message = body[:200]
        state.pm_decision_status = "failed"
        state.pm_decision_message = sr.message
        return sr

    PM_DECISION_FILE.write_text(body + "\n", encoding="utf-8")
    decision_section = _extract_md_section(body, "출하 결정").lower()
    ship_word = "ship" in decision_section
    hold_word = "hold" in decision_section
    # PM is the final word: ship requires both the gate (ship_ready)
    # AND the explicit "ship" verdict. If either says hold, we hold.
    pm_ship = ship_word and not hold_word and state.desire_scorecard_ship_ready

    state.pm_decision_status = "generated"
    state.pm_decision_path = str(PM_DECISION_FILE)
    state.pm_decision_at = utc_now_iso()
    state.pm_decision_ship_ready = pm_ship
    summary = (
        ("SHIP" if pm_ship else "HOLD")
        + f" (총점 {state.desire_scorecard_total}/30"
        + (f", rework={','.join(state.desire_scorecard_rework)}"
           if state.desire_scorecard_rework else "")
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

    # Pre-condition 1: opt-in. Default OFF — never run unless explicitly
    # asked. We accept "true"/"1"/"yes" (case-insensitive) for ergonomics.
    enabled = os.environ.get("FACTORY_RUN_CLAUDE", "").strip().lower()
    if enabled not in {"true", "1", "yes", "on"}:
        return _skip("FACTORY_RUN_CLAUDE 미설정 — 기본 OFF (스킵)")

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

    # If Product Planner Mode produced a validated report this cycle,
    # feed it into the proposal prompt. The planner-aware template
    # constrains Claude to the selected feature's MVP scope so we
    # don't drift back into "edit a button label" territory.
    planner_md: str | None = None
    if (
        state.product_planner_status == "generated"
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

    PROPOSAL_FILE.write_text(body + "\n", encoding="utf-8")
    state.claude_proposal_status = "generated"
    state.claude_proposal_path = str(PROPOSAL_FILE)
    state.claude_proposal_at = utc_now_iso()
    state.claude_proposal_skipped_reason = None

    sr.status = "passed"
    sr.message = f"제안 생성 ({len(body)} chars, model={model})"
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

    pm_md = ""
    planner_md = ""
    proposal_md = ""
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

    feature = _selected_feature_for_ticket(state)
    state.implementation_ticket_selected_feature = feature

    target_files: list[str] = []
    for src in (proposal_md, pm_md, planner_md):
        if not src:
            continue
        target_files = _parse_target_files_from_md(src)
        if target_files:
            break
    target_screens = _parse_screens_from_md(planner_md) or _parse_screens_from_md(pm_md)

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
        try:
            IMPLEMENTATION_TICKET_FILE.write_text(body, encoding="utf-8")
        except OSError as e:
            sr.status = "failed"
            sr.message = f"ticket write failed: {e}"
            sr.duration_sec = round(time.time() - t0, 3)
            state.implementation_ticket_status = "failed"
            state.implementation_ticket_message = sr.message
            return sr
        sr.status = "skipped"
        sr.message = (
            "Implementation Ticket 비어 있음 — 수정 대상 파일이 명시되지 않아 "
            "이번 사이클은 planning_only 로 종료됩니다."
        )
        sr.duration_sec = round(time.time() - t0, 3)
        state.implementation_ticket_status = "missing"
        state.implementation_ticket_path = str(IMPLEMENTATION_TICKET_FILE)
        state.implementation_ticket_at = utc_now_iso()
        state.implementation_ticket_target_files = []
        state.implementation_ticket_target_screens = list(target_screens)
        state.implementation_ticket_message = sr.message
        _emit_cycle_log(
            state, "implementation_ticket_missing",
            "implementation ticket missing — 수정 대상 파일 없음, planning_only 로 종료 예정",
            feature=feature,
        )
        return sr

    body = _build_ticket_markdown(
        state,
        feature=feature,
        target_files=target_files,
        target_screens=target_screens,
        pm_md=pm_md,
        planner_md=planner_md,
        proposal_md=proposal_md,
    )
    try:
        IMPLEMENTATION_TICKET_FILE.write_text(body, encoding="utf-8")
    except OSError as e:
        sr.status = "failed"
        sr.message = f"ticket write failed: {e}"
        sr.duration_sec = round(time.time() - t0, 3)
        state.implementation_ticket_status = "failed"
        state.implementation_ticket_message = sr.message
        return sr

    state.implementation_ticket_status = "generated"
    state.implementation_ticket_path = str(IMPLEMENTATION_TICKET_FILE)
    state.implementation_ticket_at = utc_now_iso()
    state.implementation_ticket_target_files = list(target_files)
    state.implementation_ticket_target_screens = list(target_screens)
    state.implementation_ticket_message = (
        f"Implementation Ticket 작성됨 — 대상 파일 {len(target_files)}개"
    )
    _emit_cycle_log(
        state, "implementation_ticket_created",
        f"implementation ticket created — 대상 파일 {len(target_files)}개",
        feature=feature,
        target_files=target_files[:20],
    )
    sr.status = "passed"
    sr.message = state.implementation_ticket_message
    sr.duration_sec = round(time.time() - t0, 3)
    return sr


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
    (all_passed, list_of_failed_check_names)."""
    failures: list[str] = []
    npm = shutil.which("npm")

    # 1. app/web build
    web = REPO_ROOT / "app" / "web"
    if web.is_dir() and npm:
        ok, _ = _run([npm, "run", "build"], cwd=web, timeout=300, env_override={"CI": "1"})
        if not ok:
            failures.append("build_app")

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

    # Pre-condition 1: opt-in. Default OFF.
    enabled = os.environ.get("FACTORY_APPLY_CLAUDE", "").strip().lower()
    if enabled not in {"true", "1", "yes", "on"}:
        return _skip("FACTORY_APPLY_CLAUDE 미설정 — 기본 OFF (스킵)")

    # Pre-condition 2: this cycle must have produced a fresh proposal.
    # We refuse to apply a stale proposal from a previous run because
    # the working tree may have shifted underneath it.
    if state.claude_proposal_status != "generated":
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

    # Pre-condition 5: tools.
    claude_bin = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not claude_bin:
        return _skip("claude CLI 미설치 — 스킵")
    if not PROPOSAL_FILE.is_file():
        return _skip("claude_proposal.md 없음 — 스킵")
    proposal_text = PROPOSAL_FILE.read_text(encoding="utf-8").strip()
    if not proposal_text:
        return _skip("제안 본문이 비어있음 — 스킵")

    # Snapshot before — we'll diff against this to know what to roll back.
    before_hashes = _hash_tracked_under_allowed()
    before_untracked = _untracked_under_allowed()

    prompt = _build_claude_apply_prompt(proposal_text)
    model = os.environ.get("FACTORY_CLAUDE_MODEL", "sonnet").strip() or "sonnet"
    budget_usd = os.environ.get("FACTORY_CLAUDE_BUDGET_USD", "1.0").strip() or "1.0"
    timeout_sec = float(os.environ.get("FACTORY_CLAUDE_APPLY_TIMEOUT_SEC", "900"))

    argv = [
        claude_bin,
        "-p", prompt,
        "--allowed-tools", "Read,Glob,Grep,Edit,Write",
        "--output-format", "text",
        "--model", model,
        "--max-budget-usd", budget_usd,
    ]
    apply_ok, apply_out = _run(argv, cwd=REPO_ROOT, timeout=timeout_sec)

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
        sr.status = "failed"
        sr.message = "claude CLI 실행 실패"
        sr.detail = (apply_out or "")[-1500:]
        state.claude_apply_status = "failed"
        state.claude_apply_changed_files = []
        state.claude_apply_message = sr.message
        sr.duration_sec = round(time.time() - t0, 3)
        return sr

    # Claude succeeded but didn't actually touch anything — that's fine,
    # treat as a no-op.
    if not changed_tracked and not new_untracked:
        sr.status = "skipped"
        sr.message = "claude가 어떤 파일도 변경하지 않음"
        sr.detail = (apply_out or "")[-800:]
        state.claude_apply_status = "noop"
        state.claude_apply_skipped_reason = "claude no-op"
        state.claude_apply_message = sr.message
        _emit_cycle_log(
            state, "claude_apply_no_changes",
            "claude apply no changes — Claude가 working tree를 건드리지 않음",
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
    if state.product_planner_status == "generated" and ok:
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
    )

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
    enabled = os.environ.get("FACTORY_RUN_CLAUDE", "").strip().lower()
    if enabled not in {"true", "1", "yes", "on"}:
        return _skip("FACTORY_RUN_CLAUDE 미설정 — QA 수정 제안 스킵")
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
    if state.product_planner_status == "generated":
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
    if PAUSE_FILE.exists():
        _log("paused marker present — skipping cycle")
        return 0

    RUNTIME.mkdir(parents=True, exist_ok=True)
    state = CycleState(cycle=_load_cycle_number())
    state.goal = _read_goal()
    state.current_stage = "prepare"
    state.current_task = "사이클 준비"
    state.last_message = "자동 점검 사이클 시작"
    _log(f"cycle #{state.cycle} start (goal={state.goal[:40]}…)")
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
    run_stage("product_planning", lambda: stage_product_planning(state))
    # Planner ↔ Designer ping-pong. Each stage no-ops (skipped) when
    # FACTORY_PLANNER_DESIGNER_PINGPONG is unset, so existing flows
    # are unaffected. When enabled, the four stages produce the
    # designer_critique / planner_revision / designer_final_review /
    # pm_decision artifacts and populate the desire scorecard.
    run_stage("designer_critique",     lambda: stage_designer_critique(state))
    run_stage("planner_revision",      lambda: stage_planner_revision(state))
    run_stage("designer_final_review", lambda: stage_designer_final_review(state))
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
        # No failed stages, but no actual code change either. Distinguish
        # planning_only (planner / designer / pm artifact freshly generated)
        # vs no_code_change (everything skipped).
        planner_generated = (
            state.product_planner_status == "generated"
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
        state.no_code_change_reason = (
            f"planning_only:{reason}" if planner_generated
            else f"no_code_change:{reason}"
        )
        if planner_generated:
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
        # didn't pass — even if claude_apply landed code changes, the
        # supervisor may flag designer/QA/deploy retry. Downgrade to
        # planning_only in that case.
        if state.status == "succeeded" and sup_overall != "pass":
            prior_status = state.status
            if not sup_meaningful or not sup_ticket_ok:
                state.status = "planning_only"
                state.code_changed = False
            else:
                # meaningful + ticket but agent quality lacking — keep
                # files-changed marker but force planning_only label.
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
    # planning_only / no_code_change all leave the working tree clean.
    # The bash factory loop reads the exit code as "did the cycle
    # crash?", not "did it ship code", so a clean planning_only run
    # should not look like a crash.
    return 0 if state.status in {"succeeded", "planning_only", "no_code_change"} else 1


if __name__ == "__main__":
    sys.exit(main())
