import { api, upload } from "./client";
import { postSse } from "./sse";
import type {
  AdminUserListOut,
  AuditOut,
  BuiltinTool,
  DeregisterOut,
  FileListOut,
  FileOut,
  LoginOut,
  LogoutOut,
  MeOut,
  MemoryClearOut,
  MemoryDeleteOut,
  MemoryListOut,
  MessageListOut,
  RegisterOut,
  RevokeOut,
  RunEvent,
  SessionCreatedOut,
  SessionListOut,
  SseEventName,
  TraceListOut,
  TraceRunOut,
  UsageOut,
  UserUpdateOut,
} from "./types";

/**
 * Typed wrappers for the endpoints this UI calls.
 *
 * Every return type comes from ./types, which comes from ./schema.d.ts, which
 * comes from web/openapi.json. Nothing here describes a shape by hand, so a
 * backend rename breaks the build rather than the browser.
 */

export const registerUser = (username: string, password: string) =>
  api<RegisterOut>("/v1/auth/register", { method: "POST", body: { username, password } });

export const login = (username: string, password: string) =>
  api<LoginOut>("/v1/auth/login", { method: "POST", body: { username, password } });

export const logout = () => api<LogoutOut>("/v1/auth/logout", { method: "POST" });

export const whoami = () => api<MeOut>("/v1/me");

/** Irreversible. The server demands the password even with a valid token. */
export const deregister = (password: string) =>
  api<DeregisterOut>("/v1/me", { method: "DELETE", body: { password } });

export const listSessions = () => api<SessionListOut>("/v1/sessions");

export const createSession = (title: string, model?: string) =>
  api<SessionCreatedOut>("/v1/sessions", { method: "POST", body: { title, model } });

export const getMessages = (sessionId: string) =>
  api<MessageListOut>(`/v1/sessions/${encodeURIComponent(sessionId)}/messages`);

export const listFiles = (sessionId: string) =>
  api<FileListOut>(`/v1/sessions/${encodeURIComponent(sessionId)}/files`);

/** Upload into the session workspace; the returned URL is what the model
 * gateway fetches when the file is attached to a run. */
export const uploadFile = (sessionId: string, file: File) =>
  upload<FileOut>(
    `/v1/sessions/${encodeURIComponent(sessionId)}/files?filename=${encodeURIComponent(file.name)}`,
    file,
  );

export const getUsage = () => api<UsageOut>("/v1/usage");

export const listMemories = (limit = 100) => {
  const params = new URLSearchParams();
  if (limit !== 100) params.set("limit", String(limit));
  const qs = params.toString();
  return api<MemoryListOut>(qs ? `/v1/memories?${qs}` : "/v1/memories");
};
export const clearMemories = () => api<MemoryClearOut>("/v1/memories", { method: "DELETE" });
export const deleteMemory = (factId: number) =>
  api<MemoryDeleteOut>(`/v1/memories/${encodeURIComponent(factId)}`, { method: "DELETE" });

export const listAdminUsers = () => api<AdminUserListOut>("/v1/admin/users");

export const updateAdminUser = (
  username: string,
  changes: { quota_tokens?: number; is_active?: boolean },
) =>
  api<UserUpdateOut>(`/v1/admin/users/${encodeURIComponent(username)}`, {
    method: "PATCH",
    body: changes,
  });

export const revokeAdminUser = (username: string) =>
  api<RevokeOut>(`/v1/admin/users/${encodeURIComponent(username)}/revoke`, { method: "POST" });

export interface AuditFilter {
  user?: string;
  event?: string;
  limit?: number;
}

export const listAudit = (filter: AuditFilter = {}) => {
  const params = new URLSearchParams();
  if (filter.user) params.set("user", filter.user);
  if (filter.event) params.set("event", filter.event);
  if (filter.limit) params.set("limit", String(filter.limit));
  const qs = params.toString();
  return api<AuditOut>(qs ? `/v1/admin/audit?${qs}` : "/v1/admin/audit");
};

export interface TraceFilter {
  user?: string;
  session?: string;
  status?: string;
  anomaly?: boolean;
  limit?: number;
}

export const listTraces = (filter: TraceFilter = {}) => {
  const params = new URLSearchParams();
  if (filter.user) params.set("user", filter.user);
  if (filter.session) params.set("session", filter.session);
  if (filter.status) params.set("status", filter.status);
  if (filter.anomaly) params.set("anomaly", "true");
  if (filter.limit) params.set("limit", String(filter.limit));
  const qs = params.toString();
  return api<TraceListOut>(qs ? `/v1/admin/traces?${qs}` : "/v1/admin/traces");
};

export const getTrace = (runId: string) =>
  api<TraceRunOut>(`/v1/admin/traces/${encodeURIComponent(runId)}`);

// `satisfies` in both directions: a regenerated schema that adds an event fails
// to compile until it is listed here, and an event the backend dropped fails too.
// Without it, an unknown frame would be silently ignored forever.
const KNOWN_EVENTS = {
  start: true,
  text_delta: true,
  toolcall_start: true,
  toolcall_end: true,
  compaction: true,
  plan: true,
  turn_end: true,
  error: true,
  done: true,
} satisfies Record<SseEventName, true>;

function isKnownEvent(name: string): name is SseEventName {
  return name in KNOWN_EVENTS;
}

/**
 * Stream one turn. Yields typed events as they arrive; the generator ends when
 * the server closes the stream.
 *
 * Two failure channels, and they are not interchangeable:
 * - a rejected promise / thrown ApiError means the run never started (404 unknown
 *   session, 429 rate limit, 402 quota, 401 auth);
 * - an `error` *event* means the run started and failed mid-turn, delivered
 *   inside an HTTP 200. A caller that only try/catches will miss those.
 *
 * Pass an AbortSignal to stop early; the reader is cancelled on the way out.
 */
export async function* run(
  sessionId: string,
  prompt: string,
  options: {
    model?: string;
    enableSearch?: boolean;
    builtinTools?: BuiltinTool[];
    files?: string[];
    signal?: AbortSignal;
  } = {},
): AsyncGenerator<RunEvent> {
  const frames = postSse({
    path: `/v1/sessions/${encodeURIComponent(sessionId)}/runs`,
    body: {
      prompt,
      model: options.model,
      enable_search: options.enableSearch ?? false,
      builtin_tools: options.builtinTools ?? [],
      files: options.files ?? [],
    },
    signal: options.signal,
  });
  for await (const frame of frames) {
    // Forward compatibility: an older client must survive a newer server, so an
    // event name outside KNOWN_EVENTS is skipped rather than fatal.
    if (!isKnownEvent(frame.event)) continue;
    // JSON.parse is the one unchecked step in the whole client. The server
    // validates these payloads on the way out (response models) and
    // TestSseEventPayloads pins them to the models, but the browser cannot
    // re-verify without a runtime schema library.
    yield { event: frame.event, data: JSON.parse(frame.data) } as RunEvent;
  }
}
