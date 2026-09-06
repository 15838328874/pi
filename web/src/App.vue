<script setup lang="ts">
import { onMounted, watch } from "vue";
import { NSpin } from "naive-ui";
import { useAccount } from "./stores/account";
import { useAdmin } from "./stores/admin";
import { useAuth } from "./stores/auth";
import { useChat } from "./stores/chat";
import { useUi } from "./stores/ui";
import AccountView from "./views/AccountView.vue";
import AdminView from "./views/AdminView.vue";
import ChatView from "./views/ChatView.vue";
import LoginView from "./views/LoginView.vue";

const auth = useAuth();
const ui = useUi();

// No router: the screens are picked by two booleans plus one ui.screen value,
// which would not repay the dependency.
onMounted(() => {
  // A token in sessionStorage might still be valid, so ask before picking a
  // screen. restore() never throws - a dead token and an unreachable server are
  // both normal outcomes that land on the login form.
  void auth.restore();
});

// The moment signedIn flips false - sign-out, a 401 mid-run, or deregister -
// every store forgets the user. Without this, the next login on the same tab
// would briefly render the previous user's session list, transcript, memories
// and admin tables. Centralizing it here also spares each sign-out path from
// knowing the full store list.
watch(
  () => auth.signedIn,
  (now, was) => {
    if (was && !now) {
      useUi().reset();
      useChat().clear();
      useAccount().clear();
      useAdmin().clear();
    }
  },
);
</script>

<template>
  <div v-if="!auth.restored" class="splash">
    <n-spin size="large" />
  </div>
  <template v-else-if="auth.signedIn">
    <!-- 'admin' for a non-admin falls through to the chat screen: isAdmin came
         from the server, so treating it as the gate here matches exactly what
         the server enforces on every /v1/admin request. -->
    <admin-view v-if="ui.screen === 'admin' && auth.isAdmin" />
    <account-view v-else-if="ui.screen === 'account'" />
    <chat-view v-else />
  </template>
  <login-view v-else />
</template>

<style scoped>
.splash {
  height: 100%;
  display: grid;
  place-items: center;
}
</style>
