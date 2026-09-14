"""Protocol v1: one UTF-8 JSON object per LF-terminated frame.

JSON whitespace is permitted; duplicate keys, NaN/Infinity, BOM, unknown
protocol fields, lowercase verdicts and boolean-as-integer are rejected.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

VERSION = 1
MAX_FRAME = 16_384  # Includes optional CR; excludes terminating LF.
MAX_DEPTH = 24


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ProtocolError("DUPLICATE_KEY", f"duplicate JSON key: {key[:64]}")
        obj[key] = value
    return obj


def _constant(value: str) -> None:
    raise ProtocolError("INVALID_JSON", "NaN and Infinity are not supported")


def _depth(value: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ProtocolError("JSON_TOO_DEEP", "JSON nesting is too deep")
    if isinstance(value, dict):
        for key, item in value.items():
            # Lone surrogate escapes are legal JSON syntax but not valid Unicode.
            key.encode("utf-8", "strict")
            _depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ProtocolError("INVALID_JSON", "non-finite numbers are not supported")
    elif isinstance(value, str):
        value.encode("utf-8", "strict")


def decode(raw: bytes) -> dict[str, Any]:
    """Decode a frame WITHOUT LF. A single trailing CR is optional."""
    if len(raw) > MAX_FRAME:
        raise ProtocolError("FRAME_TOO_LARGE", "frame exceeds 16384 bytes")
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    if not raw or not raw.strip():
        raise ProtocolError("EMPTY_FRAME", "empty frames are not allowed")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ProtocolError("INVALID_ENCODING", "UTF-8 BOM is not allowed")
    try:
        obj = json.loads(raw.decode("utf-8", "strict"),
                         object_pairs_hook=_unique, parse_constant=_constant)
        _depth(obj)
    except ProtocolError:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("INVALID_JSON", "invalid UTF-8 or JSON") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("INVALID_MESSAGE", "top level must be an object")
    return obj


def encode(message: dict[str, Any]) -> bytes:
    raw = json.dumps(message, ensure_ascii=False, separators=(",", ":"),
                     allow_nan=False).encode("utf-8")
    if len(raw) > MAX_FRAME:
        raise ProtocolError("FRAME_TOO_LARGE", "outgoing frame is too large")
    return raw + b"\n"


def fingerprint(message: dict[str, Any]) -> str:
    raw = json.dumps(message, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def text(obj: dict[str, Any], key: str, maximum: int = 64) -> str:
    value = obj.get(key)
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum
            or value != value.strip() or any(ord(c) < 32 for c in value)):
        raise ProtocolError("INVALID_FIELD", f"{key} must be a nonempty, trimmed string (max {maximum})")
    return value


SCHEMAS = {
    "hello": ({"client_id", "access_code"}, set()),
    "ping": ({"session_id"}, set()),
    "get_target": ({"session_id"}, set()),
    "target_ack": ({"session_id", "target_revision"}, set()),
    "get_state": ({"session_id"}, set()),
    "result": ({"session_id", "round_id", "target_revision", "verdict"}, {"details"}),
}


def validate(message: dict[str, Any]) -> None:
    if type(message.get("v")) is not int or message["v"] != VERSION:
        raise ProtocolError("BAD_VERSION", "v must be integer 1")
    kind = text(message, "type", 32)
    text(message, "msg_id")
    if kind not in SCHEMAS:
        raise ProtocolError("UNKNOWN_TYPE", "unknown client message type")
    required, optional = SCHEMAS[kind]
    allowed = required | optional | {"v", "type", "msg_id"}
    if not required.issubset(message) or not set(message).issubset(allowed):
        raise ProtocolError("INVALID_FIELDS", "required field missing or unknown field present")
    for field in ("session_id", "round_id", "client_id", "access_code"):
        if field in message:
            text(message, field)
    if "target_revision" in message:
        rev = message["target_revision"]
        if type(rev) is not int or not 1 <= rev <= 2_147_483_647:
            raise ProtocolError("INVALID_FIELD", "target_revision must be a positive integer")
    if kind == "result":
        if message["verdict"] not in ("OK", "NG"):
            raise ProtocolError("INVALID_VERDICT", "verdict must be exactly OK or NG")
        if "details" in message and not isinstance(message["details"], dict):
            raise ProtocolError("INVALID_FIELD", "details must be an object")
