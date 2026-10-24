"""Minimal Withings API client (OAuth 2.0, Measure - Getmeas, Notify).

Reference: https://developer.withings.com/api-reference
Withings answers HTTP 200 for most errors and reports them in the JSON
"status" field, so errors are classified from that value.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://account.withings.com/oauth2_user/authorize2"
API_BASE = "https://wbsapi.withings.net"

# Withings "meastype" codes -> (name, unit).
MEASURE_TYPES: dict[int, tuple[str, str]] = {
    1: ("weight", "kg"),
    5: ("fat_free_mass", "kg"),
    6: ("fat_ratio", "%"),
    8: ("fat_mass", "kg"),
    76: ("muscle_mass", "kg"),
    77: ("hydration", "kg"),
    88: ("bone_mass", "kg"),
}
WEIGHT = 1
BODY_COMPOSITION_TYPES = (5, 6, 8, 76, 77, 88)

# Notification "appli" for weight / body composition.
APPLI_WEIGHT = 1

_AUTH_ERROR_STATUSES = {100, 101, 102, 200, 401}
_TRANSIENT_STATUSES = {601, 2554, 2555}


class WithingsError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class WithingsAuthError(WithingsError):
    """Token invalid/expired/revoked: refresh or re-consent is needed."""


class WithingsTransientError(WithingsError):
    """Rate limit, network or server error: safe to retry later."""


@dataclass(frozen=True)
class TokenSet:
    user_id: str
    access_token: str
    refresh_token: str
    expires_at: datetime
    scope: str


@dataclass(frozen=True)
class Measurement:
    account_id: str
    group_id: int
    type: int
    measured_at: datetime
    value: float
    unit: str
    category: int
    device_id: str | None

    @property
    def key(self) -> str:
        return f"{self.account_id}_{self.group_id}_{self.type}"


class WithingsClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        http: httpx.Client | None = None,
        max_attempts: int = 4,
        backoff_seconds: float = 1.0,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or httpx.Client(timeout=20.0)
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds

    # ---- OAuth ----

    def authorize_url(self, redirect_uri: str, state: str, scope: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "scope": scope,
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    def exchange_code(self, code: str, redirect_uri: str) -> TokenSet:
        body = self._call(
            "/v2/oauth2",
            {
                "action": "requesttoken",
                "grant_type": "authorization_code",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        return _token_set(body)

    def refresh(self, refresh_token: str) -> TokenSet:
        body = self._call(
            "/v2/oauth2",
            {
                "action": "requesttoken",
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh_token,
            },
        )
        return _token_set(body)

    # ---- Measures ----

    def get_measures(
        self,
        access_token: str,
        account_id: str,
        *,
        last_update: datetime | None = None,
        meastypes: tuple[int, ...] = tuple(MEASURE_TYPES),
    ) -> list[Measurement]:
        """Fetch real (category=1) measures, following pagination.

        With last_update set, Withings returns groups created or modified since then,
        which also covers late or out-of-order uploads.
        """
        params: dict[str, str] = {
            "action": "getmeas",
            "meastypes": ",".join(str(t) for t in meastypes),
            "category": "1",
        }
        if last_update is not None:
            params["lastupdate"] = str(int(last_update.timestamp()))

        result: list[Measurement] = []
        offset: int | None = None
        while True:
            page_params = dict(params)
            if offset is not None:
                page_params["offset"] = str(offset)
            body = self._call("/measure", page_params, access_token=access_token)
            for group in body.get("measuregrps", []):
                result.extend(_parse_group(group, account_id))
            if body.get("more") and body.get("offset") is not None:
                offset = int(body["offset"])
            else:
                return result

    # ---- Notifications ----

    def list_subscriptions(self, access_token: str, appli: int = APPLI_WEIGHT) -> list[dict]:
        body = self._call(
            "/notify", {"action": "list", "appli": str(appli)}, access_token=access_token
        )
        return body.get("profiles", [])

    def subscribe(self, access_token: str, callback_url: str, appli: int = APPLI_WEIGHT) -> None:
        self._call(
            "/notify",
            {
                "action": "subscribe",
                "callbackurl": callback_url,
                "appli": str(appli),
                "comment": "withings-integration",
            },
            access_token=access_token,
        )

    def ensure_subscription(self, access_token: str, callback_url: str, appli: int = APPLI_WEIGHT) -> bool:
        """Subscribe unless already subscribed. Returns True when a new subscription was made."""
        existing = self.list_subscriptions(access_token, appli)
        if any(p.get("callbackurl") == callback_url for p in existing):
            return False
        self.subscribe(access_token, callback_url, appli)
        return True

    # ---- transport ----

    def _call(self, path: str, data: dict[str, str], access_token: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        action = data.get("action")
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._call_once(path, data, headers)
            except WithingsTransientError as exc:
                if attempt >= self._max_attempts:
                    raise
                delay = self._backoff * (2 ** (attempt - 1))
                log.warning(
                    "Withings transient error, retrying",
                    extra={"fields": {"path": path, "action": action, "status": exc.status, "attempt": attempt}},
                )
                time.sleep(delay)

    def _call_once(self, path: str, data: dict[str, str], headers: dict[str, str]) -> dict:
        try:
            response = self._http.post(f"{API_BASE}{path}", data=data, headers=headers)
        except httpx.HTTPError as exc:
            raise WithingsTransientError(f"HTTP error calling Withings: {type(exc).__name__}") from None
        if response.status_code >= 500 or response.status_code == 429:
            raise WithingsTransientError(f"Withings HTTP {response.status_code}", response.status_code)
        if response.status_code >= 400:
            raise WithingsError(f"Withings HTTP {response.status_code}", response.status_code)
        try:
            payload = response.json()
        except ValueError:
            raise WithingsTransientError("Withings returned non-JSON response") from None

        status = payload.get("status")
        if status == 0:
            return payload.get("body") or {}
        message = f"Withings status {status}: {payload.get('error', '')}".strip()
        if status in _AUTH_ERROR_STATUSES:
            raise WithingsAuthError(message, status)
        if status in _TRANSIENT_STATUSES:
            raise WithingsTransientError(message, status)
        raise WithingsError(message, status)


def _token_set(body: dict) -> TokenSet:
    try:
        return TokenSet(
            user_id=str(body["userid"]),
            access_token=body["access_token"],
            refresh_token=body["refresh_token"],
            expires_at=datetime.fromtimestamp(time.time() + int(body["expires_in"]), tz=timezone.utc),
            scope=body.get("scope", ""),
        )
    except (KeyError, TypeError, ValueError):
        raise WithingsError("Unexpected token response shape") from None


def _parse_group(group: dict, account_id: str) -> list[Measurement]:
    measured_at = datetime.fromtimestamp(int(group["date"]), tz=timezone.utc)
    measurements = []
    for measure in group.get("measures", []):
        mtype = int(measure["type"])
        if mtype not in MEASURE_TYPES:
            continue
        # Real value = value * 10^unit (unit is a power-of-ten exponent).
        value = round(int(measure["value"]) * (10 ** int(measure["unit"])), 6)
        measurements.append(
            Measurement(
                account_id=account_id,
                group_id=int(group["grpid"]),
                type=mtype,
                measured_at=measured_at,
                value=value,
                unit=MEASURE_TYPES[mtype][1],
                category=int(group.get("category", 1)),
                device_id=group.get("deviceid"),
            )
        )
    return measurements
