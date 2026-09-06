<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { NAlert, NButton, NEmpty, NInput, NSpin, NTag } from "naive-ui";
import { useAuth } from "../stores/auth";
import { useChat } from "../stores/chat";
import { useUi } from "../stores/ui";
import type { UiToolCall } from "../stores/chat";
import type { BuiltinTool } from "../api/types";

const auth = useAuth();
const chat = useChat();
const ui = useUi();

const draft = ref("");
const scroller = ref<HTMLElement | null>(null);
/** Follow the stream only while the reader is already at the bottom. */
const pinned = ref(true);
/** Session files panel visibility; the composer always shows pending chips. */
const showFiles = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);

/** Gateway-executed tools offered to the model, user-toggled per run. */
const TOOLS: { key: BuiltinTool; label: string }[] = [
  { key: "web_search", label: "网页搜索" },
  { key: "web_extractor", label: "网页抓取" },
  { key: "code_interpreter", label: "代码解释器" },
];

onMounted(() => {
  void chat.loadSessions();
});

// Cheap change signal: it re-evaluates only when the message count or the tail
// message's text changes, not on every unrelated reactive read. A deep watch on
// the whole transcript would re-traverse it for every 16-character delta.
const tail = computed(() => {
  const n = chat.messages.length;
  const last = n === 0 ? null : chat.messages[n - 1];
  return `${n}:${last === null ? "" : last.text.length + last.tools.length}`;
});

watch(tail, () => {
  if (pinned.value) void nextTick(scrollToBottom);
});

watch(
  () => chat.activeId,
  () => {
    pinned.value = true;
    void nextTick(scrollToBottom);
  },
);

function scrollToBottom(): void {
  const el = scroller.value;
  if (el !== null) el.scrollTop = el.scrollHeight;
}

function onScroll(): void {
  const el = scroller.value;
  if (el === null) return;
  pinned.value = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
}

async function send(): Promise<void> {
  const text = draft.value;
  if (text.trim() === "" || chat.streaming) return;
  draft.value = "";
  pinned.value = true;
  await chat.send(text);
  // Hand it back if the run never started (429 rate limit, 402 quota, 404) so
  // the user can retry without retyping.
  if (chat.error !== "") draft.value = text;
}

/**
 * Enter sends, Shift+Enter breaks the line. `isComposing` is the part that
 * matters here: confirming a Chinese IME candidate with Enter must not fire the
 * message, and composition keydowns arrive with key === "Enter".
 */
function onKeydown(e: KeyboardEvent): void {
  if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
  e.preventDefault();
  void send();
}

async function signOut(): Promise<void> {
  // Stop the stream now: the sign-out round trip takes a moment, and App.vue's
  // watcher clears the store only when signedIn actually flips.
  chat.stop();
  await auth.signOut();
}

function tagType(state: UiToolCall["state"]): "info" | "success" | "error" {
  if (state === "running") return "info";
  return state === "ok" ? "success" : "error";
}

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function pickFiles(): void {
  fileInput.value?.click();
}

async function onFilesChosen(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement;
  const list = Array.from(input.files ?? []);
  input.value = "";
  if (list.length > 0) {
    showFiles.value = true;
    await chat.upload(list);
  }
}

const STATE_TEXT: Record<UiToolCall["state"], string> = {
  running: "执行中",
  ok: "完成",
  error: "失败",
};

/**
 * Tool arguments arrive as a JSON string from the model. Pretty-print them, but
 * show the raw text when they do not parse - a truncated or malformed argument
 * string is still evidence the user should see.
 */
function prettyArgs(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

const isThinking = computed(() => {
  if (!chat.streaming || chat.messages.length === 0) return false;
  return chat.messages[chat.messages.length - 1].role === "assistant";
});
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <n-button block secondary @click="chat.startNew()">＋ 新对话</n-button>

      <div class="sessions">
        <n-empty v-if="chat.sessions.length === 0" description="还没有会话" size="small" />
        <button
          v-for="s in chat.sessions"
          :key="s.id"
          type="button"
          class="session"
          :class="{ active: s.id === chat.activeId }"
          :disabled="chat.streaming"
          @click="chat.selectSession(s.id)"
        >
          <span class="s-title">{{ s.title }}</span>
          <span class="s-meta">{{ s.model }}</span>
        </button>
      </div>

      <div class="nav">
        <n-button block secondary size="small" @click="ui.go('account')">账号与用量</n-button>
        <n-button
          v-if="auth.isAdmin"
          block
          secondary
          size="small"
          @click="ui.go('admin')"
        >
          管理控制台
        </n-button>
      </div>

      <footer class="account">
        <span class="s-meta">{{ auth.username }}</span>
        <n-button text size="small" :loading="auth.busy" @click="signOut">退出</n-button>
      </footer>
    </aside>

    <main class="main">
      <header class="topbar">
        <div class="topbar-title">
          <strong>{{ chat.active?.title ?? "新对话" }}</strong>
          <span v-if="chat.model" class="s-meta">{{ chat.model }}</span>
        </div>
        <div class="topbar-actions">
          <span v-if="chat.lastTurn !== null" class="s-meta">
            {{ chat.lastTurn.turns }} 轮 · 输入 {{ chat.lastTurn.input }} / 输出
            {{ chat.lastTurn.output }} tokens
          </span>
          <n-button
            size="tiny"
            quaternary
            :disabled="chat.activeId === null"
            @click="showFiles = !showFiles"
          >
            文件
          </n-button>
        </div>
      </header>

      <div class="banners">
        <n-alert
          v-if="chat.error !== ''"
          :key="chat.error"
          type="error"
          :bordered="false"
          closable
        >
          {{ chat.error }}
        </n-alert>
        <n-alert
          v-if="chat.streamError !== ''"
          :key="chat.streamError"
          type="warning"
          :bordered="false"
          closable
        >
          本轮执行中断：{{ chat.streamError }}
        </n-alert>
        <n-alert
          v-if="chat.filesError !== ''"
          :key="chat.filesError"
          type="error"
          :bordered="false"
          closable
          @close="chat.filesError = ''"
        >
          {{ chat.filesError }}
        </n-alert>

        <div v-if="showFiles && chat.activeId !== null" class="files-panel">
          <div class="files-head">
            <strong>会话文件</strong>
            <span class="s-meta">{{ chat.files.length }} 个 · 勾选后随下一条消息发给模型</span>
            <n-button
              size="tiny"
              quaternary
              :loading="chat.uploading"
              @click="pickFiles"
            >
              上传
            </n-button>
          </div>
          <n-empty
            v-if="chat.files.length === 0 && !chat.uploading"
            description="尚无文件，点「上传」添加"
            size="small"
          />
          <div v-else class="files-list">
            <div v-for="f in chat.files" :key="f.name" class="file-row">
              <label class="file-check">
                <input
                  type="checkbox"
                  :checked="chat.pendingFiles.includes(f.name)"
                  @change="chat.togglePending(f.name)"
                />
                <span class="file-name">{{ f.name }}</span>
              </label>
              <span class="s-meta">{{ fmtSize(f.size) }}</span>
              <a :href="f.url" target="_blank" rel="noopener" class="s-meta">下载</a>
            </div>
          </div>
        </div>
      </div>

      <div ref="scroller" class="transcript" @scroll="onScroll">
        <n-empty
          v-if="chat.messages.length === 0"
          description="输入内容开始新对话"
          class="empty"
        />
        <n-spin v-else-if="chat.loading" class="empty" />

        <article v-for="(m, i) in chat.messages" :key="i" :class="['msg', m.role]">
          <div v-if="m.role === 'system'" class="sysnote">{{ m.text }}</div>
          <template v-else>
            <div class="who">{{ m.role === "user" ? auth.username : "assistant" }}</div>
            <div v-if="m.text !== ''" class="bubble pre-wrap">{{ m.text }}</div>
            <div v-if="m.files.length > 0" class="files">
              <a
                v-for="f in m.files"
                :key="f.url"
                :href="f.url"
                target="_blank"
                rel="noopener"
                class="file-chip"
              >
                附件 {{ f.name }}
              </a>
            </div>
            <div v-if="m.plan !== undefined" class="plan">
              <strong>任务规划：{{ m.plan.title }}</strong>
              <ol class="plan-steps">
                <li v-for="(s, i) in m.plan.steps" :key="i">{{ s }}</li>
              </ol>
            </div>
            <div v-for="t in m.tools" :key="t.id" class="tool">
              <div class="tool-head">
                <n-tag :type="tagType(t.state)" size="small" :bordered="false">
                  {{ t.name }}
                </n-tag>
                <span class="s-meta">{{ STATE_TEXT[t.state] }}</span>
              </div>
              <!-- Empty while streaming: the SSE contract carries no arguments,
                   and the reload after `done` replaces this with the real ones. -->
              <pre v-if="t.args !== ''" class="mono block">{{ prettyArgs(t.args) }}</pre>
              <pre v-if="t.result !== ''" class="mono block result">{{ t.result }}</pre>
            </div>
          </template>
        </article>

        <div v-if="isThinking" class="thinking s-meta">正在思考…</div>
      </div>

      <div class="composer">
        <div class="composer-tools">
          <button
            type="button"
            class="chip"
            :class="{ on: chat.enableSearch }"
            :disabled="chat.streaming"
            title="模型端联网搜索，回答自动引用网络信息"
            @click="chat.enableSearch = !chat.enableSearch"
          >
            联网搜索
          </button>
          <button
            v-for="t in TOOLS"
            :key="t.key"
            type="button"
            class="chip"
            :class="{ on: chat.builtinTools.includes(t.key) }"
            :disabled="chat.streaming"
            :title="`允许模型调用${t.label}（由模型服务端执行）`"
            @click="chat.toggleTool(t.key)"
          >
            {{ t.label }}
          </button>
          <span class="spacer" />
          <n-button
            size="tiny"
            quaternary
            :disabled="chat.activeId === null || chat.streaming"
            :loading="chat.uploading"
            title="上传文件到会话，随消息发给模型"
            @click="pickFiles"
          >
            附件
          </n-button>
        </div>
        <div v-if="chat.pendingFiles.length > 0" class="composer-chips">
          <n-tag
            v-for="name in chat.pendingFiles"
            :key="name"
            size="small"
            closable
            :disabled="chat.streaming"
            @close="chat.togglePending(name)"
          >
            附件 {{ name }}
          </n-tag>
        </div>
        <form class="composer-input" @submit.prevent="send">
          <n-input
            v-model:value="draft"
            type="textarea"
            :autosize="{ minRows: 1, maxRows: 8 }"
            placeholder="说点什么…（Enter 发送，Shift+Enter 换行）"
            :disabled="chat.streaming"
            @keydown="onKeydown"
          />
          <n-button v-if="chat.streaming" @click="chat.stop()">停止</n-button>
          <n-button v-else type="primary" attr-type="submit" :disabled="draft.trim() === ''">
            发送
          </n-button>
        </form>
      </div>
      <input
        ref="fileInput"
        type="file"
        multiple
        hidden
        data-testid="file-input"
        @change="onFilesChosen"
      />
    </main>
  </div>
</template>

<style scoped>
.shell {
  height: 100%;
  display: grid;
  grid-template-columns: 240px 1fr;
}

.sidebar {
  display: grid;
  grid-template-rows: auto 1fr auto auto;
  gap: 8px;
  padding: 12px;
  border-right: 1px solid rgba(128, 128, 128, 0.25);
  min-height: 0;
}

.nav {
  display: grid;
  gap: 6px;
}

.sessions {
  overflow-y: auto;
  display: grid;
  gap: 4px;
  align-content: start;
  min-height: 0;
}

.session {
  display: grid;
  gap: 2px;
  padding: 8px 10px;
  text-align: left;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  cursor: pointer;
  font: inherit;
  color: inherit;
}

.session:hover:not(:disabled) {
  background: rgba(128, 128, 128, 0.12);
}

.session:disabled {
  cursor: default;
  opacity: 0.6;
}

.session.active {
  border-color: rgba(128, 128, 128, 0.4);
  background: rgba(128, 128, 128, 0.16);
}

.s-title {
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.s-meta {
  font-size: 11px;
  opacity: 0.6;
}

.account {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.main {
  display: grid;
  grid-template-rows: auto auto 1fr auto;
  min-width: 0;
  min-height: 0;
}

.topbar {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 16px;
  border-bottom: 1px solid rgba(128, 128, 128, 0.25);
}

.topbar-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.files-panel {
  display: grid;
  gap: 6px;
  margin-top: 8px;
  padding: 8px 10px;
  border: 1px solid rgba(128, 128, 128, 0.25);
  border-radius: 6px;
  max-height: 220px;
  overflow-y: auto;
}

.files-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.files-list {
  display: grid;
  gap: 2px;
}

.file-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 2px 0;
}

.file-check {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
  cursor: pointer;
}

.file-name {
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-row a {
  color: inherit;
}

.topbar-title {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.banners {
  display: grid;
  gap: 8px;
  padding: 0 16px;
}

.banners > :first-child {
  margin-top: 8px;
}

.transcript {
  overflow-y: auto;
  padding: 16px;
  display: grid;
  gap: 14px;
  align-content: start;
  min-height: 0;
}

.empty {
  justify-self: center;
  padding: 48px 0;
}

.msg {
  display: grid;
  gap: 4px;
  justify-items: start;
  max-width: 100%;
}

.msg.user {
  justify-items: end;
}

.msg.system {
  justify-items: center;
}

.who {
  font-size: 11px;
  opacity: 0.55;
}

.bubble {
  padding: 8px 12px;
  border-radius: 8px;
  background: rgba(128, 128, 128, 0.14);
  line-height: 1.65;
  max-width: 100%;
}

.msg.user .bubble {
  background: rgba(24, 160, 88, 0.16);
}

.sysnote {
  font-size: 12px;
  opacity: 0.6;
  text-align: center;
}

.tool {
  display: grid;
  gap: 4px;
  width: 100%;
  padding: 8px 10px;
  border: 1px solid rgba(128, 128, 128, 0.25);
  border-radius: 6px;
}

.tool-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.block {
  margin: 0;
  padding: 6px 8px;
  border-radius: 4px;
  background: rgba(128, 128, 128, 0.12);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 240px;
  overflow-y: auto;
}

.result {
  opacity: 0.85;
}

.plan {
  display: grid;
  gap: 6px;
  width: 100%;
  padding: 8px 10px;
  border: 1px solid rgba(128, 128, 128, 0.25);
  border-radius: 6px;
}

.plan-steps {
  margin: 0;
  padding-left: 20px;
  display: grid;
  gap: 2px;
  line-height: 1.6;
}

.thinking {
  justify-self: start;
}

.files {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  justify-content: inherit;
}

.file-chip {
  font-size: 12px;
  padding: 3px 10px;
  border: 1px solid rgba(128, 128, 128, 0.35);
  border-radius: 12px;
  color: inherit;
  text-decoration: none;
}

.composer {
  display: grid;
  gap: 6px;
  padding: 10px 16px 12px;
  border-top: 1px solid rgba(128, 128, 128, 0.25);
}

.composer-tools {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.composer-tools .spacer {
  flex: 1;
}

.chip {
  font: inherit;
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 12px;
  border: 1px solid rgba(128, 128, 128, 0.35);
  background: transparent;
  color: inherit;
  cursor: pointer;
}

.chip:hover:not(:disabled) {
  background: rgba(128, 128, 128, 0.12);
}

.chip.on {
  background: rgba(24, 160, 88, 0.18);
  border-color: rgba(24, 160, 88, 0.55);
}

.chip:disabled {
  cursor: default;
  opacity: 0.6;
}

.composer-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.composer-input {
  display: flex;
  align-items: flex-end;
  gap: 8px;
}
</style>
