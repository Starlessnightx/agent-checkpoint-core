from __future__ import annotations

import hashlib
import json
import math
from pathlib import PurePosixPath
import re
from typing import Any


class ContractError(ValueError):
    pass


def _validate(value: Any) -> None:
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _validate(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _validate(item)
        return
    raise ContractError("contract must contain finite JSON data only")


def canonical(value: Any) -> bytes:
    _validate(value)
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError) as exc:
        raise ContractError("invalid JSON encoding") from exc


def content_id(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ContractError("duplicate JSON key: " + key)
        value[key] = item
    return value


def decode(raw: bytes) -> Any:
    def reject_constant(value):
        raise ContractError("nonfinite JSON number")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=reject_constant)
    except (ValueError, UnicodeError) as exc:
        raise ContractError("invalid contract JSON") from exc
    _validate(value)
    return value


def identifier(value: str) -> str:
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ContractError("invalid SHA-256 reference")
    return value


def relative_path(value: str) -> str:
    if (type(value) is not str or not value or "\x00" in value or "\\" in value
            or value.startswith("/") or re.match(r"^[A-Za-z]:", value)):
        raise ContractError("path must be normalized and relative")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ContractError("path must not contain empty, dot or parent components")
    if str(PurePosixPath(value)) != value:
        raise ContractError("path is not canonical")
    return value


def fields(value: Any, expected: set[str]) -> dict:
    if type(value) is not dict or set(value) != expected:
        raise ContractError("contract fields differ from schema")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ContractError("unsupported schema version")
    return value
