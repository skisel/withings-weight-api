from datetime import timedelta
from urllib.parse import parse_qs

import pytest

from app.crypto import TokenCipher
from app.store import integration_id
from app.sync import STATUS_REAUTH, ReauthorizationRequired, SyncService
from tests.conftest import (
    ACCOUNT,
    ADMIN_KEY,
    WEBHOOK_SECRET,
    ago,
    auth,
    authorize,
    now,
    weight_group,
)


def run_internal_sync(client):
    def run(account_id, full):
        response = client.post(
            "/internal/sync", json={"account_id": account_id, "full": full}, headers=auth("good-oidc")
        )
        assert response.status_code == 200, response.text

    return run


# ---- personal API auth ----


@pytest.mark.parametrize(
    "path", ["/api/weight/latest", "/api/weight/history", "/api/body-composition/latest", "/api/sync/status"]
)
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic x"}])
def test_api_rejects_missing_or_invalid_key(client, path, headers):
    response = client.get(path, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_api_key_cannot_use_admin_endpoints(client):
    assert client.get("/auth/start", headers=auth()).status_code == 401
    assert client.post("/admin/sync", headers=auth()).status_code == 401


def test_health_is_public(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_rotated_keys_both_accepted(client):
    assert client.get("/api/sync/status", headers=auth("old-api-key")).status_code == 200


# ---- OAuth ----


def test_oauth_flow_stores_encrypted_tokens_subscribes_and_backfills(client, store, fake, dispatcher, settings):
    authorize(client)

    doc = store.integrations[integration_id(ACCOUNT)]
    assert "access-0" not in str(doc) and "refresh-0" not in str(doc)
    cipher = TokenCipher(settings.token_encryption_key)
    assert cipher.decrypt(doc["encrypted_refresh_token"]) == "refresh-0"
    assert doc["subscription_status"] == "subscribed"
    assert fake.calls_for("subscribe")[0]["callbackurl"] == f"https://svc.example/webhooks/withings/{WEBHOOK_SECRET}"
    assert dispatcher.dispatched == [(ACCOUNT, True)]
    exchange = fake.calls_for("requesttoken")[0]
    assert exchange["redirect_uri"] == "https://svc.example/auth/callback"


def test_oauth_state_is_single_use(client):
    start = client.get("/auth/start", headers=auth(ADMIN_KEY)).json()
    state = parse_qs(start["authorize_url"].split("?", 1)[1])["state"][0]
    assert client.get("/auth/callback", params={"code": "c", "state": state}).status_code == 200
    assert client.get("/auth/callback", params={"code": "c", "state": state}).status_code == 400


def test_oauth_rejects_unknown_or_expired_state(client, store):
    assert client.get("/auth/callback", params={"code": "c", "state": "forged"}).status_code == 400
    store.create_oauth_state("old", ago(seconds=1))
    assert client.get("/auth/callback", params={"code": "c", "state": "old"}).status_code == 400


def test_existing_subscription_is_not_duplicated(client, fake):
    fake.subscriptions.append({"callbackurl": f"https://svc.example/webhooks/withings/{WEBHOOK_SECRET}"})
    authorize(client)
    assert fake.calls_for("subscribe") == []


# ---- webhook + sync ----


def test_webhook_rejects_wrong_secret(client):
    assert client.post("/webhooks/withings/nope", data={"userid": ACCOUNT}).status_code == 404
    assert client.head("/webhooks/withings/nope").status_code == 404


def test_webhook_verification_probe(client):
    assert client.head(f"/webhooks/withings/{WEBHOOK_SECRET}").status_code == 200
    assert client.get(f"/webhooks/withings/{WEBHOOK_SECRET}").status_code == 200


def test_webhook_ignores_unknown_account(client, dispatcher):
    response = client.post(f"/webhooks/withings/{WEBHOOK_SECRET}", data={"userid": "999", "appli": "1"})
    assert response.status_code == 200
    assert dispatcher.dispatched == []


def test_duplicate_notifications_do_not_duplicate_measurements(client, fake, store, dispatcher):
    authorize(client)
    dispatcher.run = run_internal_sync(client)
    fake.groups = [weight_group(1, ago(hours=1), 78400), weight_group(2, ago(days=1), 79100)]

    for _ in range(3):
        response = client.post(f"/webhooks/withings/{WEBHOOK_SECRET}", data={"userid": ACCOUNT, "appli": "1"})
        assert response.status_code == 200

    assert len(store.measurements) == 2
    latest = client.get("/api/weight/latest", headers=auth()).json()
    assert latest["weight_kg"] == 78.4
    assert latest["source"] == "withings"
    assert latest["measured_at"].endswith("Z")


def test_incremental_sync_uses_overlapping_lastupdate(client, fake, store, dispatcher):
    authorize(client)
    dispatcher.run = run_internal_sync(client)
    client.post(f"/webhooks/withings/{WEBHOOK_SECRET}", data={"userid": ACCOUNT})
    checkpoint = store.integrations[integration_id(ACCOUNT)]["last_successful_sync_at"]

    client.post(f"/webhooks/withings/{WEBHOOK_SECRET}", data={"userid": ACCOUNT})

    getmeas = fake.calls_for("getmeas")
    assert "lastupdate" not in getmeas[0]
    assert int(getmeas[-1]["lastupdate"]) < checkpoint.timestamp()


def test_weight_history_filters_and_sorts(client, fake, dispatcher):
    authorize(client)
    dispatcher.run = run_internal_sync(client)
    fake.groups = [weight_group(i, ago(days=i), 80000 + i * 100) for i in range(1, 6)]
    client.post("/admin/sync", headers=auth(ADMIN_KEY), json={"full": True})

    frm = ago(days=3, hours=12).isoformat()
    body = client.get("/api/weight/history", params={"from": frm}, headers=auth()).json()
    assert [item["weight_kg"] for item in body["items"]] == [80.3, 80.2, 80.1]
    assert client.get("/api/weight/history", params={"from": "garbage"}, headers=auth()).status_code == 400


def test_sync_status_exposes_no_tokens(client):
    authorize(client)
    body = client.get("/api/sync/status", headers=auth()).text
    assert "access" not in body.replace("last_sync", "") and "refresh" not in body


def test_internal_sync_requires_invoker_identity(client):
    assert client.post("/internal/sync").status_code == 401
    assert client.post("/internal/sync", headers=auth("garbage")).status_code == 401
    assert client.post("/internal/sync", headers=auth("other-oidc")).status_code == 403


def test_payload_size_limit(client):
    response = client.post(
        f"/webhooks/withings/{WEBHOOK_SECRET}", content=b"x" * (70 * 1024),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 413


# ---- token refresh ----


def _service(store, withings, settings, **kwargs) -> SyncService:
    return SyncService(store, withings, TokenCipher(settings.token_encryption_key), **kwargs)


def _expire(store):
    store.integrations[integration_id(ACCOUNT)]["access_token_expires_at"] = ago(minutes=1)


def test_expired_token_is_refreshed_and_rotation_persisted(client, store, withings, settings, fake):
    authorize(client)
    _expire(store)
    service = _service(store, withings, settings)

    assert service.access_token(ACCOUNT) == "access-1"
    doc = store.integrations[integration_id(ACCOUNT)]
    assert TokenCipher(settings.token_encryption_key).decrypt(doc["encrypted_refresh_token"]) == "refresh-1"
    assert doc["refresh_lock_until"] is None
    # The next refresh must use the rotated refresh token.
    _expire(store)
    service.access_token(ACCOUNT)
    refreshes = [c for c in fake.calls_for("requesttoken") if c["grant_type"] == "refresh_token"]
    assert [c["refresh_token"] for c in refreshes] == ["refresh-0", "refresh-1"]


def test_401_from_api_forces_refresh_and_retries(client, store, withings, settings, fake):
    authorize(client)
    fake.expired_tokens.add("access-0")
    fake.groups = [weight_group(1, ago(hours=1))]
    assert _service(store, withings, settings).sync_account(ACCOUNT) == 1
    assert fake.refresh_counter == 1


def test_rejected_refresh_marks_reauthorization_required(client, store, withings, settings, fake):
    authorize(client)
    _expire(store)
    fake.refresh_status = 503
    service = _service(store, withings, settings)
    with pytest.raises(ReauthorizationRequired):
        service.access_token(ACCOUNT)
    assert store.integrations[integration_id(ACCOUNT)]["sync_status"] == STATUS_REAUTH
    status = client.get("/api/sync/status", headers=auth()).json()
    assert status["accounts"][0]["sync_status"] == STATUS_REAUTH


def test_concurrent_refresh_waits_for_lock_holder(client, store, withings, settings, fake):
    authorize(client)
    _expire(store)
    store.integrations[integration_id(ACCOUNT)]["refresh_lock_until"] = now() + timedelta(seconds=30)
    cipher = TokenCipher(settings.token_encryption_key)

    def other_instance_finishes(_seconds):
        store.update_integration(
            ACCOUNT,
            {
                "encrypted_access_token": cipher.encrypt("access-other"),
                "access_token_expires_at": now() + timedelta(hours=3),
                "refresh_lock_until": None,
            },
        )

    service = _service(store, withings, settings, sleep=other_instance_finishes)
    assert service.access_token(ACCOUNT) == "access-other"
    assert fake.refresh_counter == 0


def test_callback_probe_without_params_is_ok(client):
    assert client.get("/auth/callback").status_code == 200
    assert client.get("/auth/callback", params={"error": "access_denied"}).status_code == 400
