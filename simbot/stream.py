"""Lightweight Server-Sent Events (SSE) parser for the Sim event stream.

The Sim stream emits one JSON-encoded message per `data:` frame. This parser
yields `SimEvent` objects from a streaming iterator of bytes — no extra deps.

The SSE spec is more general than this parser handles; for the Sim's specific
shape (`data: {...}\n\n` per event) it's enough. If you need full SSE support,
swap this for `httpx-sse`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx

from simbot.types import SimEvent


def parse_sse_lines(byte_iter: Iterator[bytes]) -> Iterator[SimEvent]:
    """Parse an iterator of byte chunks into SimEvent objects.

    Buffers incoming bytes, splits on the SSE record terminator (`\\n\\n`), and
    decodes each `data:` payload as JSON. Skips comments, retry directives,
    and any non-JSON payload (with a debug-friendly fallthrough).
    """
    buffer = b""
    for chunk in byte_iter:
        if not chunk:
            continue
        buffer += chunk
        while b"\n\n" in buffer:
            record, buffer = buffer.split(b"\n\n", 1)
            event = _parse_record(record)
            if event is not None:
                yield event


def _parse_record(record: bytes) -> SimEvent | None:
    data_lines: list[str] = []
    for raw_line in record.split(b"\n"):
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or line.startswith(":"):
            continue  # comment / heartbeat
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    if not data_lines:
        return None

    payload = "\n".join(data_lines)
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, dict):
        return None

    event_type = str(parsed.get("type", "unknown"))
    return SimEvent(type=event_type, data=parsed)


def stream_events(response: httpx.Response) -> Iterator[SimEvent]:
    """Iterate `SimEvent`s from a streaming httpx Response.

    The caller is responsible for opening the stream with `client.stream(...)`
    and closing it (use `with` blocks).
    """
    yield from parse_sse_lines(response.iter_bytes())
