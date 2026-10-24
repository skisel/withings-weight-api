"""HTTP surface of the service.

Public (no key):   GET /health, GET /auth/callback, HEAD|GET|POST /webhooks/withings/{secret}
Personal API key:  GET /api/weight/latest, /api/weight/history, /api/body-composition/latest, /api/sync/status
Admin key:         GET /auth/start, POST /admin/sync, POST /admin/subscribe
Google OIDC:       POST /internal/sync  (Cloud Tasks / Cloud Scheduler)
"""

import hmac
import logging
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.crypto import TokenCipher
from app.logging_setup import configure_logging
from app.store import DatastoreStore, Store
from app.sync import STATUS_REAUTH, ReauthorizationRequired, SyncService
from app.tasks import SyncDispatcher
from app.withings import BODY_COMPOSITION_TYPES, MEASURE_TYPES, WEIGHT, WithingsClient, WithingsError

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
OAUTH_STATE_TTL = timedelta(minutes=10)
MAX_HISTORY_LIMIT = 5000


class SyncRequest(BaseModel):
    account_id: str | None = None
    full: bool = False


def _constant_time_in(candidate: str, allowed: tuple[str, ...] | list[str]) -> bool:
    match = False
    for key in allowed:
        match |= hmac.compare_digest(candidate.encode(), key.encode())
    return match


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"})


def _google_oidc_verifier(audience: str) -> Callable[[str], dict]:
    def verify(token: str) -> dict:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        return id_token.verify_oauth2_token(token, google_requests.Request(), audience=audience)

    return verify


def _parse_instant(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid '{name}': use ISO 8601 date or datetime") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _weight_view(doc: dict) -> dict:
    return {"measured_at": _iso(doc["measured_at"]), "weight_kg": doc["value"], "source": doc.get("source", "withings")}


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    client: WithingsClient | None = None,
    dispatcher: SyncDispatcher | None = None,
    oidc_verifier: Callable[[str], dict] | None = None,
) -> FastAPI:
    if settings is None:
        configure_logging()
        settings = get_settings()
    store = store or DatastoreStore(database=settings.datastore_database)
    client = client or WithingsClient(settings.withings_client_id, settings.withings_client_secret)
    sync = SyncService(store, client, TokenCipher(settings.token_encryption_key))

    def run_sync(account_id: str | None, full: bool) -> object:
        return sync.sync_account(account_id, full=full) if account_id else sync.sync_all(full=full)

    dispatcher = dispatcher or SyncDispatcher(settings, run_sync)
    oidc_verifier = oidc_verifier or _google_oidc_verifier(settings.public_base_url)

    app = FastAPI(title="withings-integration", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def limit_body_size(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "Payload too large"}, status_code=413)
        return await call_next(request)

    def require_api_key(request: Request) -> None:
        token = _bearer_token(request)
        if not token or not _constant_time_in(token, settings.api_keys):
            raise _unauthorized()

    def require_admin_key(request: Request) -> None:
        token = _bearer_token(request)
        if not token or not _constant_time_in(token, settings.admin_keys):
            raise _unauthorized()

    def require_internal_caller(request: Request) -> None:
        token = _bearer_token(request)
        if not token:
            raise _unauthorized()
        try:
            claims = oidc_verifier(token)
        except Exception:
            raise _unauthorized() from None
        if not claims.get("email_verified") or claims.get("email") != settings.invoker_service_account:
            raise HTTPException(status_code=403, detail="Forbidden")

    # ---- public ----

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/auth/start", dependencies=[Depends(require_admin_key)])
    def auth_start() -> dict:
        state = secrets.token_urlsafe(32)
        store.create_oauth_state(state, datetime.now(timezone.utc) + OAUTH_STATE_TTL)
        url = client.authorize_url(settings.redirect_uri, state, settings.withings_scope)
        return {"authorize_url": url, "expires_in_seconds": int(OAUTH_STATE_TTL.total_seconds())}

    @app.api_route("/auth/callback", methods=["GET", "HEAD"], response_class=HTMLResponse)
    def auth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
        if not (code or state or error):
            # Reachability probe (e.g. Withings dashboard validating the callback URL).
            return HTMLResponse("ok")
        if error or not code or not state:
            raise HTTPException(status_code=400, detail="Authorization was not granted")
        if not store.consume_oauth_state(state, datetime.now(timezone.utc)):
            raise HTTPException(status_code=400, detail="Invalid or expired state")
        try:
            tokens = client.exchange_code(code, settings.redirect_uri)
        except WithingsError as exc:
            log.error("Withings code exchange failed", extra={"fields": {"status": exc.status}})
            raise HTTPException(status_code=502, detail="Token exchange with Withings failed") from None

        sync.save_authorization(tokens)
        log.info("Withings authorization stored", extra={"fields": {"account_id": tokens.user_id}})
        _ensure_subscription(tokens.user_id, tokens.access_token)
        try:
            dispatcher.dispatch(tokens.user_id, full=True)
        except Exception:
            log.exception("Could not start initial backfill", extra={"fields": {"account_id": tokens.user_id}})
        return HTMLResponse("<!doctype html><p>Withings authorization completed. You can close this window.</p>")

    def _ensure_subscription(account_id: str, access_token: str) -> str:
        try:
            created = client.ensure_subscription(access_token, settings.webhook_url)
            status = "subscribed" if created else "already_subscribed"
        except WithingsError as exc:
            log.error("Withings notify subscription failed", extra={"fields": {"account_id": account_id, "status": exc.status}})
            status = "error"
        store.update_integration(account_id, {"subscription_status": status})
        return status

    @app.api_route("/webhooks/withings/{secret}", methods=["GET", "HEAD", "POST"])
    async def withings_webhook(secret: str, request: Request):
        if not _constant_time_in(secret, (settings.webhook_secret,)):
            return Response(status_code=404)
        if request.method != "POST":
            # Withings probes the callback URL when subscribing.
            return Response(status_code=200)

        form = await request.form()
        account_id = str(form.get("userid", "")).strip()
        appli = str(form.get("appli", "")).strip()
        integration = await run_in_threadpool(store.get_integration, account_id) if account_id else None
        if integration is None:
            log.warning("Ignoring Withings notification for unknown account", extra={"fields": {"appli": appli}})
            return Response(status_code=200)
        try:
            await run_in_threadpool(dispatcher.dispatch, account_id, False)
        except Exception:
            # Non-2xx makes Withings retry the notification later.
            log.exception("Could not dispatch sync for notification", extra={"fields": {"account_id": account_id}})
            return Response(status_code=503)
        log.info("Withings notification accepted", extra={"fields": {"account_id": account_id, "appli": appli}})
        return Response(status_code=200)

    # ---- internal (Cloud Tasks / Scheduler) ----

    @app.post("/internal/sync", dependencies=[Depends(require_internal_caller)])
    def internal_sync(body: SyncRequest | None = None):
        body = body or SyncRequest()
        if body.account_id:
            try:
                return {"account_id": body.account_id, "upserted": sync.sync_account(body.account_id, full=body.full)}
            except ReauthorizationRequired:
                # Retrying will not help until the user re-consents.
                return {"account_id": body.account_id, "status": STATUS_REAUTH}
        results = sync.sync_all(full=body.full)
        failed = any(r.startswith("error") for r in results.values())
        return JSONResponse({"results": results}, status_code=500 if failed else 200)

    # ---- admin ----

    @app.post("/admin/sync", dependencies=[Depends(require_admin_key)])
    def admin_sync(body: SyncRequest | None = None) -> dict:
        body = body or SyncRequest()
        return {"dispatched": dispatcher.dispatch(body.account_id, body.full)}

    @app.post("/admin/subscribe", dependencies=[Depends(require_admin_key)])
    def admin_subscribe() -> dict:
        results = {}
        for integration in store.list_integrations():
            account_id = integration["account_id"]
            try:
                results[account_id] = _ensure_subscription(account_id, sync.access_token(account_id))
            except ReauthorizationRequired:
                results[account_id] = STATUS_REAUTH
        return {"results": results}

    # ---- personal read API ----

    @app.get("/api/weight/latest", dependencies=[Depends(require_api_key)])
    def weight_latest() -> dict:
        doc = store.latest_measurement(WEIGHT)
        if doc is None:
            raise HTTPException(status_code=404, detail="No weight measurements yet")
        return _weight_view(doc)

    @app.get("/api/weight/history", dependencies=[Depends(require_api_key)])
    def weight_history(
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        limit: int = Query(default=1000, ge=1, le=MAX_HISTORY_LIMIT),
    ) -> dict:
        start, end = _parse_instant(from_, "from"), _parse_instant(to, "to")
        docs = store.measurements_between(WEIGHT, start, end, limit)
        items = [_weight_view(d) for d in reversed(docs)]  # oldest first
        return {"count": len(items), "items": items}

    @app.get("/api/body-composition/latest", dependencies=[Depends(require_api_key)])
    def body_composition_latest() -> dict:
        result = {}
        for mtype in (WEIGHT, *BODY_COMPOSITION_TYPES):
            doc = store.latest_measurement(mtype)
            if doc is not None:
                name, unit = MEASURE_TYPES[mtype]
                result[name] = {"value": doc["value"], "unit": unit, "measured_at": _iso(doc["measured_at"])}
        return {"source": "withings", "metrics": result}

    @app.get("/api/sync/status", dependencies=[Depends(require_api_key)])
    def sync_status() -> dict:
        fields = (
            "sync_status",
            "subscription_status",
            "last_successful_sync_at",
            "last_sync_attempt_at",
            "last_sync_count",
            "last_sync_error",
        )
        accounts = []
        for integration in store.list_integrations():
            view = {"account_id": integration["account_id"]}
            for field in fields:
                value = integration.get(field)
                view[field] = _iso(value) if isinstance(value, datetime) else value
            accounts.append(view)
        return {"accounts": accounts}

    return app
