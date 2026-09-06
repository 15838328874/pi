<script setup lang="ts">
import { onMounted, reactive, ref, watch } from "vue";
import {
  NAlert,
  NButton,
  NCard,
  NCheckbox,
  NInput,
  NInputNumber,
  NSwitch,
  NTag,
} from "naive-ui";
import type { AuditRecord, TraceStepOut } from "../api/types";
import { useAdmin } from "../stores/admin";
import { useAuth } from "../stores/auth";
import { useUi } from "../stores/ui";

const admin = useAdmin();
const auth = useAuth();
const ui = useUi();

const TABS = [
  { key: "users", label: "用户" },
  { key: "audit", label: "审计日志" },
  { key: "traces", label: "执行轨迹" },
] as const;

const tab = ref<(typeof TABS)[number]["key"]>("users");

/**
 * Same two-click confirmation as AccountView: revoking a user's tokens is
 * disruptive enough to demand a second click, and an inline armed button needs
 * no popover DOM - which keeps the SSR render tests honest.
 */
const armedRevoke = ref<string | null>(null);

// Filter boxes are plain refs, applied only on 查询: an admin typing a partial
// username should not fire a request per keystroke.
const auditUser = ref("");
const auditEvent = ref("");
const traceUser = ref("");
const traceStatus = ref("");
const traceAnomaly = ref(false);

/**
 * Quota edits are per-row drafts, initialized (and re-initialized after every
 * reload) from the server's value. A save writes the draft, and loadUsers()
 * replaces users with fresh rows, which re-syncs the drafts - so a rejected
 * edit visibly snaps back to the truth.
 */
const quotaDrafts = reactive<Record<string, number>>({});
watch(
  () => admin.users,
  (users) => {
    for (const u of users) quotaDrafts[u.username] = u.quota_tokens;
  },
  { immediate: true },
);

onMounted(() => {
  void admin.loadUsers();
  void admin.loadAudit();
  void admin.loadTraces();
});

function applyAuditFilter(): void {
  void admin.loadAudit({
    user: auditUser.value.trim() || undefined,
    event: auditEvent.value.trim() || undefined,
  });
}

function applyTraceFilter(): void {
  void admin.loadTraces({
    user: traceUser.value.trim() || undefined,
    status: traceStatus.value.trim() || undefined,
    anomaly: traceAnomaly.value,
  });
}

/** Audit records are deliberately untyped (see AuditOut); read them defensively. */
function str(v: unknown): string {
  return typeof v === "string" ? v : v === undefined || v === null ? "" : JSON.stringify(v);
}

/**
 * The actor of an audit row: auth events carry `username`, tool/memory events
 * carry `user` (the user id). Either way it is the one column the admin scans
 * for first, so an unknown shape falls back to "—" rather than an empty gap.
 */
function actor(r: AuditRecord): string {
  const name = str(r.username) || str(r.user);
  return name === "" ? "—" : name;
}

function auditOk(r: AuditRecord): boolean {
  return r.ok !== false;
}

function traceTagType(status: string): "success" | "error" {
  return status === "ok" ? "success" : "error";
}

function fmtMs(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

function fmtDate(iso: string): string {
  return iso === "" ? "" : iso.slice(0, 10);
}

/** One line, at most 40 chars: enough to recognize a run, short enough for a row. */
function promptPreview(p: string): string {
  const line = p.trim().split("\n", 1)[0] ?? "";
  return line.length > 40 ? `${line.slice(0, 40)}…` : line || "—";
}

/** Tool arguments arrive as a JSON string; pretty-print, raw on parse failure. */
function pretty(raw: string): string {
  if (raw === "") return "";
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

function num(v: unknown): number {
  return typeof v === "number" ? v : 0;
}

/**
 * The retrieval step's verdict in one line, lifted out of its JSON detail.
 *
 * "模型不记得我说过的事" is the question this panel exists to answer, and the answer
 * is four numbers plus a reason: what recall returned, what the gates dropped, what
 * was finally injected, and which of the outcomes it was. The full JSON stays
 * underneath for the candidate-by-candidate account.
 */
function retrievalSummary(s: TraceStepOut): string {
  if (s.kind !== "retrieval") return "";
  let parsed: unknown;
  try {
    parsed = JSON.parse(s.detail);
  } catch {
    return "";
  }
  const o = parsed as Record<string, unknown>;
  const parts = [
    str(o.outcome) || "—",
    `召回 ${num(o.recalled)}`,
    `过滤 ${num(o.gated)}`,
    `注入 ${num(o.kept)} 条 / ${num(o.injected_chars)} 字`,
  ];
  if (str(o.index) !== "") parts.push(`来源 ${str(o.index)}`);
  if (str(o.rerank) !== "") parts.push(`重排 ${str(o.rerank)}`);
  if (str(o.error) !== "") parts.push(str(o.error));
  return parts.join(" · ");
}

/** Per-stage latency of a retrieval, when the detail carries it. */
function retrievalStages(s: TraceStepOut): string {
  if (s.kind !== "retrieval") return "";
  let parsed: unknown;
  try {
    parsed = JSON.parse(s.detail);
  } catch {
    return "";
  }
  const stages = (parsed as Record<string, unknown>).stages_ms;
  if (stages === null || typeof stages !== "object") return "";
  return Object.entries(stages as Record<string, unknown>)
    .map(([name, ms]) => `${name} ${fmtMs(num(ms))}`)
    .join(" · ");
}

const ROLE_LABEL: Record<string, string> = {
  user: "用户",
  assistant: "模型",
  system: "系统",
};

/**
 * Step kinds, as the server records them. retrieval is the long-term-memory lookup
 * before the first model call (at most one per run), llm_call is one per model
 * round-trip - both are trace-only, so neither ever appears in the chat stream.
 */
const KIND_LABEL: Record<string, string> = {
  tool_call: "工具调用",
  retrieval: "记忆召回",
  llm_call: "模型调用",
  plan: "计划",
  compaction: "历史压缩",
  error: "错误",
};
</script>

<template>
  <div class="page">
    <header class="topbar">
      <n-button size="small" quaternary @click="ui.go('chat')">← 返回对话</n-button>
      <strong>管理控制台</strong>
      <span class="s-meta">{{ auth.username }}</span>
    </header>

    <n-alert v-if="admin.error" type="error" :bordered="false" class="banner">
      {{ admin.error }}
    </n-alert>

    <!-- Hand-rolled tab bar, not n-tabs: naive-ui's line tabs mount a scroll
         region that touches document during setup, which the SSR render tests
         (and a no-JS first paint) lack. v-show keeps all three panes in the
         DOM - the lists are capped server-side (200/50/50), filter state
         survives tab switches, and the tests can assert on every pane. -->
    <nav class="tabs">
      <button
        v-for="t in TABS"
        :key="t.key"
        type="button"
        class="tab"
        :class="{ active: tab === t.key }"
        @click="tab = t.key"
      >
        {{ t.label }}
      </button>
    </nav>

    <section v-show="tab === 'users'">
      <p v-if="admin.users.length === 0" class="s-meta">加载中…</p>
        <table v-else class="table">
          <thead>
            <tr>
              <th>用户</th>
              <th>角色</th>
              <th>启用</th>
              <th>月度配额（tokens）</th>
              <th>注册于</th>
              <th>令牌</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="u in admin.users" :key="u.id" :class="{ dim: !u.is_active }">
              <td class="mono">{{ u.username }}</td>
              <td>
                <n-tag v-if="u.is_admin" size="small" type="warning" :bordered="false">
                  管理员
                </n-tag>
                <n-tag v-else size="small" :bordered="false">用户</n-tag>
              </td>
              <td>
                <n-switch
                  size="small"
                  :value="u.is_active"
                  :loading="admin.busy"
                  @update:value="(v: boolean) => admin.setUserActive(u.username, v)"
                />
              </td>
              <td class="quota-cell">
                <n-input-number
                  size="small"
                  :value="quotaDrafts[u.username]"
                  :min="0"
                  :step="10000"
                  :show-button="false"
                  class="quota-input"
                  @update:value="(v: number | null) =>
                    v !== null && (quotaDrafts[u.username] = v)"
                />
                <n-button
                  size="tiny"
                  secondary
                  :disabled="quotaDrafts[u.username] === u.quota_tokens"
                  @click="admin.setUserQuota(u.username, quotaDrafts[u.username])"
                >
                  保存
                </n-button>
              </td>
              <td class="s-meta">{{ fmtDate(u.created_at) }}</td>
              <td>
                <n-button
                  v-if="armedRevoke !== u.username"
                  size="tiny"
                  quaternary
                  type="error"
                  @click="armedRevoke = u.username"
                >
                  撤销令牌
                </n-button>
                <n-button
                  v-else
                  size="tiny"
                  type="error"
                  @click="armedRevoke = null; admin.revokeTokens(u.username)"
                >
                  确认撤销
                </n-button>
              </td>
            </tr>
          </tbody>
        </table>
    </section>

    <section v-show="tab === 'audit'">
        <div class="filters">
          <n-input v-model:value="auditUser" size="small" placeholder="按用户过滤" class="filter" />
          <n-input
            v-model:value="auditEvent"
            size="small"
            placeholder="按事件过滤（auth / tool_call / memory…）"
            class="filter"
          />
          <n-button size="small" secondary @click="applyAuditFilter">查询</n-button>
        </div>

        <p v-if="admin.audit.length === 0" class="s-meta">没有匹配的审计记录。</p>
        <div v-else class="audit">
          <div v-for="(r, i) in admin.audit" :key="i" class="audit-row">
            <div class="audit-head">
              <n-tag size="small" :type="auditOk(r) ? 'success' : 'error'" :bordered="false">
                {{ str(r.event) }}
              </n-tag>
              <span class="mono">{{ actor(r) }}</span>
              <span class="s-meta">{{ str(r.ts) }}</span>
            </div>
            <pre class="mono block">{{ JSON.stringify(r, null, 2) }}</pre>
          </div>
        </div>
    </section>

    <section v-show="tab === 'traces'">
        <div class="filters">
          <n-input v-model:value="traceUser" size="small" placeholder="按用户过滤" class="filter" />
          <n-input
            v-model:value="traceStatus"
            size="small"
            placeholder="状态（ok / error / timeout）"
            class="filter"
          />
          <n-checkbox v-model:checked="traceAnomaly" size="small">只看异常</n-checkbox>
          <n-button size="small" secondary @click="applyTraceFilter">查询</n-button>
        </div>

        <p v-if="admin.runs.length === 0" class="s-meta">没有匹配的执行轨迹。</p>
        <table v-else class="table">
          <thead>
            <tr>
              <th>run</th>
              <th>用户</th>
              <th>输入</th>
              <th>模型</th>
              <th>状态</th>
              <th>轮次</th>
              <th>tokens</th>
              <th>耗时</th>
              <th>标记</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in admin.runs" :key="r.run_id">
              <td>
                <n-button text size="small" class="mono" @click="admin.selectTrace(r.run_id)">
                  {{ r.run_id }}
                </n-button>
              </td>
              <td class="mono">{{ r.username }}</td>
              <td class="prompt-cell">{{ promptPreview(r.prompt) }}</td>
              <td class="mono">{{ r.model }}</td>
              <td>
                <n-tag size="small" :type="traceTagType(r.status)" :bordered="false">
                  {{ r.status }}
                </n-tag>
              </td>
              <td>{{ r.turns }}</td>
              <td>{{ r.input_tokens }} / {{ r.output_tokens }}</td>
              <td>{{ fmtMs(r.duration_ms) }}</td>
              <td>
                <n-tag
                  v-for="f in r.flags"
                  :key="f"
                  size="small"
                  type="warning"
                  :bordered="false"
                  class="flag"
                >
                  {{ f }}
                </n-tag>
              </td>
            </tr>
          </tbody>
        </table>

        <n-card v-if="admin.detail !== null" title="轨迹详情" size="small" class="detail">
          <template #header-extra>
            <n-tag size="small" :type="traceTagType(admin.detail.status)" :bordered="false">
              {{ admin.detail.status }}
            </n-tag>
          </template>
          <p class="s-meta">
            会话 {{ admin.detail.session_id }} · {{ admin.detail.username }} ·
            {{ admin.detail.turns }} 轮 · 失败工具 {{ admin.detail.failed_tools }} 次 ·
            {{ fmtMs(admin.detail.duration_ms) }} ·
            请求 <span class="mono">{{ admin.detail.request_id || "—" }}</span>
          </p>
          <div
            v-if="admin.detail.enable_search || (admin.detail.builtin_tools ?? []).length > 0"
            class="cap-row"
          >
            <n-tag v-if="admin.detail.enable_search" size="small" type="info" :bordered="false" class="flag">
              联网搜索
            </n-tag>
            <n-tag
              v-for="t in admin.detail.builtin_tools ?? []"
              :key="t"
              size="small"
              type="info"
              :bordered="false"
              class="flag"
            >
              {{ t }}
            </n-tag>
          </div>
          <template v-if="admin.detail.prompt !== ''">
            <div class="sub-title">输入</div>
            <pre class="mono block">{{ admin.detail.prompt }}</pre>
          </template>
          <n-alert
            v-if="admin.detail.error !== ''"
            type="error"
            :bordered="false"
            class="detail-error"
          >
            {{ admin.detail.error }}
          </n-alert>

          <div class="sub-title">步骤</div>
          <p v-if="admin.detail.steps == null || admin.detail.steps.length === 0" class="s-meta">
            没有记录的步骤（早于步骤记录的旧轨迹，或这次 run 在第一次模型调用之前就结束了）。
          </p>
          <table v-else class="table">
            <thead>
              <tr>
                <th>#</th>
                <th>类型</th>
                <th>名称</th>
                <th>结果</th>
                <th>耗时</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="s in admin.detail.steps" :key="s.seq">
                <td>{{ s.seq }}</td>
                <td>{{ KIND_LABEL[s.kind] ?? s.kind }}</td>
                <td class="mono">{{ s.name }}</td>
                <td>
                  <n-tag size="small" :type="s.ok ? 'success' : 'error'" :bordered="false">
                    {{ s.ok ? "成功" : "失败" }}
                  </n-tag>
                </td>
                <td>{{ fmtMs(s.duration_ms) }}</td>
              </tr>
            </tbody>
          </table>
          <div v-for="s in admin.detail.steps ?? []" :key="`d-${s.seq}`" class="step-detail">
            <div class="s-meta">
              #{{ s.seq }} · {{ KIND_LABEL[s.kind] ?? s.kind }} ·
              <span class="mono">{{ s.name }}</span>
            </div>
            <template v-if="retrievalSummary(s) !== ''">
              <div class="s-meta">{{ retrievalSummary(s) }}</div>
              <div v-if="retrievalStages(s) !== ''" class="s-meta">
                阶段耗时 {{ retrievalStages(s) }}
              </div>
            </template>
            <div v-if="s.args !== ''" class="s-meta">{{ s.kind === "retrieval" ? "查询" : "参数" }}</div>
            <pre v-if="s.args !== ''" class="mono block">{{ pretty(s.args) }}</pre>
            <div v-if="s.detail !== ''" class="s-meta">结果</div>
            <pre v-if="s.detail !== ''" class="mono block">{{ pretty(s.detail) }}</pre>
          </div>

          <template v-if="(admin.detail.messages ?? []).length > 0">
            <div class="sub-title">
              对话回放（第 {{ admin.detail.first_idx }}–{{ admin.detail.last_idx }} 条）
            </div>
            <div v-for="m in admin.detail.messages ?? []" :key="`m-${m.idx}`" class="msg">
              <div class="msg-head">
                <span class="msg-role">{{ ROLE_LABEL[m.role] ?? m.role }}</span>
                <span class="s-meta mono">#{{ m.idx }}</span>
              </div>
              <div v-for="(b, i) in m.blocks" :key="i" class="msg-body">
                <pre v-if="b.type === 'text'" class="mono block">{{ b.text }}</pre>
                <template v-else-if="b.type === 'tool_call'">
                  <div class="s-meta">
                    工具调用 · <span class="mono">{{ b.name }}</span> ·
                    <span class="mono">{{ b.id }}</span>
                  </div>
                  <pre class="mono block">{{ pretty(b.arguments) }}</pre>
                </template>
                <template v-else-if="b.type === 'tool_result'">
                  <div class="s-meta">工具结果 · <span class="mono">{{ b.tool_use_id }}</span></div>
                  <pre class="mono block" :class="{ 'block-error': b.is_error }">
{{ b.content === "" ? "（由网关执行，无返回内容）" : b.content }}</pre>
                </template>
                <template v-else-if="b.type === 'file'">
                  <div class="s-meta">附件 · <span class="mono">{{ b.name }}</span></div>
                  <pre class="mono block">{{ b.file_url }}</pre>
                </template>
              </div>
            </div>
          </template>
        </n-card>
    </section>
  </div>
</template>

<style scoped>
.page {
  height: 100%;
  overflow-y: auto;
  padding: 16px;
  max-width: 980px;
  margin: 0 auto;
  display: grid;
  grid-template-rows: auto auto 1fr;
  gap: 12px;
}

.topbar {
  display: flex;
  align-items: center;
  gap: 12px;
}

.tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid rgba(128, 128, 128, 0.25);
}

.tab {
  padding: 6px 14px;
  border: none;
  border-bottom: 2px solid transparent;
  background: none;
  font: inherit;
  font-size: 14px;
  cursor: pointer;
  opacity: 0.65;
}

.tab.active {
  opacity: 1;
  border-bottom-color: #18a058;
}

.banner {
  max-width: none;
}

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.table th,
.table td {
  text-align: left;
  padding: 6px 10px 6px 0;
  border-bottom: 1px solid rgba(128, 128, 128, 0.2);
  vertical-align: middle;
}

.dim {
  opacity: 0.55;
}

.quota-cell {
  display: flex;
  align-items: center;
  gap: 6px;
}

.quota-input {
  width: 110px;
}

.filters {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.filter {
  width: 200px;
}

.audit {
  display: grid;
  gap: 10px;
}

.audit-row {
  display: grid;
  gap: 4px;
}

.audit-head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.detail {
  margin-top: 14px;
}

.detail-error {
  margin-top: 8px;
}

.sub-title {
  margin: 14px 0 6px;
  font-size: 13px;
  font-weight: 600;
}

.cap-row {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin: 8px 0 2px;
}

.prompt-cell {
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.step-detail {
  margin-top: 8px;
  display: grid;
  gap: 4px;
}

.msg {
  margin-top: 10px;
  display: grid;
  gap: 4px;
}

.msg-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.msg-role {
  font-size: 12px;
  font-weight: 600;
}

.msg-body {
  display: grid;
  gap: 4px;
}

.block-error {
  background: rgba(218, 65, 65, 0.12);
}

.block {
  margin: 0;
  padding: 8px;
  border-radius: 4px;
  background: rgba(128, 128, 128, 0.12);
  font-size: 12px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

.s-meta {
  font-size: 12px;
  opacity: 0.6;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}
</style>
