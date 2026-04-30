# Factory Observer — 실패 중계자 봇

## 무엇이고 왜 필요한가

운영자가 매번 Control Tower 화면을 보고, 로그를 복사해서 ChatGPT 에 전달하고,
다시 Claude Code 용 수정 prompt 를 받는 반복은 비효율적입니다.

**Factory Observer** 는 그 반복을 줄이기 위한 **읽기 전용 진단 봇**입니다.
`.runtime/*.json` + `local_factory.log` 를 읽어, 어떤 실패인지 자동 분류하고,
운영자용 요약 리포트와 Claude Code 용 수정 prompt 를 동시에 생성합니다.

```
.runtime/control_state.json ─┐
.runtime/factory_state.json ─┤
.runtime/pipeline_state.json ─┤    ┌─ .runtime/factory_failure_report.md
.runtime/forward_progress... ─┼─→  │   (운영자용 요약)
.runtime/deploy_progress.json ─┤    │
.runtime/factory_publish.json ─┤    └─ .runtime/claude_repair_prompt.md
.runtime/qa_diagnostics.json ─┤        (Claude Code 에 그대로 붙여넣기)
.runtime/local_factory.log ──┘
ps aux ─────────────────────┘
```

## 핵심 원칙 — 안전 모드

Observer 는 **절대로** 다음을 하지 않습니다:

- 코드 수정
- `git add` / `git commit` / `git push`
- runner 프로세스 kill
- `.runtime/` 파일 삭제 (자체 출력 4종 제외)
- Claude 자동 실행

오직 **상태를 읽고**, **리포트를 쓰고**, **운영자가 다음에 무엇을 해야 할지를 안내**합니다.

## 명령

### 1회 진단

```
python3 -m control_tower.local_runner.factory_observer --once
```

`--once` 는 한 번 진단하고 종료합니다. 결과는 stdout 요약 + 4 개 출력 파일.

### 감시 모드

```
python3 -m control_tower.local_runner.factory_observer --watch --interval 300
```

`--interval` 초마다 진단을 반복합니다 (기본 300 초 = 5 분). `Ctrl-C` 로 종료.

### 자체 테스트

```
python3 -m control_tower.local_runner.factory_observer --self-test
```

코드 안에 내장된 acceptance fixture 테스트 7 개 (A–F + bonus) 를 실행합니다.

## 출력 파일

| 파일 | 용도 |
| ---- | ---- |
| `.runtime/factory_failure_report.md` | 운영자용 요약. 현재 상태, root cause, 근거 로그, 수동 확인 명령, 위험도, 자동 수정 가능 여부. |
| `.runtime/claude_repair_prompt.md` | Claude Code 에 그대로 붙여넣을 수 있는 수정 요청. 증상 / 재현 로그 / root cause / 수정 대상 파일 후보 / 수정 요구사항 / acceptance test / 검증 명령 / commit message 후보 포함. |
| `.runtime/factory_manual_review_guide.md` | `publish_required` 케이스 전용. 실패가 아닌 review/publish 대기 상태에서만 생성. |
| `.runtime/factory_observer_state.json` | 마지막 진단 결과 + 출력 파일 경로 + runner process 수. |
| `.runtime/factory_observer.log` | 매 tick 의 한 줄짜리 활동 로그. |

## 진단 코드 — 15 종

| code | 분류 | 설명 |
| ---- | ---- | ---- |
| `duplicate_runner` | failure | `ps aux` 가 runner 프로세스 2 개 이상 감지. |
| `stale_runner` | failure | `pipeline_state` / `control_state` 가 stale_runner 진단을 들고 있음. runner.py 가 부팅 이후 수정됐거나 다른 runner 가 같은 .runtime 점유. |
| `runner_offline` | failure | `control_state.liveness.runner_online == false` — runner 사망 / heartbeat 끊김. |
| `git_add_ignored_file` | failure | `local_factory.log` 또는 `deploy_progress` 에 `__pycache__` / `*.pyc` / "ignored by .gitignore" 마커. |
| `git_add_failed` | failure | `factory_command_diagnostics.failed_stage == "git_add"`. |
| `qa_not_run` | failure | `factory_command_diagnostics.diagnostic_code == "qa_not_run"`. QA 단계 도달 실패. |
| `qa_gate_failed` | failure | `qa_status == failed` 이면서 실제 변경 파일이 있음. |
| `claude_apply_failed_no_code_change` | failure | ticket=generated 인데 `claude_apply` 가 0 파일 변경. |
| `implementation_ticket_missing` | failure | PM 결정은 있는데 implementation_ticket.md 가 missing/skipped. **repair prompt 에 fallback ticket 생성 요구를 포함합니다.** |
| `planner_required_output_missing` | failure | Product Planner 가 가드에 실패하고 fallback 도 진행되지 않음. |
| `current_stage_stuck` | failure | `forward_progress.status == "stuck"`. |
| `actions_pending_timeout` | failure | push 성공 후 GitHub Actions 가 30 분 이상 pending. |
| `old_deploy_failed_stale` | failure | `deploy_progress.status == failed` 인데 `control_state.deploy.status` 는 이미 ready/no_changes 로 재분류한 stale UI 케이스. |
| `publish_required` | **review (실패 아님)** | 코드 변경 + QA 통과 + commit 없음 — review/publish 대기. `factory_manual_review_guide.md` 를 생성합니다. |
| `unknown` | failure | 위 어디에도 해당하지 않는 비-healthy 상태. raw evidence 만 첨부. 분류 추가 필요. |

분류 우선순위는 위 표의 위에서 아래 순서입니다 (`duplicate_runner` 가 가장 강한 시그널).

## 중복 runner 감지 — 어떻게 동작하는가

Observer 는 매 tick 에서 `ps aux` 를 한 번 실행하고,
`control_tower.local_runner.runner` 토큰을 포함한 줄을 셉니다.
자기 자신 (`factory_observer`) 과 `grep` 줄은 자동 제외됩니다.

2 개 이상이면 즉시 `duplicate_runner` 로 분류하고 repair prompt 에 다음을 포함:

```
pkill -f control_tower.local_runner.runner
ps aux | grep control_tower.local_runner.runner | grep -v grep   # 0 개 확인
python3 -m control_tower.local_runner.runner                      # 1 개만 재실행
```

## stale_runner 시 권장 조치

`repair prompt` 에 다음 4 단계를 자동 포함합니다:

1. 모든 runner 프로세스 종료
2. `git pull --ff-only`
3. `.runtime/factory_pause.marker` 제거 (있으면)
4. runner 1 개만 재실행

같은 stale_runner 가 24 시간 내 3 회 이상 발생하면 `runner.py` 의 boot stamp 비교
로직을 점검하라는 권고가 추가됩니다.

## publish_required — 실패 아님

다음 4 조건이 모두 참이면 Observer 는 **실패가 아니라 review 대기**로 분류합니다:

- `claude_apply_status == "applied"` 또는 `changed_files_count > 0`
- `qa_status == "passed"`
- `commit_hash` 비어있음
- `push_status` ∉ {ok, succeeded}

이 경우 `claude_repair_prompt.md` 는 "수정 요청 없음" passive prompt 가 되고,
대신 `factory_manual_review_guide.md` 가 생성됩니다. 운영자는 이 가이드를 보고
UI 의 `publish_changes` / `deploy_to_server` 를 직접 눌러 publish 합니다.

## Acceptance test 매핑

self-test 가 검증하는 케이스:

| 테스트 | 입력 | 기대 결과 |
| ----- | ---- | -------- |
| A | `pipeline_state.diagnostic_code == "stale_runner"` | `diagnostic_code == "stale_runner"` |
| B | `local_factory.log` 에 `__pycache__` + `.gitignore 파일 중 하나 때문에 무시합니다` | `diagnostic_code == "git_add_ignored_file"` |
| C | `pm_decision_status=generated` + `implementation_ticket_status=missing` | `diagnostic_code == "implementation_ticket_missing"` 그리고 repair prompt 에 "fallback ticket" 요구 포함 |
| D | `changed_files=3` + `qa_status=passed` + commit 없음 | `diagnostic_code == "publish_required"` 그리고 `is_failure == false` |
| E | runner process 2 개 | `diagnostic_code == "duplicate_runner"` |
| F | 매핑되지 않은 status | `diagnostic_code == "unknown"` 그리고 evidence 에 raw control_state 필드 포함 |
| G (bonus) | `ps aux` 출력에 runner / grep / observer 혼재 | `detect_runner_processes` 가 1 개만 반환 |

```
python3 -m control_tower.local_runner.factory_observer --self-test
```

## 통합 — 다른 진단 봇과의 관계

Stampport 는 이미 두 개의 진단 레이어를 갖고 있습니다:

1. **Watchdog** (`runner.py` 내부) — pipeline / forward_progress / supervisor 를
   실시간으로 평가하고 control_state 를 갱신.
2. **Pipeline Doctor** (`pipeline_doctor.py`) — control_state 를 읽고 5 개의
   minimum diagnostic code 로 분류, `pipeline_doctor_repair_prompt.md` 를 생성.

**Factory Observer** 는 그 위에 한 층 더 얹힙니다:

- Watchdog/Doctor 가 분류하지 못하는 **운영 환경 문제** (`duplicate_runner`,
  `stale_runner`, `git_add_ignored_file`, `actions_pending_timeout` 등) 까지 포함.
- ChatGPT 왕복을 줄이는 것이 1 차 목표 — 운영자가 한 번 명령으로 두 종류의
  결과물 (사람용 요약 + Claude 용 prompt) 을 동시에 받습니다.
- Doctor 와 달리 **runner 프로세스 자체** 를 진단합니다 (`ps aux`).

## 운영 흐름 예시

```
# 1. Control Tower 가 빨갛게 변함 → 일단 Observer 한 번
python3 -m control_tower.local_runner.factory_observer --once

# 2. 사람이 보기 좋은 요약을 먼저 확인
cat .runtime/factory_failure_report.md

# 3. 자동 수정 가능 여부 = "아니오 (운영자 검토 필요)" 라면
#    Claude Code 에 prompt 를 그대로 붙여넣어 수정 위임
cat .runtime/claude_repair_prompt.md   # → 복사 → Claude Code

# 4. publish_required 였다면 review guide 따라 직접 publish
cat .runtime/factory_manual_review_guide.md
```

## 미래 확장 (현재는 미구현)

- 자동 수정 모드 — 환경변수 게이트 뒤에서 일부 코드(`fallback ticket` 생성
  같은) 를 자동으로 적용. 안전 모드를 유지한 채 단계적으로 활성화 예정.
- Slack / push notification 연동 — operator_required 진단 시 알림.
- diagnostic 반복 카운터 — 같은 코드가 N 회 반복되면 escalation.
