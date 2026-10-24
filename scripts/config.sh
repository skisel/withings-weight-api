# Shared configuration for all scripts. Sourced, not executed.
# Override any value via environment or an untracked deploy.local.env at the repo root.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$REPO_ROOT/deploy.local.env" ]]; then
  # shellcheck disable=SC1091
  source "$REPO_ROOT/deploy.local.env"
fi

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID in deploy.local.env (see README)}"
REGION="${REGION:-europe-west6}"
SERVICE="${SERVICE:-withings}"
AR_REPO="${AR_REPO:-withings}"
TASKS_QUEUE_NAME="${TASKS_QUEUE_NAME:-withings-sync}"
SCHEDULER_JOB="${SCHEDULER_JOB:-withings-daily-reconcile}"
# Daily reconciliation (recovers missed notifications). Cron in SCHEDULER_TZ.
SCHEDULER_CRON="${SCHEDULER_CRON:-30 4 * * *}"
SCHEDULER_TZ="${SCHEDULER_TZ:-Europe/Zurich}"
RUN_SA_NAME="${RUN_SA_NAME:-withings-run}"
INVOKER_SA_NAME="${INVOKER_SA_NAME:-withings-invoker}"
# Optional: billing account id (XXXXXX-XXXXXX-XXXXXX) to create a budget alert in setup.sh.
BILLING_ACCOUNT="${BILLING_ACCOUNT:-}"
BUDGET_AMOUNT="${BUDGET_AMOUNT:-5USD}"

# Pin gcloud to this project (and optionally an account) without touching the global gcloud config.
export CLOUDSDK_CORE_PROJECT="$PROJECT_ID"
if [[ -n "${GCLOUD_ACCOUNT:-}" ]]; then
  export CLOUDSDK_CORE_ACCOUNT="$GCLOUD_ACCOUNT"
fi

RUN_SA="${RUN_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
INVOKER_SA="${INVOKER_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE}"
TASKS_QUEUE_PATH="projects/${PROJECT_ID}/locations/${REGION}/queues/${TASKS_QUEUE_NAME}"

# Secret Manager secret names -> env vars in the container.
SECRET_ENV_MAPPING="WITHINGS_CLIENT_ID=withings-client-id:latest,WITHINGS_CLIENT_SECRET=withings-client-secret:latest,TOKEN_ENCRYPTION_KEY=token-encryption-key:latest,API_KEYS=api-keys:latest,ADMIN_KEYS=admin-keys:latest,WEBHOOK_SECRET=webhook-secret:latest"
ALL_SECRETS=(withings-client-id withings-client-secret token-encryption-key api-keys admin-keys webhook-secret)

log() { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }

project_number() {
  gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)'
}

# Deterministic Cloud Run URL, known before the first deploy.
service_url() {
  echo "https://${SERVICE}-$(project_number).${REGION}.run.app"
}

random_key() {
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
}

fernet_key() {
  python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
}

secret_exists() {
  gcloud secrets describe "$1" >/dev/null 2>&1
}

secret_has_version() {
  [[ -n "$(gcloud secrets versions list "$1" --filter='state=ENABLED' --limit=1 --format='value(name)' 2>/dev/null)" ]]
}

secret_value() {
  gcloud secrets versions access latest --secret="$1"
}

# Adds a new version and disables older enabled ones (keeps Secret Manager within the free active-version quota).
put_secret() {
  local name="$1" value="$2"
  if ! secret_exists "$name"; then
    gcloud secrets create "$name" --replication-policy=automatic >/dev/null
  fi
  local new_version
  new_version="$(printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- --format='value(name)')"
  new_version="${new_version##*/}"
  local v
  for v in $(gcloud secrets versions list "$name" --filter='state=ENABLED' --format='value(name)'); do
    v="${v##*/}"
    if [[ "$v" != "$new_version" ]]; then
      gcloud secrets versions disable "$v" --secret="$name" --quiet >/dev/null
    fi
  done
}

# Forces a new Cloud Run revision so ":latest" secrets are re-resolved (no rebuild).
roll_service() {
  log "Rolling Cloud Run service to pick up secret changes"
  gcloud run services update "$SERVICE" --region="$REGION" \
    --update-labels="rolled-at=$(date -u +%Y%m%d-%H%M%S)" --quiet >/dev/null
}

admin_curl() {
  local admin_key
  admin_key="$(secret_value admin-keys | cut -d, -f1)"
  curl -fsS -H "Authorization: Bearer ${admin_key}" "$@"
}
