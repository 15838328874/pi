<script setup lang="ts">
import { computed, ref } from "vue";
import { NAlert, NButton, NCard, NInput, NTabs, NTabPane } from "naive-ui";
import { useAuth } from "../stores/auth";

const auth = useAuth();

const tab = ref<"login" | "register">("login");
const username = ref("");
const password = ref("");

const canSubmit = computed(() => username.value !== "" && password.value !== "" && !auth.busy);

/**
 * No client-side validation on purpose. The username and password bounds live in
 * RegisterIn on the server and reach the browser as a 422 whose per-field
 * messages auth.error already renders - duplicating the numbers here would only
 * give them a second place to drift.
 */
async function submit(): Promise<void> {
  if (tab.value === "login") await auth.signIn(username.value, password.value);
  else await auth.signUp(username.value, password.value);
}
</script>

<template>
  <div class="center">
    <n-card class="panel">
      <template #header>
        <span class="title">pi-py</span>
      </template>

      <n-tabs v-model:value="tab" type="segment" justify-content="space-evenly">
        <n-tab-pane name="login" tab="登录" />
        <n-tab-pane name="register" tab="注册" />
      </n-tabs>

      <n-alert v-if="auth.error" type="error" :bordered="false" class="error">
        {{ auth.error }}
      </n-alert>

      <form @submit.prevent="submit">
        <label class="field">
          <span class="label">用户名</span>
          <n-input
            v-model:value="username"
            placeholder="用户名"
            autocomplete="username"
            :disabled="auth.busy"
          />
        </label>
        <label class="field">
          <span class="label">密码</span>
          <n-input
            v-model:value="password"
            type="password"
            show-password-on="click"
            placeholder="密码"
            autocomplete="current-password"
            :disabled="auth.busy"
            @keydown.enter="submit"
          />
        </label>
        <n-button type="primary" attr-type="submit" block :loading="auth.busy" :disabled="!canSubmit">
          {{ tab === "login" ? "登录" : "注册并登录" }}
        </n-button>
      </form>

      <p class="hint">
        注册会在本机创建工作区目录；每个用户只能看到自己的会话。
      </p>
    </n-card>
  </div>
</template>

<style scoped>
.center {
  height: 100%;
  display: grid;
  place-items: center;
  padding: 16px;
}

.panel {
  width: 100%;
  max-width: 380px;
}

.title {
  font-weight: 600;
  letter-spacing: 0.02em;
}

.error {
  margin-top: 12px;
}

form {
  margin-top: 16px;
  display: grid;
  gap: 12px;
}

.field {
  display: grid;
  gap: 4px;
}

.label {
  font-size: 13px;
  opacity: 0.75;
}

.hint {
  margin: 14px 0 0;
  font-size: 12px;
  line-height: 1.6;
  opacity: 0.6;
}
</style>
