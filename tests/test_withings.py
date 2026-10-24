import httpx
import pytest

from app.withings import WithingsClient, WithingsTransientError, _parse_group


def test_parse_group_applies_unit_exponent_and_skips_unknown_types():
    group = {
        "grpid": 7,
        "date": 1_700_000_000,
        "category": 1,
        "measures": [
            {"value": 78400, "type": 1, "unit": -3},
            {"value": 2153, "type": 6, "unit": -2},
            {"value": 120, "type": 9, "unit": 0},  # diastolic BP, not stored
        ],
    }
    parsed = _parse_group(group, "acc")
    assert [(m.type, m.value, m.unit) for m in parsed] == [(1, 78.4, "kg"), (6, 21.53, "%")]
    assert parsed[0].key == "acc_7_1"
    assert parsed[0].measured_at.tzinfo is not None


def test_get_measures_follows_pagination():
    pages = iter(
        [
            {"status": 0, "body": {"measuregrps": [{"grpid": 1, "date": 1, "measures": [{"value": 1, "type": 1, "unit": 0}]}], "more": 1, "offset": 1}},
            {"status": 0, "body": {"measuregrps": [{"grpid": 2, "date": 2, "measures": [{"value": 2, "type": 1, "unit": 0}]}], "more": 0}},
        ]
    )
    client = WithingsClient("c", "s", http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=next(pages)))))
    assert [m.group_id for m in client.get_measures("tok", "acc")] == [1, 2]


def test_transient_errors_are_retried_then_raised():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(200, json={"status": 601, "error": "too many requests"})

    client = WithingsClient("c", "s", http=httpx.Client(transport=httpx.MockTransport(handler)), max_attempts=3, backoff_seconds=0)
    with pytest.raises(WithingsTransientError):
        client.get_measures("tok", "acc")
    assert len(attempts) == 3
