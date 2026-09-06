import { computed, ref } from "vue";
import type { Ref } from "vue";
import { defineStore } from "pinia";
import { errorMessage } from "../api/client";
import {
  createSession,
  getMessages,
  listFiles,
  listSessions,
  run,
  uploadFile,
} from "../api/endpoints";
import type {
  BuiltinTool,
  FileOut,
  MessageOut,
  Plan,
  Role,
  RunEvent,
  SessionSummary,
} from "../api/types";

/** One tool call as the transcript renders it. */
export interface UiToolCall {
  id: string;
  name: string;
  /** Raw JSON arguments. Empty while streaming: the SSE contract carries none. */
  args: string;
  state: "running" | "ok" | "error";
  result: string;
}

/** A file attached to a user message, as the transcript renders it. */
export interface UiFile {
  name: string;
  url: string;
}

export interface UiMessage {
  role: Role;
  text: string;
  tools: UiToolCall[];
  /** Files attached to this message (uploads ride inside the message blocks). */
  files: UiFile[];
  /** Present when this message is the plan card produced by submit_plan. */
  plan?: Plan;
}

/**
 * The agent loop streams text, then tool calls, then runs each tool, then loops.
 * There is no message-boundary event, so a text_delta arriving after a
 * tool_result is the only signal that a new assistant message has begun.
 *
 * This painter exists to turn that stream into messages, and it is deliberately
 * throwaway: send() replaces its output with the persisted transcript once the
 * stream ends. A mistake here shows up as a briefly mis-shaped message rather
 * than a wrong history.
 *
 * Exported for tests/transcript.test.ts: there is no browser here to catch it.
 */
export function painter(messages: Ref<UiMessage[]>) {
  let current: UiMessage | null = null;
  let afterToolEnd = false;

  function assistant(): UiMessage {
    if (current === null || afterToolEnd) {
      messages.value.push({ role: "assistant", text: "", tools: [], files: [] });
      // Read it back rather than keeping the object we pushed: push() stores the
      // raw value, and mutating a raw object bypasses the proxy's set trap, so
      // the streamed deltas would never reach the DOM.
      current = messages.value[messages.value.length - 1];
      afterToolEnd = false;
    }
    return current;
  }

  function toolCall(id: string, name: string): UiToolCall {
    const msg = assistant();
    // Elements read back out of a reactive array are already proxies.
    const existing = msg.tools.find((t) => t.id === id);
    if (existing !== undefined) return existing;
    msg.tools.push({ id, name, args: "", state: "running", result: "" });
    return msg.tools[msg.tools.length - 1];
  }

  return function apply(ev: RunEvent): void {
    switch (ev.event) {
      case "text_delta":
        assistant().text += ev.data.text;
        break;

      case "toolcall_start":
        // Emitted once per streamed argument chunk, so the same id arrives
        // repeatedly. toolCall() keys on the id and ignores the repeats.
        toolCall(ev.data.id, ev.data.name);
        break;

      case "toolcall_end": {
        const call = toolCall(ev.data.id, ev.data.name);
        call.state = ev.data.ok ? "ok" : "error";
        call.result = ev.data.result;
        afterToolEnd = true;
        break;
      }

      case "compaction":
        messages.value.push({
          role: "system",
          text: `已压缩历史：${ev.data.dropped} 条消息被摘要替代（${ev.data.chars_before} → ${ev.data.chars_after} 字符）`,
          tools: [],
          files: [],
        });
        current = null;
        break;

      case "plan":
        // Its own message, like compaction. The server emits `plan` after the last
        // toolcall_end and before turn_end, so nothing follows it in this run.
        // Role is assistant to match what toUi() renders after the reload.
        messages.value.push({
          role: "assistant",
          text: "",
          tools: [],
          files: [],
          plan: { title: ev.data.title, steps: ev.data.steps },
        });
        current = null;
        break;

      case "start":
      case "turn_end":
      case "error":
      case "done":
        // Handled by send(): they are run-level facts, not transcript content.
        break;
    }
  };
}

/**
 * Parse persisted submit_plan arguments into a plan, or null if they are anything
 * else. A null sends the call down the ordinary tool-row path: raw arguments are
 * still evidence the user should see, which is the same reasoning prettyArgs uses.
 */
function parsePlan(args: string): Plan | null {
  let v: unknown;
  try {
    v = JSON.parse(args);
  } catch {
    return null;
  }
  if (typeof v !== "object" || v === null) return null;
  const { title, steps } = v as { title?: unknown; steps?: unknown };
  if (typeof title !== "string" || !Array.isArray(steps)) return null;
  const parsed = steps.filter((s): s is string => typeof s === "string");
  if (parsed.length !== steps.length || parsed.length === 0) return null;
  return { title, steps: parsed };
}

/**
 * Fold persisted messages into renderable ones.
 *
 * The loop appends tool results as a *user* message, so a result arrives one
 * message after the call it answers. Attaching results back onto their calls is
 * what makes the transcript readable, and it drops the now-empty user messages.
 *
 * Exported for tests/transcript.test.ts.
 */
export function toUi(history: MessageOut[]): UiMessage[] {
  const out: UiMessage[] = [];
  const calls = new Map<string, UiToolCall>();

  // A submit_plan that failed validation was never recorded, so it must not render
  // as a plan card. Its result arrives one message later, which means the fold
  // cannot decide that when it sees the call - hence the pre-pass.
  const failed = new Set<string>();
  for (const m of history) {
    for (const b of m.blocks) {
      if (b.type === "tool_result" && b.is_error) failed.add(b.tool_use_id);
    }
  }

  for (const m of history) {
    const ui: UiMessage = { role: m.role, text: "", tools: [], files: [] };
    for (const b of m.blocks) {
      switch (b.type) {
        case "text":
          ui.text += b.text;
          break;
        case "file":
          ui.files.push({ name: b.name || b.file_url, url: b.file_url });
          break;
        case "tool_call": {
          const call: UiToolCall = {
            id: b.id,
            name: b.name,
            args: b.arguments,
            // A stored call with no stored result means the run was cut short.
            state: "running",
            result: "",
          };
          calls.set(b.id, call);
          const plan = b.name === "submit_plan" && !failed.has(b.id) ? parsePlan(b.arguments) : null;
          if (plan !== null) {
            // Registered above so the paired tool_result is absorbed rather than
            // falling through to the orphan branch and rendering as a stray user
            // bubble, but deliberately not pushed into ui.tools: the card below
            // already shows this call, and a row of raw JSON under it is noise.
            ui.plan = plan;
            break;
          }
          ui.tools.push(call);
          break;
        }
        case "tool_result": {
          const call = calls.get(b.tool_use_id);
          if (call === undefined) {
            // No matching call - an orphan from a truncated run. Show it rather
            // than silently discarding text the user paid tokens for.
            ui.text += b.content;
            break;
          }
          call.state = b.is_error ? "error" : "ok";
          call.result = b.content;
          break;
        }
      }
    }
    if (ui.text === "" && ui.tools.length === 0 && ui.files.length === 0 && ui.plan === undefined)
      continue;
    out.push(ui);
  }
  return out;
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

function titleFrom(prompt: string): string {
  const line = prompt.trim().split("\n", 1)[0]?.trim() ?? "";
  return line.length > 40 ? `${line.slice(0, 40)}…` : line || "新对话";
}

export const useChat = defineStore("chat", () => {
  const sessions = ref<SessionSummary[]>([]);
  /** Null means "composing a conversation that does not exist yet". */
  const activeId = ref<string | null>(null);
  const messages = ref<UiMessage[]>([]);
  const loading = ref(false);
  const streaming = ref(false);
  const error = ref("");
  /** A mid-turn failure, which arrives as an `error` event inside HTTP 200. */
  const streamError = ref("");
  const model = ref("");
  const lastTurn = ref<{ turns: number; input: number; output: number } | null>(null);

  // --- model-native capabilities (per run, user-toggled) ---
  /** 联网搜索: the endpoint weaves search results into the answer. */
  const enableSearch = ref(false);
  /** Gateway-executed tools offered to the model this run. */
  const builtinTools = ref<BuiltinTool[]>([]);

  // --- session files ---
  const files = ref<FileOut[]>([]);
  /** Names from `files` that ride along with the next send(). */
  const pendingFiles = ref<string[]>([]);
  const uploading = ref(false);
  const filesError = ref("");

  function toggleTool(tool: BuiltinTool): void {
    const i = builtinTools.value.indexOf(tool);
    if (i === -1) builtinTools.value.push(tool);
    else builtinTools.value.splice(i, 1);
  }

  function togglePending(name: string): void {
    const i = pendingFiles.value.indexOf(name);
    if (i === -1) pendingFiles.value.push(name);
    else pendingFiles.value.splice(i, 1);
  }

  async function loadFiles(id: string): Promise<void> {
    try {
      files.value = (await listFiles(id)).files;
      // Drop pending names that no longer exist on the server.
      const names = new Set(files.value.map((f) => f.name));
      pendingFiles.value = pendingFiles.value.filter((n) => names.has(n));
    } catch (err) {
      filesError.value = errorMessage(err);
    }
  }

  async function upload(list: File[]): Promise<void> {
    const sid = activeId.value;
    if (sid === null || list.length === 0) return;
    uploading.value = true;
    filesError.value = "";
    try {
      for (const f of list) {
        const out = await uploadFile(sid, f);
        if (!files.value.some((e) => e.name === out.name)) files.value.push(out);
        if (!pendingFiles.value.includes(out.name)) pendingFiles.value.push(out.name);
      }
    } catch (err) {
      filesError.value = errorMessage(err);
    } finally {
      uploading.value = false;
    }
  }

  let aborter: AbortController | null = null;

  const active = computed(() => sessions.value.find((s) => s.id === activeId.value) ?? null);

  async function loadSessions(): Promise<void> {
    try {
      sessions.value = (await listSessions()).sessions;
    } catch (err) {
      error.value = errorMessage(err);
    }
  }

  /** Clear the pane without creating anything; send() creates on first message. */
  function startNew(): void {
    stop();
    activeId.value = null;
    messages.value = [];
    streamError.value = "";
    lastTurn.value = null;
  }

  async function selectSession(id: string): Promise<void> {
    if (id === activeId.value) return;
    // Painting a live stream into another session's transcript is worse than
    // interrupting it, and the server serializes turns per session anyway.
    stop();
    activeId.value = id;
    streamError.value = "";
    lastTurn.value = null;
    files.value = [];
    pendingFiles.value = [];
    filesError.value = "";
    void loadFiles(id);
    await reload(id);
  }

  async function reload(id: string): Promise<void> {
    loading.value = true;
    error.value = "";
    try {
      const list = (await getMessages(id)).messages;
      // The session may have changed under us while the request was in flight.
      if (activeId.value === id) messages.value = toUi(list);
    } catch (err) {
      error.value = errorMessage(err);
    } finally {
      loading.value = false;
    }
  }

  /** Cancel an in-flight run. The reader is released on the way out. */
  function stop(): void {
    aborter?.abort();
    aborter = null;
  }

  /**
   * Wipe everything, on sign-out.
   *
   * Pinia's $reset() does not exist for setup stores and these refs outlive the
   * components that read them, so without this a second user signing in on the
   * same tab would briefly see the first user's session list and transcript.
   */
  function clear(): void {
    stop();
    sessions.value = [];
    activeId.value = null;
    messages.value = [];
    loading.value = false;
    streaming.value = false;
    error.value = "";
    streamError.value = "";
    model.value = "";
    lastTurn.value = null;
    enableSearch.value = false;
    builtinTools.value = [];
    files.value = [];
    pendingFiles.value = [];
    uploading.value = false;
    filesError.value = "";
  }

  async function send(prompt: string): Promise<void> {
    const text = prompt.trim();
    if (streaming.value || text === "") return;
    error.value = "";
    streamError.value = "";

    if (activeId.value === null) {
      loading.value = true;
      try {
        const created = await createSession(titleFrom(text));
        sessions.value.unshift({
          id: created.id,
          title: created.title,
          model: created.model,
          // SessionCreatedOut has no created_at; the list refresh fills it in.
          created_at: "",
        });
        activeId.value = created.id;
        model.value = created.model;
      } catch (err) {
        error.value = errorMessage(err);
        return;
      } finally {
        loading.value = false;
      }
    }
    const sid = activeId.value;
    const attach = [...pendingFiles.value];

    // Painted optimistically. If the run is aborted this disappears on the
    // reload below, which is the truth: the server persists a turn only when the
    // stream completes, so an aborted turn stores nothing - prompt included.
    const attached = attach.map((name) => {
      const f = files.value.find((x) => x.name === name);
      return { name, url: f?.url ?? "" };
    });
    messages.value.push({ role: "user", text, tools: [], files: attached });

    const apply = painter(messages);
    aborter = new AbortController();
    streaming.value = true;
    try {
      for await (const ev of run(sid, text, {
        signal: aborter.signal,
        enableSearch: enableSearch.value,
        builtinTools: [...builtinTools.value],
        files: attach,
      })) {
        if (ev.event === "start") model.value = ev.data.model;
        else if (ev.event === "turn_end") {
          lastTurn.value = {
            turns: ev.data.turns,
            input: ev.data.usage.input_tokens,
            output: ev.data.usage.output_tokens,
          };
        } else if (ev.event === "error") {
          // Inside an HTTP 200: the run started and then failed. The stream
          // keeps going to `done`, so record it and carry on.
          streamError.value = ev.data.message;
        } else {
          apply(ev);
        }
      }
    } catch (err) {
      // Throwing means the run never started, or the connection dropped. An
      // abort is the user's own doing and is not an error worth showing.
      if (!isAbort(err)) error.value = errorMessage(err);
    } finally {
      streaming.value = false;
      aborter = null;
    }

    // Sent with this turn; the chips disappear from the composer, and the
    // message itself now carries them (reload below re-renders from blocks).
    pendingFiles.value = [];

    // The persisted transcript is the source of truth: it carries full tool
    // arguments and untruncated results, neither of which the stream sends.
    await reload(sid);
    await loadSessions();
  }

  return {
    sessions,
    activeId,
    active,
    messages,
    loading,
    streaming,
    error,
    streamError,
    model,
    lastTurn,
    enableSearch,
    builtinTools,
    files,
    pendingFiles,
    uploading,
    filesError,
    loadSessions,
    startNew,
    selectSession,
    stop,
    clear,
    send,
    toggleTool,
    togglePending,
    loadFiles,
    upload,
  };
});
