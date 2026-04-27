#!/usr/bin/env bash
# Server-side deploy step, invoked over SSH by the GitHub Actions
# workflow after it has rsynced source + built dist files into:
#
#     /home/admin/stampport/                      (repo source)
#     /home/admin/stampport-stage/web/dist/       (built app/web)
#     /home/admin/stampport-stage/control/dist/   (built control_tower/web)
#
# Idempotent. Safe to re-run. Does NOT touch:
#     - /home/admin/review-doctor
#     - the existing nginx root `location /`
#     - the ReviewDr API on 127.0.0.1:8000
#
# What it does:
#     1. Promote built dist files into /var/www/{stampport,stampport-control}
#     2. Refresh both backend venvs and reinstall requirements.txt
#     3. Install / refresh both systemd units, daemon-reload, restart
#     4. Apply nginx snippet (backup + idempotent include + reload)
#     5. Local healthcheck on both APIs

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/home/admin/stampport}"
STAGE_ROOT="${STAGE_ROOT:-/home/admin/stampport-stage}"

APP_API_DIR="$REPO_ROOT/app/api"
CTRL_API_DIR="$REPO_ROOT/control_tower/api"

APP_WEB_DIST="$STAGE_ROOT/web/dist"
CTRL_WEB_DIST="$STAGE_ROOT/control/dist"

APP_WEB_DEPLOY="/var/www/stampport"
CTRL_WEB_DEPLOY="/var/www/stampport-control"

APP_API_PORT="8200"
CTRL_API_PORT="8210"

SYSTEMD_DIR="/etc/systemd/system"

log()  { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[1;33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[1;31m✗ %s\033[0m\n" "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 0. Sanity
# ---------------------------------------------------------------------------
log "0/5 환경 확인"
[[ -d "$REPO_ROOT" ]]    || die "repo root missing: $REPO_ROOT"
[[ -d "$APP_API_DIR" ]]  || die "app api missing: $APP_API_DIR"
[[ -d "$CTRL_API_DIR" ]] || die "control tower api missing: $CTRL_API_DIR"
[[ -d "$APP_WEB_DIST" ]] || die "app web dist missing: $APP_WEB_DIST (CI rsync step likely failed)"
[[ -d "$CTRL_WEB_DIST" ]] || die "control web dist missing: $CTRL_WEB_DIST"
command -v rsync   >/dev/null || die "rsync not installed"
command -v python3 >/dev/null || die "python3 not installed"
command -v sudo    >/dev/null || die "sudo not installed"
ok "OK"

# ---------------------------------------------------------------------------
# 1. Publish frontends
# ---------------------------------------------------------------------------
log "1/5 정적 산출물 배포"
sudo mkdir -p "$APP_WEB_DEPLOY" "$CTRL_WEB_DEPLOY"
sudo rsync -a --delete "$APP_WEB_DIST/"  "$APP_WEB_DEPLOY/"
sudo rsync -a --delete "$CTRL_WEB_DIST/" "$CTRL_WEB_DEPLOY/"
sudo chown -R www-data:www-data "$APP_WEB_DEPLOY" "$CTRL_WEB_DEPLOY" 2>/dev/null || true
ok "$APP_WEB_DEPLOY · $CTRL_WEB_DEPLOY 갱신 완료"

# ---------------------------------------------------------------------------
# 2. Backend venvs (refresh in place)
# ---------------------------------------------------------------------------
log "2/5 백엔드 가상환경 / 의존성"
ensure_venv() {
  local dir="$1"
  cd "$dir"
  if [[ ! -d .venv ]]; then
    if ! python3 -m venv .venv 2>/dev/null; then
      warn "python3-venv 누락 → apt 설치 시도"
      sudo apt update -y
      sudo apt install -y python3-venv python3-full
      python3 -m venv .venv
    fi
    ok "venv 생성: $dir/.venv"
  fi
  ./.venv/bin/pip install --upgrade pip >/dev/null
  ./.venv/bin/pip install -r requirements.txt
  ok "requirements.txt 적용: $dir"
}
ensure_venv "$APP_API_DIR"
ensure_venv "$CTRL_API_DIR"

# ---------------------------------------------------------------------------
# 3. systemd units
# ---------------------------------------------------------------------------
log "3/5 systemd 등록 / 재시작"
sudo install -m 0644 "$REPO_ROOT/deploy/stampport-api.service" \
    "$SYSTEMD_DIR/stampport-api.service"
sudo install -m 0644 "$REPO_ROOT/deploy/stampport-control-api.service" \
    "$SYSTEMD_DIR/stampport-control-api.service"
sudo systemctl daemon-reload
sudo systemctl enable stampport-api stampport-control-api >/dev/null
sudo systemctl restart stampport-api
sudo systemctl restart stampport-control-api
sleep 2
sudo systemctl --no-pager --lines=0 status stampport-api          || true
sudo systemctl --no-pager --lines=0 status stampport-control-api  || true
ok "stampport-api(:$APP_API_PORT) · stampport-control-api(:$CTRL_API_PORT) 재시작"

# ---------------------------------------------------------------------------
# 4. nginx
# ---------------------------------------------------------------------------
log "4/5 nginx 설정 적용"
bash "$REPO_ROOT/scripts/server_apply_nginx.sh"
ok "nginx 갱신 완료"

# ---------------------------------------------------------------------------
# 5. Local healthcheck (the public healthcheck is run from the CI runner)
# ---------------------------------------------------------------------------
log "5/5 로컬 헬스 체크"
for url in "http://127.0.0.1:$APP_API_PORT/health" "http://127.0.0.1:$CTRL_API_PORT/health"; do
  if curl -fsS --max-time 5 "$url" >/dev/null; then
    ok "OK $url"
  else
    die "FAIL $url — journalctl -u stampport-api / stampport-control-api 확인"
  fi
done

cat <<EOF

\033[1;32m✅ 서버 측 배포 완료\033[0m
  Frontend (app)     : https://reviewdr.kr/stampport/
  Frontend (control) : https://reviewdr.kr/stampport-control/
  API      (app)     : https://reviewdr.kr/stampport-api/health
  API      (control) : https://reviewdr.kr/stampport-control-api/health
EOF
