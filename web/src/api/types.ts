/**
 * Type aliases over the generated schema. Everything here resolves into
 * ./schema.d.ts, which openapi-typescript produced from web/openapi.json - so
 * renaming a backend model breaks this file at compile time instead of at
 * runtime in somebody's browser.
 *
 * Do not hand-write a request or response shape in this directory. If a type is
 * missing here, it is missing from the backend's response models: fix it in
 * src/pi/server/app.py, re-run tools/dump_openapi.py, then npm run gen:api.
 */
import type { components } from "./schema";

type Schemas = components["schemas"];

export type ErrorOut = Schemas["ErrorOut"];
export type LoginOut = Schemas["LoginOut"];
export type LogoutOut = Schemas["LogoutOut"];
export type MeOut = Schemas["MeOut"];
export type Plan = Schemas["Plan"];
export type RegisterOut = Schemas["RegisterOut"];
export type Role = Schemas["Role"];
export type SessionCreatedOut = Schemas["SessionCreatedOut"];
export type SessionListOut = Schemas["SessionListOut"];
export type SessionSummary = Schemas["SessionSummary"];
export type MessageOut = Schemas["MessageOut"];
export type MessageListOut = Schemas["MessageListOut"];
export type UsageOut = Schemas["UsageOut"];
export type UsageModelOut = Schemas["UsageModelOut"];
export type DeregisterOut = Schemas["DeregisterOut"];

export type MemoryFactOut = Schemas["MemoryFactOut"];
export type MemoryListOut = Schemas["MemoryListOut"];
export type MemoryDeleteOut = Schemas["MemoryDeleteOut"];
export type MemoryClearOut = Schemas["MemoryClearOut"];

export type AdminUserOut = Schemas["AdminUserOut"];
export type AdminUserListOut = Schemas["AdminUserListOut"];
export type UserUpdateOut = Schemas["UserUpdateOut"];
export type RevokeOut = Schemas["RevokeOut"];
export type TraceRunOut = Schemas["TraceRunOut"];
export type TraceStepOut = Schemas["TraceStepOut"];
export type TraceListOut = Schemas["TraceListOut"];
/** One audit_events row, read back verbatim. Loose on purpose: the backend
 * keeps this untyped (a legacy payload must render, not 500), so the client
 * treats every field as optional when displaying it. */
export type AuditRecord = Record<string, unknown>;
export type AuditOut = Schemas["AuditOut"];

export type TextBlock = Schemas["TextBlock"];
export type ToolCallBlock = Schemas["ToolCallBlock"];
export type ToolResultBlock = Schemas["ToolResultBlock"];
export type FileBlock = Schemas["FileBlock"];
export type Block = TextBlock | ToolCallBlock | ToolResultBlock | FileBlock;

export type FileOut = Schemas["FileOut"];
export type FileListOut = Schemas["FileListOut"];

/** Which gateway-executed tools a run offers the model. */
export type BuiltinTool = "web_search" | "web_extractor" | "code_interpreter";

/**
 * Event name -> data payload, for POST /v1/sessions/{id}/runs.
 *
 * The backend publishes these as an `anyOf` under text/event-stream because
 * OpenAPI cannot express "the event: line picks which one"; this map is the
 * shape a client actually wants. TestSseEventPayloads in tests/test_server.py
 * is what keeps the members honest.
 */
export interface SseEventMap {
  start: Schemas["SseStartData"];
  text_delta: Schemas["SseTextDeltaData"];
  toolcall_start: Schemas["SseToolCallStartData"];
  toolcall_end: Schemas["SseToolCallEndData"];
  compaction: Schemas["SseCompactionData"];
  plan: Schemas["SsePlanData"];
  turn_end: Schemas["SseTurnEndData"];
  error: Schemas["SseErrorData"];
  done: Schemas["SseDoneData"];
}

export type SseEventName = keyof SseEventMap;

/** One typed stream event. Narrow on `event` to get the matching payload. */
export type RunEvent = {
  [K in SseEventName]: { event: K; data: SseEventMap[K] };
}[SseEventName];
