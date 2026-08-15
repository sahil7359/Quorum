import type { ReviewEvent } from "./types";

// why the scheme is added if missing: Render's blueprint wires this from the
// backend service's `host` property (see render.yaml), which is a bare
// hostname with no `https://`. `fetch` against a scheme-less base treats it as
// a path, not an origin, and fails. Local dev already includes the scheme, so
// this only ever fires for the deployed, auto-wired value.
function normalizeBase(raw: string): string {
  return /^https?:\/\//.test(raw) ? raw : `https://${raw}`;
}

const API_BASE = normalizeBase(process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000");

export class ReviewRequestError extends Error {}

/**
 * Streams a review over SSE. `fetch` + a manual reader, not the browser's
 * `EventSource`: `EventSource` cannot send a POST body, and the repo/PR
 * number have to be in the request, not the URL -- the backend deliberately
 * keeps them out of the query string (no PR content in a URL that ends up in
 * logs, browser history, or a proxy's access log).
 *
 * Parses the `text/event-stream` framing by hand: blocks separated by a
 * blank line, an `event:` line and a `data:` line each. `: ping - ...`
 * comment lines (sse-starlette's keepalive, sent so a long-silent ingestion
 * or LLM call doesn't look like a dead connection to an intermediary) are
 * skipped, not errors.
 */
export async function* streamReview(
  repo: string,
  prNumber: number,
  signal?: AbortSignal,
): AsyncGenerator<ReviewEvent> {
  const response = await fetch(`${API_BASE}/api/reviews`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo, pr_number: prNumber }),
    signal,
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new ReviewRequestError(`${response.status}: ${detail.slice(0, 300)}`);
  }
  if (!response.body) {
    throw new ReviewRequestError("no response body");
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      // why normalise CRLF to LF here rather than parse both: sse-starlette
      // frames events with \r\n line endings and \r\n\r\n block separators,
      // not the \n / \n\n the SSE spec also permits. Splitting on "\n\n"
      // against a \r\n\r\n stream never finds a boundary, so every event sat
      // unparsed in this buffer until the stream closed -- the whole reason
      // the first browser test showed a review that ran on the backend but
      // never rendered. Normalising once, up front, means the boundary and
      // line logic below only has to know about one newline convention.
      buffer += value.replace(/\r\n/g, "\n");

      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const parsed = parseBlock(block);
        if (parsed) yield parsed;
        boundary = buffer.indexOf("\n\n");
      }
    }
  } finally {
    reader.releaseLock();
  }
}

function parseBlock(block: string): ReviewEvent | null {
  let eventName: string | null = null;
  let data: string | null = null;

  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue; // keepalive comment
    if (line.startsWith("event: ")) eventName = line.slice("event: ".length);
    else if (line.startsWith("data: ")) data = line.slice("data: ".length);
  }

  if (eventName === null || data === null) return null;
  // why an unsafe cast rather than a runtime schema check: this is the same
  // trust boundary as any same-team backend/frontend pair -- the payload
  // shape is asserted by review_service.py's own event_ helpers and covered
  // by that side's tests, not by data an external caller controls the shape
  // of. A full runtime validator here would be checking our own backend's
  // honesty, not a real trust boundary. Parsed into `unknown` first, not
  // `any`, so this cast is the one deliberate escape hatch rather than a
  // silent `any` leaking into the rest of the block.
  const payload: unknown = JSON.parse(data);
  return { event: eventName, data: payload } as ReviewEvent;
}
