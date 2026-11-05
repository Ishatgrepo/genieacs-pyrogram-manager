from datetime import timedelta
import pytest
from app.domain import classify, flatten, now, rate, stamp, validate_value
from conftest import sample


def test_redaction():
    params = flatten(sample())
    assert next(p for p in params if p["path"].endswith("Password"))["value"] == "[redacted]"
    assert "never-store-me" not in str(params)


def test_status_boundaries():
    t = now()
    assert classify(None) == "UNKNOWN"
    assert classify(t, at=t) == "ACTIVE"
    assert classify(t - timedelta(seconds=601), at=t) == "STALE"
    assert classify(t - timedelta(seconds=1801), at=t) == "OFFLINE"
    assert classify(t + timedelta(hours=1), at=t) == "UNKNOWN"


def test_timezone_preserved():
    assert stamp("2026-01-01T05:30:00+05:30").hour == 0


def test_traffic():
    t = now()
    a = {"value": 100, "sample_at": t}
    b = {"value": 200, "sample_at": t + timedelta(seconds=10)}
    assert rate(a, b) == 80
    assert rate(a, a) is None
    assert rate(a, b, reboot=True) is None
    assert rate(b, {"value": 10, "sample_at": t + timedelta(seconds=20)}) is None
    assert (
        rate(
            {"value": 2**32 - 10, "sample_at": t},
            {"value": 10, "sample_at": t + timedelta(seconds=10)},
            bits=32,
        )
        == 16
    )
    assert rate(a, {"value": 1000, "sample_at": t + timedelta(hours=2)}) is None
    assert rate(a, {"value": float("nan"), "sample_at": t + timedelta(seconds=10)}) is None


@pytest.mark.parametrize(
    "value,kind",
    [
        (True, "xsd:int"),
        (-1, "xsd:unsignedInt"),
        (2**32, "xsd:unsignedInt"),
        ("true", "xsd:boolean"),
        ("bad\nline", "xsd:string"),
    ],
)
def test_invalid_values(value, kind):
    with pytest.raises(ValueError):
        validate_value(value, kind)
