import { ref } from "vue";
import { describe, expect, it } from "vitest";
import { painter, toUi } from "../src/stores/chat";
import type { UiMessage } from "../src/stores/chat";
import type { MessageOut, RunEvent } from "../src/api/types";
// Captured from GET /v1/sessions/{id}/messages through the Vite dev proxy, on a
// session whose history was written the same way runner.py writes it: two turns,
// the second calling two tools with one of them denied by policy.
import captured from "./fixtures/tool-transcript.json";

const transcript = captured.messages as MessageOut[];

describe("toUi", () => {
  it("folds the eight stored messages into six renderable ones", () => {
    expect(transcript).toHaveLength(8);
    const ui = toUi(transcript);
    expect(ui.map((m) => m.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
      "assistant",
      "assistant",
    ]);
  });

  it("drops the user messages that carried only tool results", () => {
    // The loop stores results as a *user* message, which would otherwise render
    // as if the human had typed the tool output. Four are stored here: two real
    // prompts and two result carriers.
    const stored = transcript.filter((m) => m.role === "user");
    expect(stored).toHaveLength(4);
    expect(toUi(transcript).filter((m) => m.role === "user")).toHaveLength(2);
  });

  it("attaches each result to the call it answers, with its error state", () => {
    const calls = toUi(transcript).flatMap((m) => m.tools);
    expect(calls.map((c) => [c.id, c.name, c.state])).toEqual([
      ["call_1", "ls", "ok"],
      ["call_2", "read", "ok"],
      ["call_3", "bash", "error"],
    ]);
    expect(calls[2].result).toContain("denied by security policy");
  });

  it("keeps the arguments, which the live stream never sends", () => {
    const calls = toUi(transcript).flatMap((m) => m.tools);
    expect(calls[0].args).toBe('{"path": "."}');
    expect(calls[2].args).toBe('{"command": "wc -l README.md"}');
  });

  it("leaves a call with no stored result marked as still running", () => {
    // That is what an aborted run looks like once #52 is fixed: the call is
    // persisted, its result never was.
    const ui = toUi([
      {
        idx: 0,
        role: "assistant",
        blocks: [{ type: "tool_call", id: "c1", name: "bash", arguments: "{}" }],
      },
    ]);
    expect(ui[0].tools[0].state).toBe("running");
  });

  it("shows an orphan result instead of swallowing it", () => {
    const ui = toUi([
      {
        idx: 0,
        role: "user",
        blocks: [{ type: "tool_result", tool_use_id: "unknown", content: "stray output", is_error: false }],
      },
    ]);
    expect(ui).toHaveLength(1);
    expect(ui[0].text).toBe("stray output");
  });

  it("concatenates several text blocks in one message", () => {
    const ui = toUi([
      {
        idx: 0,
        role: "assistant",
        blocks: [
          { type: "text", text: "first " },
          { type: "text", text: "second" },
        ],
      },
    ]);
    expect(ui[0].text).toBe("first second");
  });
});

describe("toUi plan folding", () => {
  const PLAN = { title: "重构 sessions 表", steps: ["加可空 plan 列", "写迁移 0003"] };

  /**
   * A plan run as the loop persists it: submit_plan plus the batch-mate it cut
   * short, then the user message carrying both results. The call's `arguments`
   * are the only copy of the plan that survives in history - there is no plan
   * block - so the card is reconstructed by parsing them here.
   */
  function planHistory(args: string = JSON.stringify(PLAN), planOk = true): MessageOut[] {
    return [
      {
        idx: 0,
        role: "assistant",
        blocks: [
          { type: "text", text: "我先规划。" },
          { type: "tool_call", id: "c1", name: "submit_plan", arguments: args },
          { type: "tool_call", id: "c2", name: "bash", arguments: '{"command": "rm -rf build"}' },
        ],
      },
      {
        idx: 1,
        role: "user",
        blocks: [
          {
            type: "tool_result",
            tool_use_id: "c1",
            content: planOk ? "Plan recorded (2 steps)." : "Error: invalid plan: 21 steps",
            is_error: !planOk,
          },
          {
            type: "tool_result",
            tool_use_id: "c2",
            content: "Error: skipped - submit_plan ended this turn.",
            is_error: true,
          },
        ],
      },
    ];
  }

  it("folds submit_plan into a card instead of a tool row", () => {
    const ui = toUi(planHistory());
    expect(ui).toHaveLength(1);
    expect(ui[0].plan).toEqual(PLAN);
    // A row of raw JSON under the card that already shows it would be noise.
    expect(ui[0].tools.map((t) => t.name)).toEqual(["bash"]);
  });

  it("absorbs the plan's own result rather than showing it as a stray bubble", () => {
    // It arrives in a *user* message. Unfolded, the transcript would read as if
    // the human had typed "Plan recorded (2 steps)."
    const ui = toUi(planHistory());
    expect(ui).toHaveLength(1);
    expect(ui.filter((m) => m.role === "user")).toHaveLength(0);
  });

  it("keeps the batch-mate that submit_plan cut short, as an error", () => {
    // It really did not run, and hiding that would leave the user guessing
    // whether the rm -rf happened.
    const ui = toUi(planHistory());
    expect(ui[0].tools[0]).toMatchObject({ id: "c2", name: "bash", state: "error" });
    expect(ui[0].tools[0].result).toContain("skipped");
  });

  it("falls back to the raw tool row when the arguments will not parse", () => {
    const ui = toUi(planHistory("{not json"));
    expect(ui[0].plan).toBeUndefined();
    expect(ui[0].tools.map((t) => t.name)).toEqual(["submit_plan", "bash"]);
  });

  it("renders no card for a submit_plan the tool rejected", () => {
    // Nothing was recorded, so a card would advertise a plan that does not
    // exist. The verdict arrives one message *after* the call, which is why the
    // fold has to pre-pass the whole history before it can decide.
    const ui = toUi(planHistory(JSON.stringify(PLAN), false));
    expect(ui[0].plan).toBeUndefined();
    expect(ui[0].tools.map((t) => t.name)).toEqual(["submit_plan", "bash"]);
    expect(ui[0].tools[0].state).toBe("error");
  });
});

describe("painter", () => {
  function stream(events: RunEvent[]): UiMessage[] {
    const messages = ref<UiMessage[]>([]);
    const apply = painter(messages);
    for (const ev of events) apply(ev);
    return messages.value;
  }

  const text = (t: string): RunEvent => ({ event: "text_delta", data: { text: t } });
  const start = (id: string, name: string): RunEvent => ({ event: "toolcall_start", data: { id, name } });
  const end = (id: string, name: string, ok: boolean, result: string): RunEvent => ({
    event: "toolcall_end",
    data: { id, name, ok, result },
  });

  it("concatenates deltas into a single assistant message", () => {
    const out = stream([text("你好！"), text("我是 "), text("pi-py")]);
    expect(out).toHaveLength(1);
    expect(out[0].text).toBe("你好！我是 pi-py");
  });

  it("collapses the repeated toolcall_start frames for one id", () => {
    // The loop emits a start per streamed argument chunk, so one call arrives
    // several times. Treating each frame as a new call would triple the UI.
    const out = stream([start("c1", "bash"), start("c1", "bash"), start("c1", "bash")]);
    expect(out[0].tools).toHaveLength(1);
    expect(out[0].tools[0].state).toBe("running");
  });

  it("keeps a call in the message that asked for it", () => {
    const out = stream([text("我来看看。"), start("c1", "ls"), end("c1", "ls", true, "README.md")]);
    expect(out).toHaveLength(1);
    expect(out[0].text).toBe("我来看看。");
    expect(out[0].tools[0]).toMatchObject({ id: "c1", name: "ls", state: "ok", result: "README.md" });
  });

  it("starts a new assistant message for text after a tool result", () => {
    // There is no message-boundary event on the wire, so this heuristic is the
    // only thing separating "the model talked, called a tool, then talked again"
    // from one long run-on bubble.
    const out = stream([
      text("先列目录。"),
      start("c1", "ls"),
      end("c1", "ls", true, "src"),
      text("里面有一个目录。"),
    ]);
    expect(out.map((m) => m.text)).toEqual(["先列目录。", "里面有一个目录。"]);
    expect(out[0].tools).toHaveLength(1);
    expect(out[1].tools).toHaveLength(0);
  });

  it("does not split on text that arrives before the tool finishes", () => {
    const out = stream([text("a"), start("c1", "ls"), text("b")]);
    expect(out).toHaveLength(1);
    expect(out[0].text).toBe("ab");
  });

  it("records a failed tool without stopping the transcript", () => {
    const out = stream([start("c1", "bash"), end("c1", "bash", false, "denied"), text("换一种方式。")]);
    expect(out[0].tools[0].state).toBe("error");
    expect(out).toHaveLength(2);
  });

  it("inserts a system note on compaction and starts fresh after it", () => {
    const out = stream([
      text("旧内容"),
      { event: "compaction", data: { dropped: 12, chars_before: 90000, chars_after: 4000 } },
      text("新内容"),
    ]);
    expect(out.map((m) => m.role)).toEqual(["assistant", "system", "assistant"]);
    expect(out[1].text).toContain("12");
    expect(out[2].text).toBe("新内容");
  });

  it("gives the plan its own assistant message", () => {
    const out = stream([
      text("我先规划。"),
      start("c1", "submit_plan"),
      end("c1", "submit_plan", true, "Plan recorded (2 steps)."),
      { event: "plan", data: { title: "重构 sessions 表", steps: ["加可空 plan 列", "写迁移 0003"] } },
    ]);
    expect(out.map((m) => m.role)).toEqual(["assistant", "assistant"]);
    expect(out[1].plan).toEqual({ title: "重构 sessions 表", steps: ["加可空 plan 列", "写迁移 0003"] });
    expect(out[0].plan).toBeUndefined();
    expect(out[0].tools.map((t) => t.name)).toEqual(["submit_plan"]);
  });

  it("attaches a result whose start frame was missed", () => {
    // Defensive: a provider that streams no argument deltas would emit the end
    // without a start. Losing the result would be worse than inventing a row.
    const out = stream([end("c9", "read", true, "contents")]);
    expect(out[0].tools[0]).toMatchObject({ id: "c9", name: "read", state: "ok" });
  });
});
