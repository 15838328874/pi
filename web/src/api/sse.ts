import { ApiError, authHeaders, toApiError } from "./client";

/**
 * Server-Sent Events over POST.
 *
 * The browser's EventSource only speaks GET, and /v1/sessions/{id}/runs is a
 * POST (it carries a prompt body and an Authorization header), so the stream has
 * to be read by hand: fetch -> res.body.getReader() -> split on blank lines.
 */
export interface SseFrame {
  /** Value of the frame's `event:` field, or "message" when absent. */
  event: string;
  /** The `data:` field(s) joined with newlines, exactly as the SSE spec says. */
  data: string;
}

// A frame ends at a blank line. Accept CRLF as well as LF: the server writes LF,
// but a proxy is allowed to normalise line endings and a parser that only knows
// one form silently hangs on the other.
const FRAME_END = /\r?\n\r?\n/;

export function parseFrame(raw: string): SseFrame | null {
  let event = "message";
  const data: string[] = [];
  for (const line of raw.split(/\r?\n/)) {
    // A leading colon is a comment - that is how a keep-alive is sent.
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    if (colon === -1) continue;
    const field = line.slice(0, colon);
    // Per spec, strip exactly one leading space after the colon.
    const value = line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  if (!data.length && event === "message") return null;
  return { event, data: data.join("\n") };
}

export interface SseRequest {
  path: string;
  body?: unknown;
  signal?: AbortSignal;
}

/**
 * Yields frames as they arrive. Rejects with ApiError for a non-2xx response,
 * because 404/429/402 are all decided *before* the stream is opened - only a
 * run that fails mid-turn becomes an `error` event inside an HTTP 200.
 */
export async function* postSse(request: SseRequest): AsyncGenerator<SseFrame> {
  const res = await fetch(request.path, {
    method: "POST",
    headers: authHeaders(),
    body: request.body === undefined ? undefined : JSON.stringify(request.body),
    signal: request.signal,
  });

  if (!res.ok) throw await toApiError(res);
  if (!res.body) throw new ApiError(res.status, "浏览器没有提供响应流", res.headers.get("X-Request-Id") ?? "");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let match = FRAME_END.exec(buffer);
      while (match !== null) {
        const raw = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        const frame = parseFrame(raw);
        if (frame) yield frame;
        match = FRAME_END.exec(buffer);
      }
    }
    // A server that closes without a trailing blank line still owes us the last
    // frame; without this the final `done` can be dropped on an abrupt close.
    buffer += decoder.decode();
    if (buffer.trim()) {
      const frame = parseFrame(buffer);
      if (frame) yield frame;
    }
  } finally {
    // Release the connection when the consumer breaks out early (user hit stop).
    reader.cancel().catch(() => undefined);
  }
}
