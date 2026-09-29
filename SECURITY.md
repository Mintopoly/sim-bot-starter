# Security Policy

## Reporting a vulnerability

If you find a security issue in this kit (e.g. a way the client mishandles
credentials, a deserialization issue in the SSE parser, or a path that leaks
the API key in logs), **do not open a public issue.**

Please report it privately by:

1. Opening a [private security advisory](../../security/advisories/new) on GitHub, or
2. Reaching the maintainers via [Discord](https://discord.gg/childrenoftitan).

Include reproduction steps, affected version, and the impact you can demonstrate.
We'll acknowledge within 72 hours and aim to ship a fix within 14 days for
confirmed issues.

## Scope

This kit is a thin Python client for the public Sim API. The **bot kit itself**
is in scope: client code, SSE parser, retry logic, examples, Dockerfile.

Out of scope:

- Issues in the Sim API itself - report those at
  [childrenoftitan.com](https://www.childrenoftitan.com) or via Discord.
- Vulnerabilities in upstream dependencies (`httpx`, `numpy`, etc.) - report
  upstream first; we'll bump versions once a fix is published.

## Handling your API key

This kit reads your `SIM_API_KEY` from the environment or a local `.env` file
(via `python-dotenv`). The client only sends the key as a `Bearer` token to
the configured `base_url` (default `https://www.childrenoftitan.com`).

We never log the key, never persist it, and never include it in error
messages. If you find a path where it leaks, please report it.

If your key is ever exposed (committed to git, pasted in a screenshot, etc.),
rotate it immediately from your account settings - the new key invalidates
the old one.
