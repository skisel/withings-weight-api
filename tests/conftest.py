from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.crypto import TokenCipher
from app.main import create_app
from app.store import MemoryStore
from app.withings import WithingsClient

API_KEY = "test-api-key"
ADMIN_KEY = "test-admin-key"
WEBHOOK_SECRET = "hook-secret"
INVOKER = "invoker@test.iam.gserviceaccount.com"
ACCOUNT = "12345"


class FakeWithings:
    """Scripted Withings backend served through httpx.MockTransport."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.groups: list[dict] = []
        self.subscriptions: list[dict] = []
        self.refresh_counter = 0
        self.refresh_status = 0
        self.expired_tokens: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        path = request.url.path
        self.calls.append((path, form))
        action = form.get("action")
        if path == "/v2/oauth2":
            if form["grant_type"] == "authorization_code":
                return _ok(_token_body("access-0", "refresh-0"))
            if self.refresh_status:
                return httpx.Response(200, json={"status": self.refresh_status, "error": "invalid"})
            self.refresh_counter += 1
            n = self.refresh_counter
            return _ok(_token_body(f"access-{n}", f"refresh-{n}"))
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token in self.expired_tokens:
            return httpx.Response(200, json={"status": 401, "error": "invalid token"})
        if path == "/measure" and action == "getmeas":
            return _ok({"measuregrps": self.groups, "more": 0, "offset": 0})
        if path == "/notify" and action == "list":
            return _ok({"profiles": self.subscriptions})
        if path == "/notify" and action == "subscribe":
            self.subscriptions.append({"callbackurl": form["callbackurl"]})
            return _ok({})
        return httpx.Response(404)

    def calls_for(self, action: str) -> list[dict]:
        return [form for _, form in self.calls if form.get("action") == action]


def _ok(body: dict) -> httpx.Response:
    return httpx.Response(200, json={"status": 0, "body": body})


def _token_body(access: str, refresh: str) -> dict:
    return {
        "userid": int(ACCOUNT),
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": 10800,
        "scope": "user.metrics",
        "token_type": "Bearer",
    }


def weight_group(grpid: int, when: datetime, value: int = 78400, unit: int = -3) -> dict:
    return {
        "grpid": grpid,
        "date": int(when.timestamp()),
        "category": 1,
        "deviceid": "dev",
        "measures": [{"value": value, "type": 1, "unit": unit}],
    }


class RecordingDispatcher:
    def __init__(self):
        self.dispatched: list[tuple[str | None, bool]] = []
        self.run = None

    def dispatch(self, account_id, full=False):
        self.dispatched.append((account_id, full))
        if self.run:
            self.run(account_id, full)
        return "recorded"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        withings_client_id="cid",
        withings_client_secret="csecret",
        token_encryption_key=TokenCipher.generate_key(),
        api_keys=(API_KEY, "old-api-key"),
        admin_keys=(ADMIN_KEY,),
        webhook_secret=WEBHOOK_SECRET,
        public_base_url="https://svc.example",
        tasks_queue="",
        invoker_service_account=INVOKER,
        datastore_database="",
    )


@pytest.fixture
def fake() -> FakeWithings:
    return FakeWithings()


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def withings(fake) -> WithingsClient:
    return WithingsClient("cid", "csecret", http=httpx.Client(transport=httpx.MockTransport(fake.handler)), backoff_seconds=0)


@pytest.fixture
def dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


@pytest.fixture
def client(settings, store, withings, dispatcher) -> TestClient:
    def verify(token: str) -> dict:
        if token == "good-oidc":
            return {"email": INVOKER, "email_verified": True}
        if token == "other-oidc":
            return {"email": "someone@else", "email_verified": True}
        raise ValueError("bad token")

    app = create_app(settings=settings, store=store, client=withings, dispatcher=dispatcher, oidc_verifier=verify)
    return TestClient(app)


def auth(key: str = API_KEY) -> dict:
    return {"Authorization": f"Bearer {key}"}


def authorize(client: TestClient) -> None:
    """Run the OAuth flow end-to-end against the fake."""
    start = client.get("/auth/start", headers=auth(ADMIN_KEY)).json()
    state = parse_qs(start["authorize_url"].split("?", 1)[1])["state"][0]
    response = client.get("/auth/callback", params={"code": "the-code", "state": state})
    assert response.status_code == 200


def now() -> datetime:
    return datetime.now(timezone.utc)


def ago(**kwargs) -> datetime:
    return now() - timedelta(**kwargs)
