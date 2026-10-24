#!/usr/bin/env bash
# End-to-end checks against the deployed service (no Withings account needed).
# Seeds one fake measurement dated 2000-01-01 and removes it afterwards.
set -uo pipefail
source "$(dirname "$0")/config.sh"

URL="$(service_url)"
API_KEY="$(secret_value api-keys | cut -d, -f1)"
ADMIN_KEY="$(secret_value admin-keys | cut -d, -f1)"
HOOK="$(secret_value webhook-secret)"
PY="${REPO_ROOT}/.venv/bin/python"
failures=0
BODY="$(mktemp)"
BIG=""
trap 'rm -f "$BODY" "$BIG"' EXIT

check() {  # check <name> <expected-status> <curl args...>
  local name="$1" expected="$2"; shift 2
  local status
  status="$(curl -s -o "$BODY" -w '%{http_code}' "$@")"
  if [[ "$status" == "$expected" ]]; then
    printf '  \033[32mPASS\033[0m %-45s %s\n' "$name" "$status"
  else
    printf '  \033[31mFAIL\033[0m %-45s got %s want %s: %s\n' "$name" "$status" "$expected" "$(head -c 300 "$BODY")"
    failures=$((failures + 1))
  fi
}
body_contains() {  # body_contains <name> <text>
  if grep -q -- "$2" "$BODY"; then printf '  \033[32mPASS\033[0m %s\n' "$1"
  else printf '  \033[31mFAIL\033[0m %s: %s\n' "$1" "$(head -c 300 "$BODY")"; failures=$((failures + 1)); fi
}

log "Public / auth checks against ${URL}"
check "health" 200 "$URL/health"
check "latest without key" 401 "$URL/api/weight/latest"
check "latest with wrong key" 401 -H "Authorization: Bearer nope" "$URL/api/weight/latest"
check "auth/start with API key (not admin)" 401 -H "Authorization: Bearer $API_KEY" "$URL/auth/start"
check "internal/sync without OIDC" 401 -X POST "$URL/internal/sync"
check "internal/sync with API key" 401 -X POST -H "Authorization: Bearer $API_KEY" "$URL/internal/sync"
check "webhook wrong secret" 404 -X POST -d userid=1 "$URL/webhooks/withings/wrong"
check "webhook HEAD probe" 200 -I "$URL/webhooks/withings/$HOOK"
check "webhook unknown account ignored" 200 -X POST -d "userid=1&appli=1" "$URL/webhooks/withings/$HOOK"
BIG="$(mktemp)"; head -c 70000 /dev/zero | tr '\0' 'x' >"$BIG"
check "payload too large" 413 -X POST --data-binary "@$BIG" \
  -H "Content-Type: application/x-www-form-urlencoded" "$URL/webhooks/withings/$HOOK"

log "Datastore-backed endpoints"
check "sync status" 200 -H "Authorization: Bearer $API_KEY" "$URL/api/sync/status"
check "auth/start (stores OAuth state)" 200 -H "Authorization: Bearer $ADMIN_KEY" "$URL/auth/start"
body_contains "authorize_url points to Withings" "account.withings.com/oauth2_user/authorize2"
check "callback with forged state" 400 "$URL/auth/callback?code=x&state=forged"

GCLOUD_TOKEN="$(gcloud auth print-access-token)" "$PY" "$REPO_ROOT/scripts/integration_seed.py" seed "$PROJECT_ID"
check "history returns seeded measurement" 200 -H "Authorization: Bearer $API_KEY" \
  "$URL/api/weight/history?from=1999-12-31&to=2000-01-02"
body_contains "seeded value and UTC timestamp" '"measured_at":"2000-01-01T06:00:00Z","weight_kg":12.345'
check "history bad date" 400 -H "Authorization: Bearer $API_KEY" "$URL/api/weight/history?from=garbage"
check "latest weight" 200 -H "Authorization: Bearer $API_KEY" "$URL/api/weight/latest"
check "body composition latest" 200 -H "Authorization: Bearer $API_KEY" "$URL/api/body-composition/latest"
check "body composition history (seeded weigh-in)" 200 -H "Authorization: Bearer $API_KEY" \
  "$URL/api/body-composition/history?from=1999-12-31&to=2000-01-02"
body_contains "weigh-in grouped with metrics" '"metrics":{"weight":{"value":12.345,"unit":"kg"}}'
check "body composition history without key" 401 "$URL/api/body-composition/history"
GCLOUD_TOKEN="$(gcloud auth print-access-token)" "$PY" "$REPO_ROOT/scripts/integration_seed.py" clean "$PROJECT_ID"

log "Async paths (Cloud Tasks + Cloud Scheduler -> /internal/sync with OIDC)"
check "admin sync enqueues Cloud Task" 200 -X POST -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" -d '{}' "$URL/admin/sync"
body_contains "dispatched via queue" '"dispatched":"queued"'
gcloud scheduler jobs run "$SCHEDULER_JOB" --location="$REGION" --quiet
log "Waiting for task + scheduler calls to reach /internal/sync"
found=0
for _ in $(seq 12); do
  sleep 5
  found="$(gcloud logging read \
    "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$SERVICE\" AND httpRequest.requestUrl:\"/internal/sync\" AND httpRequest.status=200 AND timestamp>=\"$(date -u -v-5M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '-5 min' +%Y-%m-%dT%H:%M:%SZ)\"" \
    --limit=10 --format='value(httpRequest.userAgent)' | sort -u | tr '\n' ' ')"
  [[ "$found" == *Tasks* && "$found" == *Scheduler* ]] && break
done
if [[ "$found" == *Tasks* && "$found" == *Scheduler* ]]; then
  printf '  \033[32mPASS\033[0m %s\n' "internal/sync called with 200 by: $found"
else
  printf '  \033[31mFAIL\033[0m %s\n' "internal/sync 200 callers seen: '${found}'"
  failures=$((failures + 1))
fi

if (( failures )); then log "${failures} check(s) failed"; exit 1; fi
log "All integration checks passed"
