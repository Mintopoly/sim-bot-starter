"""Smoke test for the SSE stream: print up to 50 events or run ~70s, then exit."""

from __future__ import annotations

import time

from simbot import Client


def run() -> None:
    deadline = time.monotonic() + 70
    count = 0
    seen_types: set[str] = set()
    with Client() as client, client.stream() as events:
        for event in events:
            count += 1
            seen_types.add(event.type)
            payload_keys = sorted((event.data or {}).keys())[:6]
            print(f"[{count:03d}] type={event.type}  keys={payload_keys}")
            if time.monotonic() > deadline:
                break
            if count >= 50:
                break
    print(f"--- {count} events, types: {sorted(seen_types)}")


if __name__ == "__main__":
    run()
