"""Tests for the SSE parser."""

from __future__ import annotations

from simbot.stream import parse_sse_lines


def _chunks(text: str) -> list[bytes]:
    """Split text into single-byte chunks to stress the buffering logic."""
    return [text.encode("utf-8")[i : i + 1] for i in range(len(text))]


def test_parses_single_event() -> None:
    raw = b'data: {"type":"block:advance","block":42}\n\n'
    events = list(parse_sse_lines(iter([raw])))
    assert len(events) == 1
    assert events[0].type == "block:advance"
    assert events[0].data["block"] == 42


def test_parses_multiple_events_in_one_chunk() -> None:
    raw = (
        b'data: {"type":"ping"}\n\n'
        b'data: {"type":"block:advance","block":1}\n\n'
    )
    events = list(parse_sse_lines(iter([raw])))
    assert [e.type for e in events] == ["ping", "block:advance"]


def test_parses_event_split_across_chunks() -> None:
    full = b'data: {"type":"event:start","event":{"id":"e1"}}\n\n'
    # Split into 1-byte chunks — buffer must reassemble
    chunks = [bytes([b]) for b in full]
    events = list(parse_sse_lines(iter(chunks)))
    assert len(events) == 1
    assert events[0].type == "event:start"
    assert events[0].data["event"]["id"] == "e1"


def test_skips_comments_and_blank_lines() -> None:
    raw = b": heartbeat\n\ndata: {\"type\":\"ping\"}\n\n"
    events = list(parse_sse_lines(iter([raw])))
    assert len(events) == 1
    assert events[0].type == "ping"


def test_invalid_json_is_skipped_not_raised() -> None:
    raw = b"data: not-json\n\ndata: {\"type\":\"ok\"}\n\n"
    events = list(parse_sse_lines(iter([raw])))
    assert len(events) == 1
    assert events[0].type == "ok"


def test_unknown_event_type_passthrough() -> None:
    raw = b'data: {"type":"future_event","x":1}\n\n'
    events = list(parse_sse_lines(iter([raw])))
    assert len(events) == 1
    assert events[0].type == "future_event"
    assert events[0].data["x"] == 1
