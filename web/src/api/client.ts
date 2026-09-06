import type { ErrorOut } from "./types";

/**
 * Bearer token lives in sessionStorage, deliberately not localStorage.
 *
 * localStorage survives a closed tab and is readable by any script on the
 * origin forever; sessionStorage dies with the tab, which bounds what an XSS
 * or a shared machine can hand over. Neither is actually safe against XSS -
 * the token is still readable by injected script while the tab is open. The
 * real fix is an httpOnly cookie, which needs CSRF protection and a SameSite
 * policy on the backend; that is a deliberate later step, not an oversight.
 */
const TOKEN_KEY = "pi-py.access-token";

export const tokenStore = {
  get(): string | null {
    return sessionStorage.getItem(TOKEN_KEY);
  },
  set(token: string): void {
    sessionStorage.setItem(TOKEN_KEY, token);
  },
  clear(): void {
    sessionStorage.removeItem(TOKEN_KEY);
  },
};

/**
 * Every non-2xx body on this API is {"detail": string} except 422 (FastAPI's
 * HTTPValidationError) and 503 (ReadyOut). requestId is the X-Request-Id the
 * server middleware stamps on every response - it is the only thing that ties a
 * browser complaint back to a line in the server's JSON access log, so it goes
 * into the message the user can copy.
 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly requestId: string,
  ) {
    super(message);
    this.name = "ApiError";
  }

  get isAuthFailure(): boolean {
    return this.status === 401;
  }
}

interface FieldError {
  loc: (string | number)[];
  msg: string;
}

function describe(status: number, body: unknown): string {
  if (body && typeof body === "object") {
    const detail = (body as ErrorOut).detail;
    if (typeof detail === "string") return detail;
    // 422: detail is a list of per-field errors.
    if (Array.isArray(detail)) {
      const parts = (detail as FieldError[])
        .filter((e) => Array.isArray(e.loc))
        .map((e) => `${e.loc.filter((p) => p !== "body").join(".") || "body"}: ${e.msg}`);
      if (parts.length) return parts.join("; ");
    }
  }
  return `请求失败（HTTP ${status}）`;
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
}

/**
 * Turn anything a catch block receives into displayable text.
 *
 * The catches are heterogeneous on purpose: ApiError carries the server's
 * detail plus its X-Request-Id, while a network failure or an abort is a raw
 * TypeError/DOMException with no status at all. Joining them here keeps the
 * request id in the message instead of only wherever someone remembered it.
 */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    return err.requestId ? `${err.message}（request ${err.requestId}）` : err.message;
  }
  if (err instanceof Error) return err.message;
  return String(err);
}

async function toError(res: Response): Promise<ApiError> {
  const requestId = res.headers.get("X-Request-Id") ?? "";
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    // A non-JSON error body (proxy 502, truncated response) is still reportable.
    body = null;
  }
  return new ApiError(res.status, describe(res.status, body), requestId);
}

/**
 * Convert a failed Response into an ApiError, dropping the token on a 401.
 *
 * Both the JSON path and the streaming path go through here, so "a revoked or
 * expired token is not recoverable by retrying" is enforced once rather than at
 * each call site. Clearing it stops the UI from showing a logged-in shell around
 * requests that all fail.
 */
export async function toApiError(res: Response): Promise<ApiError> {
  const err = await toError(res);
  if (err.isAuthFailure) tokenStore.clear();
  return err;
}

/** JSON request/response. Throws ApiError on any non-2xx. */
export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {};
  const token = tokenStore.get();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.body !== undefined) headers["Content-Type"] = "application/json";

  // A network failure or an abort rejects here with a raw TypeError/DOMException
  // rather than an ApiError: there is no status code to report.
  const res = await fetch(path, {
    method: options.method ?? "GET",
    headers,
    signal: options.signal,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });

  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as T;
}

/** Authenticated headers for the streaming call, which cannot use api(). */
export function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = tokenStore.get();
  if (token) headers.Authorization = `Bearer ${token}`;
  // Declaring what we accept is content negotiation only. Keeping the stream
  // unbuffered is the server's job (Cache-Control: no-cache plus
  // X-Accel-Buffering: no); a browser cannot suppress Accept-Encoding from
  // fetch, so do not expect this header to influence compression.
  headers.Accept = "text/event-stream";
  return headers;
}

/**
 * Multipart upload (POST /v1/sessions/{id}/files). api() is JSON-only on
 * purpose, so this is the one upload path - FormData sets its own boundary,
 * which a hand-built Content-Type header would corrupt. The filename travels
 * inside the multipart part, which is where FastAPI's UploadFile reads it.
 */
export async function upload<T>(path: string, file: File): Promise<T> {
  const headers: Record<string, string> = {};
  const token = tokenStore.get();
  if (token) headers.Authorization = `Bearer ${token}`;
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(path, { method: "POST", headers, body: form });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as T;
}
