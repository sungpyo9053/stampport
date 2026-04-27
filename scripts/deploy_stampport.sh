#!/usr/bin/env bash
# Deploy Stampport (frontend + backend) on the reviewdr.kr Lightsail box.
#
# Run this as the `admin` user on the server:
#
#     cd /home/admin/stampport
#     bash scripts/deploy_stampport.sh
#
# Idempotent. Safe to re-run after each `git pull`. Does NOT touch
# the existing ReviewDr deployment, the root location in nginx, or
# port 8000.
#
# What it does, in order:
#   1. git pull (if running from a checkout)
#   2. install/refresh the Python venv at app/api/.venv
#   3. npm install + vite build for app/web
#   4. rsync dist/ → /var/www/stampport
#   5. install systemd unit + reload + restart stampport-api
#   6. reload nginx (after `nginx -t`)
#   7. health-check the API both locally and through nginx

set -euo pipefail

# --- Config ----------------------------------------------------------------
REPO_ROOT="/home/admin/stampport"
API_DIR="$REPO_ROOT/app/api"
WEB_DIR="$REPO_ROOT/app/web"
WEB_DEPLOY_DIR="/var/www/stampport"
SYSTEMD_UNIT_SRC="$REPO_ROOT/deploy/stampport-api.service"
SYSTEMD_UNIT_DST="/etc/systemd/system/stampport-api.service"
SERVICE_NAME="stampport-api"
API_PORT="8200"
PUBLIC_HEALTH_URL="https://reviewdr.kr/stampport-api/health"

# --- Helpers ---------------------------------------------------------------
log()  { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[1;33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[1;31m✗ %s\033[0m\n" "$*" >&2; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "필수 명령이 없습니다: $1"
}

# --- 0. Sanity --------------------------------------------------------------
log "0/7 환경 확인"
[[ -d "$REPO_ROOT" ]] || die "리포지토리 경로가 없습니다: $REPO_ROOT"
require_cmd git
require_cmd python3
require_cmd npm
require_cmd rsync
require_cmd sudo
ok "환경 OK"

cd "$REPO_ROOT"

# --- 1. Source update -------------------------------------------------------
log "1/7 git pull"
if [[ -d .git ]]; then
  git pull --ff-only
  ok "git pull 완료"
else
  warn ".git 디렉터리가 없어 git pull을 건너뜁니다"
fi

# --- 2. Backend venv --------------------------------------------------------
log "2/7 백엔드 가상환경 준비"
cd "$API_DIR"

if [[ ! -d .venv ]]; then
  if ! python3 -m venv .venv 2>/dev/null; then
    warn "python3 -m venv 실패 — python3-venv 설치 시도"
    sudo apt update -y
    sudo apt install -y python3-venv python3-full
    rm -rf .venv
    python3 -m venv .venv
  fi
  ok "venv 생성"
else
  ok "venv 존재 (재사용)"
fi

# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt
deactivate
ok "Python 의존성 설치/업데이트 완료"

# --- 3. Frontend build ------------------------------------------------------
log "3/7 프론트엔드 빌드"
cd "$WEB_DIR"
# `npm ci` would be stricter, but it requires a committed package-lock.json.
# Fall back to `npm install` if no lockfile exists yet.
if [[ -f package-lock.json ]]; then
  npm ci
else
  npm install
fi
npm run build
[[ -d dist ]] || die "vite build 결과물(dist)이 없습니다"
ok "vite build 완료"

# --- 4. Publish dist → /var/www/stampport ----------------------------------
log "4/7 빌드 결과물 배포"
sudo mkdir -p "$WEB_DEPLOY_DIR"
# --delete keeps /var/www/stampport in sync with dist (no stale assets).
sudo rsync -a --delete "$WEB_DIR/dist/" "$WEB_DEPLOY_DIR/"
sudo chown -R www-data:www-data "$WEB_DEPLOY_DIR" 2>/dev/null || true
ok "rsync → $WEB_DEPLOY_DIR 완료"

# --- 5. Systemd unit --------------------------------------------------------
log "5/7 systemd 등록 / 재시작"
sudo install -m 0644 "$SYSTEMD_UNIT_SRC" "$SYSTEMD_UNIT_DST"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" >/dev/null
sudo systemctl restart "$SERVICE_NAME"
sleep 1
sudo systemctl --no-pager --lines=0 status "$SERVICE_NAME" || true
ok "$SERVICE_NAME 재시작 완료 (포트 $API_PORT)"

# --- 6. Nginx reload --------------------------------------------------------
log "6/7 nginx 검증 / reload"
if sudo nginx -t; then
  sudo systemctl reload nginx
  ok "nginx reload 완료"
else
  die "nginx 설정 검증 실패 — deploy/nginx-stampport.conf 가 sites-available/default 에 포함됐는지 확인"
fi

# --- 7. Health checks -------------------------------------------------------
log "7/7 헬스 체크"
sleep 1
LOCAL_URL="http://127.0.0.1:${API_PORT}/health"
if curl -fsS "$LOCAL_URL" >/dev/null; then
  ok "로컬 헬스 OK ($LOCAL_URL)"
else
  die "로컬 헬스 실패: $LOCAL_URL — journalctl -u $SERVICE_NAME -n 50 으로 확인하세요"
fi

if curl -fsS "$PUBLIC_HEALTH_URL" >/dev/null; then
  ok "공개 헬스 OK ($PUBLIC_HEALTH_URL)"
else
  warn "공개 헬스 실패: $PUBLIC_HEALTH_URL — nginx location /stampport-api/ 설정과 인증서를 확인하세요"
fi

cat <<EOF

\033[1;32m✅ Stampport 배포 완료\033[0m
  Frontend : https://reviewdr.kr/stampport/
  API      : https://reviewdr.kr/stampport-api/health
  Service  : sudo systemctl status $SERVICE_NAME

기존 ReviewDr (포트 8000, 루트 /) 는 건드리지 않았습니다.
EOF
