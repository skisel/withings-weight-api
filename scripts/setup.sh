#!/usr/bin/env bash
# One-time (idempotent) GCP bootstrap: APIs, Datastore index/TTL, Artifact Registry, service accounts,
# IAM, secrets, Cloud Tasks queue, optional budget. Safe to re-run.
set -euo pipefail
source "$(dirname "$0")/config.sh"

log "Project: $PROJECT_ID  Region: $REGION  Service: $SERVICE"
PROJECT_NUMBER="$(project_number)"

log "Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  firestore.googleapis.com \
  datastore.googleapis.com \
  secretmanager.googleapis.com \
  cloudtasks.googleapis.com \
  cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  iam.googleapis.com

log "Artifact Registry repository"
if ! gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$AR_REPO" --repository-format=docker --location="$REGION" \
    --description="withings-integration images"
fi
# Keep only the most recent images to stay within the free storage allowance.
CLEANUP_POLICY="$(mktemp)"
cat >"$CLEANUP_POLICY" <<'JSON'
[
  {"name": "keep-recent", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 3}},
  {"name": "delete-old", "action": {"type": "Delete"}, "condition": {"olderThan": "1d"}}
]
JSON
gcloud artifacts repositories set-cleanup-policies "$AR_REPO" --location="$REGION" \
  --policy="$CLEANUP_POLICY" --no-dry-run --quiet >/dev/null
rm -f "$CLEANUP_POLICY"

log "Datastore-mode database \"(default)\" (Firestore free tier applies to it)"
if ! gcloud firestore databases describe --database='(default)' >/dev/null 2>&1; then
  gcloud firestore databases create --database='(default)' --location="$REGION" --type=datastore-mode
fi
gcloud datastore indexes create "$REPO_ROOT/index.yaml" --quiet
# Expired OAuth state entities are removed automatically.
gcloud firestore fields ttls update expires_at --database='(default)' --collection-group=oauth_states \
  --enable-ttl --async --quiet >/dev/null 2>&1 || warn "Could not set TTL on oauth_states.expires_at (may already be set)"

log "Service accounts"
for sa in "$RUN_SA_NAME" "$INVOKER_SA_NAME"; do
  if ! gcloud iam service-accounts describe "${sa}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$sa" --display-name="withings-integration ${sa}"
  fi
done

log "Secrets"
ensure_generated_secret() {
  local name="$1" generator="$2"
  if ! secret_exists "$name" || ! secret_has_version "$name"; then
    put_secret "$name" "$($generator)"
    log "  created $name"
  fi
}
ensure_generated_secret token-encryption-key fernet_key
ensure_generated_secret api-keys random_key
ensure_generated_secret admin-keys random_key
ensure_generated_secret webhook-secret random_key
for name in withings-client-id withings-client-secret; do
  if ! secret_exists "$name"; then
    gcloud secrets create "$name" --replication-policy=automatic >/dev/null
  fi
done

log "IAM bindings"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUN_SA}" --role=roles/datastore.user --condition=None --quiet >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUN_SA}" --role=roles/cloudtasks.enqueuer --condition=None --quiet >/dev/null
for name in "${ALL_SECRETS[@]}"; do
  gcloud secrets add-iam-policy-binding "$name" \
    --member="serviceAccount:${RUN_SA}" --role=roles/secretmanager.secretAccessor --quiet >/dev/null
done
# The runtime SA creates Cloud Tasks that carry OIDC tokens minted for the invoker SA.
gcloud iam service-accounts add-iam-policy-binding "$INVOKER_SA" \
  --member="serviceAccount:${RUN_SA}" --role=roles/iam.serviceAccountUser --quiet >/dev/null
# Cloud Build (gcloud builds submit) runs as the default compute SA in new projects.
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
for role in roles/artifactregistry.writer roles/logging.logWriter roles/storage.objectViewer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${COMPUTE_SA}" --role="$role" --condition=None --quiet >/dev/null
done

log "Cloud Tasks queue"
if ! gcloud tasks queues describe "$TASKS_QUEUE_NAME" --location="$REGION" >/dev/null 2>&1; then
  gcloud tasks queues create "$TASKS_QUEUE_NAME" --location="$REGION"
fi
gcloud tasks queues update "$TASKS_QUEUE_NAME" --location="$REGION" \
  --max-attempts=10 --min-backoff=30s --max-backoff=3600s --max-concurrent-dispatches=1 \
  --max-dispatches-per-second=1 >/dev/null

if [[ -n "$BILLING_ACCOUNT" ]]; then
  log "Budget alert (${BUDGET_AMOUNT}/month)"
  gcloud services enable billingbudgets.googleapis.com
  if ! gcloud billing budgets list --billing-account="$BILLING_ACCOUNT" --format='value(displayName)' \
      | grep -qx "withings-integration"; then
    gcloud billing budgets create --billing-account="$BILLING_ACCOUNT" \
      --display-name="withings-integration" --budget-amount="$BUDGET_AMOUNT" \
      --filter-projects="projects/${PROJECT_ID}" \
      --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
  fi
else
  warn "BILLING_ACCOUNT not set: skipping budget alert. Set one up (strongly recommended)."
fi

if ! secret_has_version withings-client-id || ! secret_has_version withings-client-secret; then
  warn "Withings credentials not set yet. Run: scripts/set-withings-credentials.sh"
fi

cat >&2 <<MSG

Setup complete.
  Service URL (after deploy): $(service_url)
  Withings callback URL to register in the Withings developer dashboard:
      $(service_url)/auth/callback

Next steps:
  1. scripts/set-withings-credentials.sh
  2. scripts/deploy.sh
  3. scripts/authorize.sh
  4. scripts/show-keys.sh   (prints your personal API bearer token)
MSG
