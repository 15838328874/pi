<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { NAlert, NButton, NCard, NInput, NProgress, NTag } from "naive-ui";
import { useAccount } from "../stores/account";
import { useAuth } from "../stores/auth";
import { useUi } from "../stores/ui";

const account = useAccount();
const auth = useAuth();
const ui = useUi();

const password = ref("");

/**
 * Destructive buttons confirm in place: first click arms the button, second
 * click fires. Not n-popconfirm - its popover needs a live DOM during setup,
 * which the SSR render tests (and this view's first paint) deliberately lack.
 */
const armed = ref<string | null>(null);

onMounted(() => {
  void account.loadUsage();
  void account.loadMemories();
});

const KIND_TEXT: Record<string, string> = {
  preference: "偏好",
  convention: "约定",
  environment: "环境",
  fact: "事实",
};

function kindText(kind: string): string {
  return KIND_TEXT[kind] ?? kind;
}

/**
 * Memory status as the template needs it: 'ready' and 'disabled' are normal
 * deployments, anything else is "unavailable: <reason>" - a Milvus or embedding
 * outage the admin broke, which the user can still read here.
 */
const memoryTag = computed<{ text: string; type: "success" | "warning" | "error" }>(() => {
  const s = account.memoryStatus;
  if (s === "ready") return { text: "记忆已启用", type: "success" };
  if (s === "disabled") return { text: "记忆未启用", type: "warning" };
  return { text: s || "…", type: "error" };
});

const quotaPercent = computed(() => {
  const u = account.usage;
  if (u === null || u.quota_tokens <= 0) return 0;
  return Math.min(100, Math.round((u.used_tokens / u.quota_tokens) * 100));
});

function fmtTokens(n: number): string {
  return n >= 10_000 ? `${(n / 10_000).toFixed(1)} 万` : String(n);
}

function fmtDate(iso: string): string {
  return iso === "" ? "" : iso.slice(0, 10);
}
</script>

<template>
  <div class="page">
    <header class="topbar">
      <n-button size="small" quaternary @click="ui.go('chat')">← 返回对话</n-button>
      <strong>账号与用量</strong>
      <span class="s-meta">{{ auth.username }}</span>
    </header>

    <n-alert v-if="account.error" type="error" :bordered="false" class="banner">
      {{ account.error }}
    </n-alert>

    <div class="cards">
      <n-card title="本月用量" size="small">
        <template v-if="account.usage !== null">
          <div class="quota">
            <span>
              已用 {{ fmtTokens(account.usage.used_tokens) }} /
              {{ fmtTokens(account.usage.quota_tokens) }} tokens
              （{{ account.usage.month }}）
            </span>
            <n-progress
              type="line"
              :percentage="quotaPercent"
              :status="quotaPercent >= 100 ? 'error' : 'default'"
            />
          </div>
          <table class="table">
            <thead>
              <tr>
                <th>模型</th>
                <th>输入</th>
                <th>输出</th>
                <th>次数</th>
                <th>估算费用 ($)</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="m in account.usage.models" :key="m.model">
                <td class="mono">{{ m.model }}</td>
                <td>{{ fmtTokens(m.input_tokens) }}</td>
                <td>{{ fmtTokens(m.output_tokens) }}</td>
                <td>{{ m.runs }}</td>
                <td>{{ m.est_cost_usd.toFixed(4) }}</td>
              </tr>
            </tbody>
          </table>
          <p class="s-meta">
            合计：输入 {{ fmtTokens(account.usage.total_input_tokens) }} · 输出
            {{ fmtTokens(account.usage.total_output_tokens) }} · 约
            ${{ account.usage.total_est_cost_usd.toFixed(4) }}
          </p>
        </template>
        <p v-else class="s-meta">加载中…</p>
      </n-card>

      <n-card title="长期记忆" size="small">
        <template #header-extra>
          <n-tag size="small" :type="memoryTag.type" :bordered="false">
            {{ memoryTag.text }}
          </n-tag>
        </template>

        <p v-if="account.facts.length === 0" class="s-meta">
          还没有记忆。对话结束后，代理会从中提取偏好与事实存入 MySQL。
        </p>
        <template v-else>
          <div class="facts">
            <div v-for="f in account.facts" :key="f.id" class="fact">
              <div class="fact-body">
                <div class="fact-text">{{ f.text }}</div>
                <div class="s-meta">
                  {{ kindText(f.kind) }} · 来自会话 {{ f.source_session.slice(0, 8) }} ·
                  {{ fmtDate(f.created_at) }}
                </div>
              </div>
              <n-button
                v-if="armed !== `fact-${f.id}`"
                size="tiny"
                quaternary
                type="error"
                @click="armed = `fact-${f.id}`"
              >
                删除
              </n-button>
              <n-button
                v-else
                size="tiny"
                type="error"
                @click="armed = null; account.removeFact(f.id)"
              >
                确认删除
              </n-button>
            </div>
          </div>
          <div class="row-end">
            <n-button
              v-if="armed !== 'clear'"
              size="small"
              secondary
              type="error"
              @click="armed = 'clear'"
            >
              清空全部记忆
            </n-button>
            <n-button
              v-else
              size="small"
              type="error"
              @click="armed = null; account.clearFacts()"
            >
              确认清空 {{ account.facts.length }} 条
            </n-button>
          </div>
        </template>
      </n-card>

      <n-card title="危险操作" size="small" class="danger">
        <p class="s-meta">
          注销会永久删除账号与全部数据：会话、消息、记忆、用量与审计记录，以及工作区目录。
          需要密码确认——只有令牌不足以执行不可逆的删除。
        </p>
        <div class="destroy">
          <n-input
            v-model:value="password"
            type="password"
            show-password-on="click"
            placeholder="密码"
            class="destroy-input"
            :disabled="account.busy"
          />
          <n-button
            v-if="armed !== 'destroy'"
            type="error"
            secondary
            :disabled="password === ''"
            @click="armed = 'destroy'"
          >
            注销账号
          </n-button>
          <n-button
            v-else
            type="error"
            :loading="account.busy"
            :disabled="password === ''"
            @click="account.destroy(password)"
          >
            确认注销（不可恢复）
          </n-button>
        </div>
      </n-card>
    </div>
  </div>
</template>

<style scoped>
.page {
  height: 100%;
  overflow-y: auto;
  display: grid;
  grid-template-rows: auto auto 1fr;
  gap: 12px;
  padding: 16px;
  max-width: 760px;
  margin: 0 auto;
}

.topbar {
  display: flex;
  align-items: center;
  gap: 12px;
}

.banner {
  max-width: none;
}

.cards {
  display: grid;
  gap: 12px;
  align-content: start;
}

.quota {
  display: grid;
  gap: 6px;
  margin-bottom: 12px;
  font-size: 13px;
}

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.table th,
.table td {
  text-align: left;
  padding: 4px 8px 4px 0;
  border-bottom: 1px solid rgba(128, 128, 128, 0.2);
}

.facts {
  display: grid;
  gap: 8px;
}

.fact {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.fact-body {
  flex: 1;
  display: grid;
  gap: 2px;
}

.fact-text {
  font-size: 13px;
  line-height: 1.5;
}

.row-end {
  margin-top: 10px;
  display: flex;
  justify-content: flex-end;
}

.destroy {
  margin-top: 10px;
  display: flex;
  gap: 8px;
  align-items: center;
}

.destroy-input {
  max-width: 220px;
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
