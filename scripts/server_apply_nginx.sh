#!/usr/bin/env bash
# Refresh /etc/nginx/snippets/stampport-locations.conf from the repo and
# make sure /etc/nginx/sites-available/default `include`s it. Idempotent.
#
# Run on the server (called by server_deploy.sh, but safe to run manually):
#
#     bash /home/admin/stampport/scripts/server_apply_nginx.sh
#
# Does NOT touch the existing root location (ReviewDr) or any other
# already-present locations.

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/home/admin/stampport}"
SNIPPET_SRC="$REPO_ROOT/deploy/nginx-stampport.conf"
SNIPPET_DST="/etc/nginx/snippets/stampport-locations.conf"
PATCHER="$REPO_ROOT/scripts/server_patch_nginx.py"
NGINX_CONF="/etc/nginx/sites-available/default"

[[ -f "$SNIPPET_SRC" ]] || { echo "snippet missing: $SNIPPET_SRC" >&2; exit 1; }
[[ -f "$NGINX_CONF" ]]  || { echo "nginx conf missing: $NGINX_CONF" >&2; exit 1; }
[[ -f "$PATCHER" ]]     || { echo "patcher missing: $PATCHER" >&2; exit 1; }

# 1. Drop the latest snippet into snippets/ (always overwrite).
sudo install -d -m 0755 /etc/nginx/snippets
sudo install -m 0644 "$SNIPPET_SRC" "$SNIPPET_DST"
echo "snippet refreshed → $SNIPPET_DST"

# 2. Inject the include line into the SSL server block (idempotent).
sudo python3 "$PATCHER" --conf "$NGINX_CONF" --backup-dir /etc/nginx/backups

# 3. Validate and reload — fail loudly if the resulting config is bad.
sudo nginx -t
sudo systemctl reload nginx
echo "nginx reloaded"
