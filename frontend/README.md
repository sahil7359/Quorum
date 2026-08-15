# Quorum frontend

A thin Next.js reader for the Quorum review API. No secrets, no server-side intelligence: it
POSTs a `{repo, pr_number}` to the backend, parses the Server-Sent Events stream, and renders
the review as it forms.

## Why it's shaped this way

- **`fetch` + a manual SSE reader, not `EventSource`.** `EventSource` can only GET, and the
  repo/PR number must travel in a request body, not the URL — the backend deliberately keeps PR
  identifiers out of query strings (nothing sensitive in a logged or cached URL). The parser
  normalises `\r\n` framing to `\n` up front; sse-starlette uses CRLF, and splitting on `\n\n`
  against a CRLF stream silently buffers every event forever — a bug caught only by running the
  real thing in a browser, not by any unit test.
- **The event types mirror the backend exactly.** `lib/types.ts`'s `ReviewEvent` union is the
  one source of truth for what can arrive on the stream, matching `review_service.py`'s
  `review_stream` and `_node_event`. The stream is trusted the way any same-team API is — the
  shape is asserted by the backend's own tests, so there's no runtime schema validator here.
- **Progressive, not spinner-then-result.** Each pipeline step (ingest → route → specialists →
  synthesise, plus a first-time ingestion step) fills in with real detail as its event arrives,
  because the whole point of the SSE surface is watching a review form rather than waiting
  blankly for it.

## Develop

```bash
pnpm install
pnpm dev          # http://localhost:3000, expects the backend on :8000
```

`pnpm typecheck` (strict `tsc`), `pnpm lint` (typescript-eslint, type-checked rules, no `any`,
no floating promises), `pnpm build`.

Point it at a different backend with `NEXT_PUBLIC_API_BASE` (defaults to
`http://localhost:8000`). The value is inlined at build time, not read at runtime — it's a
`NEXT_PUBLIC_` var.
