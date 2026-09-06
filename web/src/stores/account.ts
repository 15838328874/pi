import { ref } from "vue";
import { defineStore } from "pinia";
import { errorMessage } from "../api/client";
import { clearMemories, deleteMemory, deregister, getUsage, listMemories } from "../api/endpoints";
import type { MemoryFactOut, UsageOut } from "../api/types";
import { useAuth } from "./auth";
import { useChat } from "./chat";
import { useUi } from "./ui";

/**
 * Everything the account screens show: this month's bill, the long-term memory
 * the agent has accumulated, and the self-destruct button.
 *
 * Same convention as auth.ts: actions report failure through `error` and never
 * throw, so the views are plain templates with no try/catch.
 */
export const useAccount = defineStore("account", () => {
  const usage = ref<UsageOut | null>(null);
  const facts = ref<MemoryFactOut[]>([]);
  const memoryStatus = ref("");
  const busy = ref(false);
  const error = ref("");

  async function loadUsage(): Promise<void> {
    try {
      usage.value = await getUsage();
    } catch (err) {
      error.value = errorMessage(err);
    }
  }

  async function loadMemories(): Promise<void> {
    try {
      const r = await listMemories();
      facts.value = r.facts;
      memoryStatus.value = r.status;
    } catch (err) {
      error.value = errorMessage(err);
    }
  }

  async function removeFact(id: number): Promise<void> {
    error.value = "";
    try {
      await deleteMemory(id);
      facts.value = facts.value.filter((f) => f.id !== id);
    } catch (err) {
      error.value = errorMessage(err);
    }
  }

  async function clearFacts(): Promise<void> {
    error.value = "";
    try {
      await clearMemories();
      facts.value = [];
    } catch (err) {
      error.value = errorMessage(err);
    }
  }

  /**
   * Erase the account server-side. Irreversible: the server purges all seven
   * trace tables, so on success the client's only job is to look signed-out.
   *
   * forget() flips signedIn, and the watcher in App.vue clears every data
   * store - including this one - so nothing from the deleted account survives
   * into the next login on this tab.
   */
  async function destroy(password: string): Promise<boolean> {
    error.value = "";
    busy.value = true;
    try {
      await deregister(password);
      useUi().reset();
      useChat().clear();
      useAuth().forget();
      return true;
    } catch (err) {
      error.value = errorMessage(err);
      return false;
    } finally {
      busy.value = false;
    }
  }

  function clear(): void {
    usage.value = null;
    facts.value = [];
    memoryStatus.value = "";
    busy.value = false;
    error.value = "";
  }

  return {
    usage,
    facts,
    memoryStatus,
    busy,
    error,
    loadUsage,
    loadMemories,
    removeFact,
    clearFacts,
    destroy,
    clear,
  };
});
