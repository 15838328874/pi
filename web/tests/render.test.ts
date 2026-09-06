import { createSSRApp } from "vue";
import type { Component } from "vue";
import { renderToString } from "vue/server-renderer";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it } from "vitest";
import App from "../src/App.vue";
import AccountView from "../src/views/AccountView.vue";
import AdminView from "../src/views/AdminView.vue";
import ChatView from "../src/views/ChatView.vue";
import LoginView from "../src/views/LoginView.vue";
import { tokenStore } from "../src/api/client";
import { useAccount } from "../src/stores/account";
import { useAdmin } from "../src/stores/admin";
import { useAuth } from "../src/stores/auth";
import { useChat } from "../src/stores/chat";
import { useUi } from "../src/stores/ui";
import type { UiMessage } from "../src/stores/chat";

/**
 * Render smoke tests, via vue's server renderer.
 *
 * No browser and no server needed: the point is to prove the templates actually
 * produce markup from store state. A binding typo, a wrong prop name or a
 * component that throws during setup all pass vue-tsc and the bundler, then show
 * up as a blank page - which is exactly what cannot be caught by hand here.
 *
 * What these do NOT prove is layout or appearance. Nothing in this repo can.
 */

const storage = new Map<string, string>();

beforeEach(() => {
  // auth.ts reads sessionStorage when the store is first created, so it has to
  // exist before any useAuth() call.
  if (!("sessionStorage" in globalThis)) {
    Object.defineProperty(globalThis, "sessionStorage", {
      configurable: true,
      value: {
        getItem: (k: string) => storage.get(k) ?? null,
        setItem: (k: string, v: string) => void storage.set(k, v),
        removeItem: (k: string) => void storage.delete(k),
      },
    });
  }
  storage.clear();
  setActivePinia(createPinia());
});

async function render(component: Component): Promise<string> {
  return renderToString(createSSRApp(component));
}

/** signedIn is a computed over BOTH the token and the username. */
function signIn(username: string, admin = false): void {
  tokenStore.set("a-token");
  const auth = useAuth();
  auth.restored = true;
  auth.username = username;
  auth.isAdmin = admin;
}

const TRANSCRIPT: UiMessage[] = [
  { role: "user", text: "看一下当前目录", tools: [], files: [] },
  {
    role: "assistant",
    text: "我来列一下目录。",
    tools: [{ id: "c1", name: "ls", args: '{"path": "."}', state: "ok", result: "README.md\nsrc" }],
    files: [],
  },
  { role: "system", text: "已压缩历史：12 条消息被摘要替代", tools: [], files: [] },
  {
    role: "assistant",
    text: "",
    tools: [{ id: "c2", name: "bash", args: "", state: "error", result: "denied by security policy" }],
    files: [],
  },
  {
    role: "assistant",
    text: "",
    tools: [],
    files: [],
    plan: { title: "给 sessions 加 plan 列", steps: ["写迁移 0003", "补 DDL 可移植性测试"] },
  },
];

describe("App.vue screen selection", () => {
  it("shows a spinner until the stored token has been checked", async () => {
    const auth = useAuth();
    auth.restored = false;
    const html = await render(App);
    expect(html).toContain("n-spin");
    expect(html).not.toContain("登录");
  });

  it("shows the login form when there is no session", async () => {
    useAuth().restored = true;
    const html = await render(App);
    expect(html).toContain("登录");
    expect(html).not.toContain("新对话");
  });

  it("shows the login form when a username is set but the token is gone", async () => {
    // The half-signed-in state a 401 during restore() leaves behind.
    const auth = useAuth();
    auth.restored = true;
    auth.username = "alice";
    const html = await render(App);
    expect(html).toContain("登录");
    expect(html).not.toContain("新对话");
  });

  it("shows the chat once signed in", async () => {
    signIn("alice");
    const html = await render(App);
    expect(html).toContain("新对话");
    expect(html).toContain("alice");
    expect(html).not.toContain("注册并登录");
  });

  it("offers the admin console in the sidebar only to admins", async () => {
    signIn("alice");
    const html = await render(ChatView);
    expect(html).toContain("账号与用量");
    expect(html).not.toContain("管理控制台");

    signIn("admin", true);
    const adminHtml = await render(ChatView);
    expect(adminHtml).toContain("管理控制台");
  });

  it("switches to the account screen when selected", async () => {
    signIn("alice");
    useUi().screen = "account";
    const html = await render(App);
    expect(html).toContain("账号与用量");
    expect(html).toContain("长期记忆");
    expect(html).not.toContain("新对话");
  });

  it("switches to the admin screen only for admins", async () => {
    signIn("admin", true);
    useUi().screen = "admin";
    const html = await render(App);
    expect(html).toContain("管理控制台");
    expect(html).toContain("审计日志");
    expect(html).toContain("执行轨迹");
    expect(html).not.toContain("新对话");
  });

  it("falls back to chat when a non-admin lands on the admin screen", async () => {
    // Not reachable by clicking (the button is admin-only), but the store is
    // just a string: a stale value must not hand the console to a non-admin.
    signIn("alice");
    useUi().screen = "admin";
    const html = await render(App);
    expect(html).toContain("新对话");
    expect(html).not.toContain("审计日志");
  });
});

describe("LoginView.vue", () => {
  it("renders both modes, the fields and a submit button", async () => {
    const html = await render(LoginView);
    for (const needle of ["登录", "注册", "用户名", "密码", "pi-py"]) {
      expect(html, needle).toContain(needle);
    }
    // attr-type="submit" is what makes Enter inside the form work.
    expect(html).toContain('type="submit"');
  });

  it("surfaces a failed login as an alert, request id included", async () => {
    useAuth().error = "invalid credentials（request 1bcb7a83b7ad）";
    const html = await render(LoginView);
    expect(html).toContain("invalid credentials");
    expect(html).toContain("1bcb7a83b7ad");
  });

  it("disables submit while the fields are empty", async () => {
    const html = await render(LoginView);
    expect(html).toContain("disabled");
  });
});

describe("ChatView.vue", () => {
  it("renders the transcript, the tool calls and their states", async () => {
    signIn("alice");
    const chat = useChat();
    chat.messages = TRANSCRIPT;
    chat.activeId = "s1";
    chat.sessions = [
      { id: "s1", title: "看一下当前目录", model: "fake/demo", created_at: "2026-09-04T00:00:00+00:00" },
    ];

    const html = await render(ChatView);
    expect(html).toContain("看一下当前目录");
    expect(html).toContain("我来列一下目录。");
    expect(html).toContain("alice");
    // Tool names, their states, the arguments the live stream never carries, and
    // the stored result - all of which only reach the screen through this template.
    expect(html).toContain("ls");
    expect(html).toContain("完成");
    expect(html).toContain("README.md");
    expect(html).toContain("bash");
    expect(html).toContain("失败");
    expect(html).toContain("denied by security policy");
    expect(html).toContain("path");
    expect(html).toContain("已压缩历史");
  });

  it("renders the plan card with its title and ordered steps", async () => {
    signIn("alice");
    const chat = useChat();
    chat.messages = TRANSCRIPT;
    chat.activeId = "s1";
    const html = await render(ChatView);
    expect(html).toContain("任务规划");
    expect(html).toContain("给 sessions 加 plan 列");
    expect(html).toContain("写迁移 0003");
    expect(html).toContain("补 DDL 可移植性测试");
    // Ordered, because the steps are a sequence and the numbering is the only
    // thing on the page that says so.
    expect(html).toContain("<ol");
    expect(html).toContain("<li");
  });

  it("renders the empty state for a brand new conversation", async () => {
    signIn("alice");
    const html = await render(ChatView);
    expect(html).toContain("输入内容开始新对话");
    expect(html).not.toContain("正在思考");
    // The positive half of the streaming test below: idle shows a primary send
    // button, so its absence there means something, not just a renamed class.
    expect(html).toContain("发送");
    expect(html).toContain("n-button--primary-type");
    expect(html).not.toContain("停止");
  });

  it("shows the mid-turn failure that arrives inside an HTTP 200", async () => {
    signIn("alice");
    useChat().streamError = "run timed out after 600s";
    const html = await render(ChatView);
    expect(html).toContain("本轮执行中断");
    expect(html).toContain("run timed out after 600s");
  });

  it("shows the last turn's token counts", async () => {
    signIn("alice");
    useChat().lastTurn = { turns: 3, input: 1200, output: 340 };
    const html = await render(ChatView);
    expect(html).toContain("3 轮");
    expect(html).toContain("1200");
    expect(html).toContain("340");
  });

  it("offers stop instead of send while a run is streaming", async () => {
    signIn("alice");
    const chat = useChat();
    chat.streaming = true;
    chat.messages = [{ role: "assistant", text: "", tools: [], files: [] }];
    const html = await render(ChatView);
    expect(html).toContain("停止");
    expect(html).toContain("正在思考");
    // "发送" is also in the placeholder, so check for the send button itself:
    // it is the view's only primary button, and streaming swaps it for stop.
    expect(html).not.toContain("n-button--primary-type");
    expect(html).toContain("disabled");
  });
});

describe("AccountView.vue", () => {
  it("renders the usage panel with month, quota, model rows and cost", async () => {
    signIn("alice");
    const account = useAccount();
    account.usage = {
      month: "2026-09",
      models: [
        {
          model: "openai/qwen-flash",
          input_tokens: 1200,
          output_tokens: 340,
          est_cost_usd: 0.0021,
          runs: 3,
        },
      ],
      total_input_tokens: 1200,
      total_output_tokens: 340,
      total_est_cost_usd: 0.0021,
      quota_tokens: 2_000_000,
      used_tokens: 1540,
    };
    const html = await render(AccountView);
    expect(html).toContain("2026-09");
    expect(html).toContain("openai/qwen-flash");
    expect(html).toContain("0.0021");
    // The quota line is the one place the user learns they are near the limit.
    expect(html).toContain("200.0 万");
    expect(html).toContain("1540");
  });

  it("renders memory status, the facts with kinds, delete and clear-all", async () => {
    signIn("alice");
    const account = useAccount();
    account.memoryStatus = "ready";
    account.facts = [
      {
        id: 1,
        text: "项目用 uv 管理 Python 依赖",
        kind: "convention",
        source_session: "sess-aaaaaaaa1111",
        created_at: "2026-09-04T00:00:00+00:00",
      },
      {
        id: 2,
        text: "回复保持简洁",
        kind: "preference",
        source_session: "sess-bbbbbbbb2222",
        created_at: "2026-09-05T00:00:00+00:00",
      },
    ];
    const html = await render(AccountView);
    expect(html).toContain("记忆已启用");
    expect(html).toContain("项目用 uv 管理 Python 依赖");
    expect(html).toContain("约定");
    expect(html).toContain("偏好");
    expect(html).toContain("删除");
    expect(html).toContain("清空全部记忆");
  });

  it("says the memory backend is down rather than pretending it is off", async () => {
    signIn("alice");
    const account = useAccount();
    account.memoryStatus = "unavailable: milvus unreachable";
    const html = await render(AccountView);
    expect(html).toContain("unavailable: milvus unreachable");
  });

  it("renders the deregistration danger zone with a password field", async () => {
    signIn("alice");
    const html = await render(AccountView);
    expect(html).toContain("注销账号");
    expect(html).toContain("密码");
    expect(html).toContain("永久删除");
  });
});

describe("AdminView.vue", () => {
  it("renders the user table with roles, quota inputs and token revocation", async () => {
    signIn("admin", true);
    const admin = useAdmin();
    admin.users = [
      {
        id: 1,
        username: "admin",
        is_admin: true,
        is_active: true,
        quota_tokens: 5_000_000,
        created_at: "2026-08-01T00:00:00+00:00",
      },
      {
        id: 2,
        username: "alice",
        is_admin: false,
        is_active: false,
        quota_tokens: 1_000_000,
        created_at: "2026-09-01T00:00:00+00:00",
      },
    ];
    const html = await render(AdminView);
    expect(html).toContain("管理员");
    expect(html).toContain("alice");
    expect(html).toContain("撤销");
    expect(html).toContain("保存");
    expect(html).toContain("月度配额");
  });

  it("renders audit records with an event tag and their verbatim JSON", async () => {
    signIn("admin", true);
    const admin = useAdmin();
    // auth events carry `username`, tool/memory events carry `user`: the row
    // must show whichever one is present.
    admin.audit = [
      { ts: "2026-09-05T10:00:00+00:00", username: "alice", event: "auth", ok: true, action: "login" },
      { ts: "2026-09-05T10:05:00+00:00", user: "42", event: "tool_call", ok: false, tool: "bash" },
    ];
    const html = await render(AdminView);
    expect(html).toContain("tool_call");
    expect(html).toContain("alice");
    expect(html).toContain("42");
    expect(html).toContain("login");
    expect(html).toContain("bash");
  });

  it("renders run rows, anomaly flags and the selected run's steps", async () => {
    signIn("admin", true);
    const admin = useAdmin();
    admin.runs = [
      {
        run_id: "abc123def456",
        username: "alice",
        session_id: "s1",
        model: "openai/qwen-flash",
        status: "ok",
        error: "",
        prompt: "列出当前目录",
        request_id: "req000001",
        enable_search: false,
        input_tokens: 1200,
        output_tokens: 340,
        turns: 3,
        failed_tools: 0,
        duration_ms: 4500,
        flags: [],
        started_at: "2026-09-05T09:00:00+00:00",
        ended_at: "2026-09-05T09:00:05+00:00",
      },
      {
        run_id: "fff000fff000",
        username: "bob",
        session_id: "s2",
        model: "openai/qwen-flash",
        status: "error",
        error: "boom",
        prompt: "",
        request_id: "req000002",
        enable_search: true,
        input_tokens: 10,
        output_tokens: 0,
        turns: 0,
        failed_tools: 3,
        duration_ms: 300,
        flags: ["error", "empty", "tool_storm"],
        started_at: "2026-09-05T09:10:00+00:00",
        ended_at: "",
      },
    ];
    admin.detail = {
      ...admin.runs[0],
      builtin_tools: ["web_search"],
      first_idx: 0,
      last_idx: 2,
      steps: [
        { seq: 0, kind: "tool_call", name: "bash", ok: true, args: '{"cmd":"ls"}', detail: "README.md src", duration_ms: 120, ts: "" },
        { seq: 1, kind: "plan", name: "submit_plan", ok: true, args: "", detail: "", duration_ms: 5, ts: "" },
      ],
      messages: [
        { idx: 0, role: "user", blocks: [{ type: "text", text: "列出当前目录" }] },
        { idx: 2, role: "assistant", blocks: [{ type: "tool_call", id: "c1", name: "bash", arguments: '{"cmd":"ls"}' }] },
      ],
    };
    const html = await render(AdminView);
    expect(html).toContain("abc123def456");
    expect(html).toContain("tool_storm");
    expect(html).toContain("轨迹详情");
    expect(html).toContain("bash");
    expect(html).toContain("submit_plan");
    expect(html).toContain("req000001");
    expect(html).toContain("列出当前目录");
    expect(html).toContain("web_search");
    expect(html).toContain("对话回放");
    expect(html).toContain("工具调用");
  });

  it("explains a step-less detail instead of an empty table", async () => {
    signIn("admin", true);
    const admin = useAdmin();
    admin.runs = [
      {
        run_id: "abc123def456",
        username: "alice",
        session_id: "s1",
        model: "openai/qwen-flash",
        status: "ok",
        error: "",
        prompt: "",
        request_id: "",
        enable_search: false,
        input_tokens: 1200,
        output_tokens: 340,
        turns: 3,
        failed_tools: 0,
        duration_ms: 4500,
        flags: [],
        started_at: "2026-09-05T09:00:00+00:00",
        ended_at: "2026-09-05T09:00:05+00:00",
      },
    ];
    admin.detail = { ...admin.runs[0], steps: [] };
    const html = await render(AdminView);
    expect(html).toContain("没有记录的步骤");
  });

  it("renders a retrieval step as a memory verdict rather than a blob of JSON", async () => {
    signIn("admin", true);
    const admin = useAdmin();
    admin.runs = [
      {
        run_id: "abc123def456",
        username: "alice",
        session_id: "s1",
        model: "openai/qwen-flash",
        status: "ok",
        error: "",
        prompt: "uv 怎么装依赖",
        request_id: "req000001",
        enable_search: false,
        input_tokens: 1200,
        output_tokens: 340,
        turns: 1,
        failed_tools: 0,
        duration_ms: 5900,
        flags: ["memory_failed"],
        started_at: "2026-09-06T09:00:00+00:00",
        ended_at: "2026-09-06T09:00:06+00:00",
      },
    ];
    admin.detail = {
      ...admin.runs[0],
      first_idx: 0,
      last_idx: 1,
      steps: [
        {
          seq: 0,
          kind: "retrieval",
          name: "memory.retrieve",
          ok: false,
          args: "uv 怎么装依赖",
          detail: JSON.stringify({
            enabled: true,
            ok: false,
            outcome: "embed_failed",
            stage: "embed",
            error: "ConnectionError: embedding endpoint is down",
            index: "",
            query: "uv 怎么装依赖",
            min_similarity: 0.35,
            recalled: 0,
            gated: 0,
            reranked: 0,
            rerank: "",
            kept: 0,
            injected_chars: 0,
            candidates: [],
            kept_facts: [],
            stages_ms: { embed: 5012.3 },
            text: "",
          }),
          duration_ms: 5012.3,
          ts: "",
        },
        {
          seq: 1,
          kind: "llm_call",
          name: "qwen-flash",
          ok: true,
          args: "",
          detail: JSON.stringify({
            turn: 1,
            model: "qwen-flash",
            stop_reason: "end_turn",
            input_tokens: 1200,
            output_tokens: 340,
            duration_ms: 900,
            error: "",
          }),
          duration_ms: 900,
          ts: "",
        },
      ],
      messages: [],
    };
    const html = await render(AdminView);
    // Kind labels, not the raw enum from the database.
    expect(html).toContain("记忆召回");
    expect(html).toContain("模型调用");
    expect(html).toContain("memory_failed");
    // The verdict and the counts lifted out of the JSON, which is the whole point:
    // this is what answers "模型为什么不记得" without reading the blob below it.
    expect(html).toContain("embed_failed");
    expect(html).toContain("注入 0 条 / 0 字");
    expect(html).toContain("ConnectionError: embedding endpoint is down");
    expect(html).toContain("阶段耗时");
    expect(html).toContain("5.0s");
    // args are the query for a retrieval, and the detail is pretty-printed.
    expect(html).toContain("查询");
    expect(html).toContain("min_similarity");
  });
});
