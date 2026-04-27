#!/usr/bin/env bash
# Stampport 자동화 공장 — SSH 기반 자동 배포 스크립트.
#
# control_tower/local_runner/runner.py 의 deploy_to_server 명령이 이
# 스크립트를 호출합니다. 단독 실행도 지원하며, 기본은 dry-run이라
# 실제 SSH가 나가려면 LOCAL_RUNNER_DEPLOY_DRY_RUN=false 가 필요합니다.
#
# 환경변수
# ---------
#   STAMPPORT_DEPLOY_HOST          (필수) 대상 호스트
#   STAMPPORT_DEPLOY_USER          (필수) SSH 사용자
#   STAMPPORT_DEPLOY_SSH_KEY       (선택) SSH key 경로
#   STAMPPORT_DEPLOY_REMOTE_DIR    (선택) 리포 경로. 기본 /home/admin/stampport
#   STAMPPORT_DEPLOY_BRANCH        (선택) 브랜치. 기본 main
#   STAMPPORT_DEPLOY_BASE_URL      (선택) 헬스체크 prefix. 기본 https://reviewdr.kr
#   LOCAL_RUNNER_DEPLOY_DRY_RUN    (선택) true(기본) 면 SSH/curl 실행 대신
#                                  실행할 명령을 그대로 출력만 합니다.
#
# 종료 코드
# ----------
#   0  성공
#   1+ 실패 — stderr에 단계별 사유 출력
#
# 안전 정책
# ----------
# - SSH는 BatchMode + ConnectTimeout으로 묶여 인터랙티브 입력이 불가능합니다.
# - 원격 명령은 ssh ... bash -s 에 stdin 으로 흘려보내 argv에 사용자 입력이
#   끼지 않습니다.
# - dry-run에서는 어떤 외부 호출도 발생하지 않습니다 (echo만).

set -euo pipefail

HOST="${STAMPPORT_DEPLOY_HOST:-}"
DEPLOY_USER="${STAMPPORT_DEPLOY_USER:-}"
KEY="${STAMPPORT_DEPLOY_SSH_KEY:-}"
REMOTE_DIR="${STAMPPORT_DEPLOY_REMOTE_DIR:-/home/admin/stampport}"
BRANCH="${STAMPPORT_DEPLOY_BRANCH:-main}"
BASE_URL="${STAMPPORT_DEPLOY_BASE_URL:-https://reviewdr.kr}"
DRY_RUN="${LOCAL_RUNNER_DEPLOY_DRY_RUN:-true}"

die() { printf "  ✗ %s\n" "$*" >&2; exit 1; }
log() { printf "▶ %s\n" "$*"; }
ok()  { printf "  ✓ %s\n" "$*"; }

[[ -n "$HOST" ]]        || die "STAMPPORT_DEPLOY_HOST 미설정"
[[ -n "$DEPLOY_USER" ]] || die "STAMPPORT_DEPLOY_USER 미설정"

# Whitespace in REMOTE_DIR / BRANCH would let a hostile env var inject
# extra commands into the remote heredoc. Reject early.
case "$REMOTE_DIR" in *[$'\n\r\t ;|&`$']*) die "REMOTE_DIR 부적합 문자 포함" ;; esac
case "$BRANCH"     in *[$'\n\r\t ;|&`$']*) die "BRANCH 부적합 문자 포함" ;; esac

ssh_args=(
  "-o" "BatchMode=yes"
  "-o" "StrictHostKeyChecking=accept-new"
  "-o" "ConnectTimeout=10"
)
if [[ -n "$KEY" ]]; then
  [[ -f "$KEY" ]] || die "SSH key 경로 잘못됨: $KEY"
  ssh_args+=("-i" "$KEY")
fi

# Remote script is generated once and reused for echo-on-dry-run +
# stdin pipe on real run. Quoting the heredoc with 'EOSCRIPT' prevents
# any local expansion — variables are baked in via printf -v above.
read -r -d '' REMOTE_SCRIPT <<EOSCRIPT || true
set -euo pipefail
cd "$REMOTE_DIR"
echo "▶ git pull --ff-only origin $BRANCH"
git pull --ff-only origin "$BRANCH"

echo "▶ app/web npm install + build"
cd "$REMOTE_DIR/app/web"
npm install --no-audit --no-fund
npm run build
sudo mkdir -p /var/www/stampport
sudo rm -rf /var/www/stampport/*
sudo cp -r dist/. /var/www/stampport/
sudo chown -R www-data:www-data /var/www/stampport 2>/dev/null || true

echo "▶ control_tower/web npm install + build"
cd "$REMOTE_DIR/control_tower/web"
npm install --no-audit --no-fund
npm run build
sudo mkdir -p /var/www/stampport-control
sudo rm -rf /var/www/stampport-control/*
sudo cp -r dist/. /var/www/stampport-control/
sudo chown -R www-data:www-data /var/www/stampport-control 2>/dev/null || true

echo "✅ remote deploy 완료"
EOSCRIPT

run_ssh() {
  if [[ "$DRY_RUN" == "true" || "$DRY_RUN" == "1" ]]; then
    echo "[DRY RUN] would ssh ${ssh_args[*]} ${DEPLOY_USER}@${HOST} bash -s <<EOSCRIPT"
    printf '%s\n' "$REMOTE_SCRIPT" | sed 's/^/  | /'
    echo "[DRY RUN] EOSCRIPT"
    return 0
  fi
  printf '%s\n' "$REMOTE_SCRIPT" | ssh "${ssh_args[@]}" "${DEPLOY_USER}@${HOST}" "bash -s"
}

# Curl helper — dry-run mode just prints the URL. mode=GET runs `curl
# -fsS` (full body fetch — fails on 4xx/5xx); mode=HEAD runs `curl -fsS
# -I` (HEAD-only — picks up nginx 200 without downloading the SPA).
run_curl() {
  local mode="$1" url="$2"
  if [[ "$DRY_RUN" == "true" || "$DRY_RUN" == "1" ]]; then
    echo "[DRY RUN] would curl $mode $url"
    return 0
  fi
  if [[ "$mode" == "GET" ]]; then
    curl -fsS --max-time 10 "$url" >/dev/null
  else
    curl -fsSI --max-time 10 "$url" >/dev/null
  fi
}

log "1/4 SSH 배포 (${DEPLOY_USER}@${HOST}:${REMOTE_DIR} · branch=${BRANCH} · dry_run=${DRY_RUN})"
run_ssh
ok "SSH 배포 완료"

log "2/4 control API 헬스 체크 — ${BASE_URL}/stampport-control-api/health"
run_curl GET  "${BASE_URL}/stampport-control-api/health"
ok "stampport-control-api 헬스 OK"

log "3/4 stampport 앱 URL 확인 — ${BASE_URL}/stampport/"
run_curl HEAD "${BASE_URL}/stampport/"
ok "stampport 앱 URL OK"

log "4/4 stampport 관제실 URL 확인 — ${BASE_URL}/stampport-control/"
run_curl HEAD "${BASE_URL}/stampport-control/"
ok "stampport 관제실 URL OK"

if [[ "$DRY_RUN" == "true" || "$DRY_RUN" == "1" ]]; then
  echo "✅ Stampport 서버 배포 dry-run 완료 (실제 변경 없음)"
else
  echo "✅ Stampport 서버 배포 성공"
fi
