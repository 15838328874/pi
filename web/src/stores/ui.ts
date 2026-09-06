import { ref } from "vue";
import { defineStore } from "pinia";

/** Which full-screen view App.vue is showing. Still no router: three screens. */
export type Screen = "chat" | "account" | "admin";

/**
 * The one piece of navigation state in the app.
 *
 * Kept in a store rather than in App.vue so the sidebar in ChatView can switch
 * away from the chat without prop-drilling or a component event bus, and so
 * sign-out can reset it from anywhere (a user who was staring at the admin
 * console must not be put back there after the next login).
 */
export const useUi = defineStore("ui", () => {
  const screen = ref<Screen>("chat");

  function go(target: Screen): void {
    // 'admin' for a non-admin is guarded in App.vue's template, not here: the
    // store has no idea who is signed in, and the view does.
    screen.value = target;
  }

  function reset(): void {
    screen.value = "chat";
  }

  return { screen, go, reset };
});
