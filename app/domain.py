import hashlib
import json
import math
import re
from datetime import UTC, datetime

SECRET = re.compile(
    r"password|passphrase|secret|token|credential|private.?key|preshared|wepkey|"
    r"connectionrequesturl|username|authorization|url$",
    re.IGNORECASE,
)
TERMINAL = {"COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"}
ROLES = {"VIEWER": 0, "OPERATOR": 1, "ADMIN": 2, "OWNER": 3}


def now():
    return datetime.now(UTC)


def stamp(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def flatten(tree, prefix=""):
    out = []
    for key, value in tree.items():
        if key.startswith("_") or not isinstance(value, dict):
            continue
        path = f"{prefix}.{key}" if prefix else key
        if "_value" in value:
            out.append(
                {
                    "path": path,
                    "value": "[redacted]" if SECRET.search(path) else value["_value"],
                    "type": value.get("_type"),
                    "writable": value.get("_writable", False),
                    "sample_at": stamp(value.get("_timestamp")),
                }
            )
        out.extend(flatten(value, path))
    return out


def classify(last_inform, active=600, offline=1800, at=None):
    last = stamp(last_inform)
    if last is None:
        return "UNKNOWN"
    age = ((at or now()) - last).total_seconds()
    if age < -60:
        return "UNKNOWN"
    return "ACTIVE" if age <= active else "STALE" if age <= offline else "OFFLINE"


def rate(previous, current, bits=None, reboot=False, max_gap=1800):
    """Return bit/s only from distinct device sample timestamps; never collector time."""
    if not previous or reboot:
        return None
    a, b = stamp(previous.get("sample_at")), stamp(current.get("sample_at"))
    if not a or not b or not 0 < (b - a).total_seconds() <= max_gap:
        return None
    x, y = previous.get("value"), current.get("value")
    if (
        isinstance(x, bool)
        or isinstance(y, bool)
        or not isinstance(x, (int, float))
        or not isinstance(y, (int, float))
    ):
        return None
    if not math.isfinite(x) or not math.isfinite(y) or min(x, y) < 0:
        return None
    delta = y - x
    if delta < 0:
        # Only accept a wrap close to the configured modulus, otherwise it is a reset.
        if bits not in (32, 64) or x < 0.9 * (2**bits) or y > 0.1 * (2**bits):
            return None
        delta += 2**bits
    return delta * 8 / (b - a).total_seconds()


def parameters_matching(parameters, query="", category="", data_type=""):
    categories = {
        "network": r"WAN|LAN|Ethernet|IP\.|Interface|DSL",
        "wifi": r"WiFi|WLAN",
        "fiber": r"Optical|GPON|EPON|PON|Laser",
        "cellular": r"Cellular|LTE|5G|RSRP|RSRQ|SINR|RSSI|CellID|PCI",
        "traffic": r"Bytes|Packets|Errors|Discard",
        "hardware": r"CPU|Memory|Temperature|UpTime|Load",
    }
    return [
        p
        for p in parameters
        if (not query or query.lower() in (p["path"] + str(p["value"])).lower())
        and (
            not category or re.search(categories.get(category, re.escape(category)), p["path"], re.IGNORECASE)
        )
        and (not data_type or p.get("type") == data_type)
    ]


def validate_value(value, kind):
    if kind in ("xsd:unsignedInt", "xsd:unsignedLong", "xsd:int", "xsd:long"):
        if type(value) is not int:
            raise ValueError("Integer value required")
        bits = 64 if kind.endswith(("Long", "long")) else 32
        low, high = (0, 2**bits - 1) if "unsigned" in kind else (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1)
        if not low <= value <= high:
            raise ValueError("Integer out of range")
    elif kind == "xsd:boolean":
        if type(value) is not bool:
            raise ValueError("Boolean value required")
    elif kind == "xsd:string":
        if not isinstance(value, str) or len(value) > 1024 or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid string")
    else:
        raise ValueError("Unsupported writable data type")
