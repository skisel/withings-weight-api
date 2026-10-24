#!/usr/bin/env bash
# Start the one-time Withings OAuth consent: gets a fresh authorize URL (admin key) and opens it.
set -euo pipefail
source "$(dirname "$0")/config.sh"

response="$(admin_curl "$(service_url)/auth/start")"
authorize_url="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["authorize_url"])' <<<"$response")"
log "Open this URL within 10 minutes and grant access:"
echo "$authorize_url"
if command -v open >/dev/null 2>&1; then open "$authorize_url"; fi
log "Afterwards check: scripts/api.sh /api/sync/status"
