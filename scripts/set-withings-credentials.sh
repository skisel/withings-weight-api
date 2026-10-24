#!/usr/bin/env bash
# Store the Withings developer app client id/secret in Secret Manager (prompts, nothing echoed).
set -euo pipefail
source "$(dirname "$0")/config.sh"

read -r -p "Withings client id: " client_id
read -r -s -p "Withings client secret: " client_secret
echo
[[ -n "$client_id" && -n "$client_secret" ]] || { warn "Both values are required"; exit 1; }

put_secret withings-client-id "$client_id"
put_secret withings-client-secret "$client_secret"
log "Stored Withings credentials."

if gcloud run services describe "$SERVICE" --region="$REGION" >/dev/null 2>&1; then
  roll_service
fi
