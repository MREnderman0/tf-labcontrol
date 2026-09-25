"""Opt-in SCPI scalar message framing; never infer device batching support."""

from __future__ import annotations

import re
from collections.abc import Sequence

from lab_control.domain.instruments import DeviceError, ValidationError

_QUERY = re.compile(r"(?:\*[A-Za-z][A-Za-z0-9]*|:?[A-Za-z][A-Za-z0-9]*(?::[A-Za-z][A-Za-z0-9]*)*)\?")


def scalar_query_message(queries: Sequence[str]) -> str:
    """Join only argument-free, rooted queries selected by a qualified model."""
    if not 1 <= len(queries) <= 64 or any(not _QUERY.fullmatch(q) for q in queries):
        raise ValidationError("Expected 1..64 simple SCPI queries without arguments or selectors")
    return ";".join(q if q.startswith("*") else ":" + q.lstrip(":") for q in queries)


def scalar_response_fields(raw: bytes, expected: int) -> tuple[bytes, ...]:
    """Validate one complete response with an exact number of simple scalars.

    Caller must already have read a complete message. Quoted strings, multiline
    tables and binary blocks need their own framing and are deliberately excluded.
    """
    if not 1 <= expected <= 64 or len(raw) > 65536:
        raise DeviceError("Invalid scalar response size", "parse_error")
    body = raw.removesuffix(b"\n").removesuffix(b"\r")
    if any(byte < 32 or byte > 126 or byte in b"\"'#" for byte in body):
        raise DeviceError("Expected a single simple SCPI scalar response", "parse_error")
    fields = tuple(part.strip() for part in body.split(b";"))
    if len(fields) != expected or not all(fields):
        raise DeviceError(f"Expected {expected} SCPI scalar fields, received {len(fields)}", "parse_error")
    return fields
