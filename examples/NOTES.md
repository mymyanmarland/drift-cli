# Real-page runs — 2026-10-24

Each run: `drift watch <url>` immediately followed by
`drift check <url>`. Raw console output is in `*-run.txt`;
the written reports are in `*-drift-report.md`.

## 1. RelayModels — fallback page used, honestly

First choice was the API listing, tried **without any key**:

```
GET https://api.relaymodels.com/v1/models
-> 401 {"error": {"message": "Invalid token ...", "type": "new_api_error"}}
```

The endpoint requires a token, so the run used the public site
instead: **https://relaymodels.com/**

Result: snapshot 68 lines → check **NO CHANGE** (exit 0).
See `relaymodels-run.txt`, `relaymodels-drift-report.md`.

## 2. Next.js docs

**https://nextjs.org/docs** — snapshot 41 lines → check
**NO CHANGE** (exit 0).
See `nextjs-docs-run.txt`, `nextjs-docs-drift-report.md`.

## 3. KeyScope (owner's site) — fetch refused by the site's edge

**https://keyscope.funapp.live** — the site is up (cross-check
with curl at the same time: HTTP 200, full 30,448-byte body), but
its edge closes the connection early for Python's stdlib TLS
client: the body stops at exactly 16,139 of 30,448 bytes on every
attempt. Two things in drift-cli came out of finding this:

- `fetch()` now offers ALPN `http/1.1` (without it, the handshake
  itself was dropped — no response at all).
- A truncated body raises an honest ERROR (watch/check exit 2)
  instead of snapshotting a partial page, which would fake a diff
  on every later check.

Result: watch **ERROR** (exit 2), no snapshot, no report — see
`keyscope-run.txt`. A browser-grade TLS transport (e.g. an
optional curl fallback) is the v0.2 fix.
