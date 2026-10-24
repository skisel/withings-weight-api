# withings-integration

Personal backend that syncs Withings weight / body-composition measurements into the project's default
Firestore-in-Datastore-mode database
and exposes them through a small read-only JSON API protected by a static bearer token.

Runs on Google Cloud (your own project, region `europe-west6`):
Cloud Run (scale to zero) + Firestore in Datastore mode (`(default)` database, free tier) + Secret Manager + Cloud Tasks + one Cloud Scheduler job.

```
Withings ──notify──▶ /webhooks/withings/<secret> ──Cloud Tasks──▶ /internal/sync ──getmeas──▶ Withings
                                                                       │
Cloud Scheduler (daily) ──────────────────────────────────────────────▶│ upsert
                                                                       ▼
your scripts ──Bearer API key──▶ /api/weight/latest ◀──────────── Datastore
```

## API

All `/api/*` endpoints require `Authorization: Bearer <API_KEY>`.

| Endpoint | Result |
| --- | --- |
| `GET /health` | `{"status":"ok"}` (public) |
| `GET /api/weight/latest` | `{"measured_at":"2026-10-24T06:35:00Z","weight_kg":78.4,"source":"withings"}` |
| `GET /api/weight/history?from=2026-01-01&to=2026-02-01&limit=1000` | `{"count":N,"items":[...]}`, oldest first; `from` inclusive, `to` exclusive, ISO 8601 (UTC if no offset) |
| `GET /api/body-composition/latest` | Latest value per metric (weight, fat ratio, fat mass, fat free mass, muscle, hydration, bone) |
| `GET /api/body-composition/history?from=&to=&limit=&composition_only=` | One entry per weigh-in, oldest first: `{"measured_at", "source", "metrics": {"weight": {"value","unit"}, "fat_ratio": {...}, ...}}`. Weigh-ins without body composition (e.g. not barefoot) contain only `weight`; `composition_only=true` skips them |
| `GET /api/sync/status` | Sync / authorization / subscription status per account |

```bash
curl -H "Authorization: Bearer $API_KEY" "$SERVICE_URL/api/weight/latest"
```

Admin endpoints (`Authorization: Bearer <ADMIN_KEY>`): `GET /auth/start`, `POST /admin/sync`
(`{"full": true}` re-imports all history), `POST /admin/subscribe` (re-registers the Withings webhook).

## First-time deployment

Prerequisites: `gcloud` logged in with an account that owns the project, `python3`, `curl`.
Billing must be enabled on the project.

Scripts always target `PROJECT_ID` via `CLOUDSDK_CORE_PROJECT`, so your
global gcloud config is left alone. Configure the project (and optionally account, billing, region) in an
untracked `deploy.local.env`:

```bash
PROJECT_ID=your-gcp-project-id            # required
GCLOUD_ACCOUNT=you@gmail.com              # optional
BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX   # optional: creates a 5 USD/month budget alert
```

1. **Bootstrap GCP** (idempotent, safe to re-run):
   ```bash
   scripts/setup.sh
   ```
   Enables APIs, creates the Datastore index (`index.yaml`) and TTL policy, Artifact Registry, service accounts, IAM, the Cloud Tasks queue,
   and generates the token-encryption key, API key, admin key and webhook secret in Secret Manager.
   It prints the service URL and the OAuth callback URL.

2. **Create a Withings developer app** at https://developer.withings.com/dashboard/ with callback URL
   `https://withings-<project-number>.europe-west6.run.app/auth/callback` (printed by setup), then:
   ```bash
   scripts/set-withings-credentials.sh
   ```

3. **Deploy**:
   ```bash
   scripts/deploy.sh
   ```

4. **Authorize once** (opens the Withings consent page in your browser):
   ```bash
   scripts/authorize.sh
   ```
   On success the service stores encrypted tokens, subscribes to weight notifications and queues a full
   history import.

5. **Get your bearer token and try it**:
   ```bash
   scripts/show-keys.sh
   scripts/api.sh /api/sync/status
   scripts/api.sh /api/weight/latest
   ```

## Updating

```bash
git commit ...         # image tag = git short sha (adds -dirty-<timestamp> for uncommitted changes)
scripts/deploy.sh
```

Other helpers:

| Script | Purpose |
| --- | --- |
| `scripts/integration-test.sh` | End-to-end checks against the deployed service (auth, Datastore, webhook, Cloud Tasks, Scheduler) |
| `scripts/sync.sh [--full]` | Trigger a sync now (`--full` re-imports the entire history) |
| `scripts/rotate-key.sh api\|admin [--finalize]` | Add a new key (old stays valid), then drop old keys |
| `scripts/set-withings-credentials.sh` | Replace Withings client id/secret |
| `scripts/authorize.sh` | Re-run consent, e.g. when `/api/sync/status` says `reauthorization_required` |

## How it works

- **Tokens**: access/refresh tokens are Fernet-encrypted (key in Secret Manager) before being stored in
  `integrations/withings_<userid>`. Access tokens are refreshed 10 minutes before expiry under a
  Datastore-transaction lock; the rotated refresh token is persisted immediately. A rejected refresh marks
  the integration `reauthorization_required`.
- **Webhook**: Withings notifications are only triggers. The handler checks the secret in the URL path and
  the known `userid`, enqueues a Cloud Task and returns 200 immediately (Cloud Run throttles CPU after the
  response, so no work happens after replying).
- **Sync**: `getmeas` with `lastupdate = last checkpoint - 6h` (overlap catches delayed uploads).
  Measurements are upserted under `measurements/<userid>_<grpid>_<type>`, so duplicate or out-of-order
  notifications never create duplicates.
- **Recovery**: Cloud Scheduler calls `/internal/sync` daily (04:30 Europe/Zurich); Cloud Tasks retries
  failed syncs with backoff. `/internal/*` accepts only Google OIDC tokens of the `withings-invoker` SA.
- **Keys**: `API_KEYS` / `ADMIN_KEYS` are comma-separated lists so keys can be rotated without downtime;
  compared in constant time. Secrets are mounted from Secret Manager at instance start.

## Cost notes

The project's `(default)` database is in Datastore mode (location `nam5`), and the Firestore free tier
applies only to the `(default)` database, so data is stored there instead of in a separate Native database.

Designed to stay inside free tiers at personal volume: Cloud Run min-instances 0 / max 2,
6 secrets with a single active version each, Artifact Registry cleanup keeps 3 images, one scheduler job.
Budget alerts are notifications only, not caps. Check current pricing before relying on $0.

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Known limitations / next steps

- Measurements deleted in Withings are not deleted here (re-run a full sync after manual cleanup in Datastore).
- Cloud Run request logs include the webhook URL path (with its secret); logs stay inside the project.
- No per-IP rate limiting beyond Cloud Run's max-instances bound.
- Data deletion: remove the Datastore `measurements` / `integrations` kinds and revoke the app in
  your Withings account settings.
