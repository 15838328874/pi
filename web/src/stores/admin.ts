import { ref } from "vue";
import { defineStore } from "pinia";
import { errorMessage } from "../api/client";
import {
  getTrace,
  listAdminUsers,
  listAudit,
  listTraces,
  revokeAdminUser,
  updateAdminUser,
} from "../api/endpoints";
import type { AuditFilter, TraceFilter } from "../api/endpoints";
import type { AdminUserOut, AuditRecord, TraceRunOut } from "../api/types";

/**
 * The admin console's three panels: accounts, the audit log, and execution
 * traces. All of it lives behind the server's admin gate - this store merely
 * renders what /v1/admin/* decides the caller may see.
 */
export const useAdmin = defineStore("admin", () => {
  const users = ref<AdminUserOut[]>([]);
  const audit = ref<AuditRecord[]>([]);
  const runs = ref<TraceRunOut[]>([]);
  /** The run whose steps are expanded; null = none selected. */
  const detail = ref<TraceRunOut | null>(null);
  const busy = ref(false);
  const error = ref("");

  async function loadUsers(): Promise<void> {
    try {
      users.value = (await listAdminUsers()).users;
    } catch (err) {
      error.value = errorMessage(err);
    }
  }

  async function loadAudit(filter: AuditFilter = {}): Promise<void> {
    try {
      audit.value = (await listAudit(filter)).records;
    } catch (err) {
      error.value = errorMessage(err);
    }
  }

  async function loadTraces(filter: TraceFilter = {}): Promise<void> {
    try {
      runs.value = (await listTraces(filter)).runs;
      detail.value = null;
    } catch (err) {
      error.value = errorMessage(err);
    }
  }

  /** Fetch one run with its step list. The list view omits steps on purpose. */
  async function selectTrace(runId: string): Promise<void> {
    error.value = "";
    try {
      detail.value = await getTrace(runId);
    } catch (err) {
      error.value = errorMessage(err);
    }
  }

  async function setUserActive(username: string, active: boolean): Promise<void> {
    error.value = "";
    try {
      await updateAdminUser(username, { is_active: active });
      await loadUsers();
    } catch (err) {
      // The switch snaps back: loadUsers() re-reads the server's truth.
      error.value = errorMessage(err);
      await loadUsers();
    }
  }

  async function setUserQuota(username: string, quotaTokens: number): Promise<void> {
    error.value = "";
    try {
      await updateAdminUser(username, { quota_tokens: quotaTokens });
      await loadUsers();
    } catch (err) {
      error.value = errorMessage(err);
    }
  }

  /**
   * Kill the user's live tokens. Their next request gets a 401; the account and
   * its data stay exactly as they are.
   */
  async function revokeTokens(username: string): Promise<void> {
    error.value = "";
    try {
      await revokeAdminUser(username);
    } catch (err) {
      error.value = errorMessage(err);
    }
  }

  function clear(): void {
    users.value = [];
    audit.value = [];
    runs.value = [];
    detail.value = null;
    busy.value = false;
    error.value = "";
  }

  return {
    users,
    audit,
    runs,
    detail,
    busy,
    error,
    loadUsers,
    loadAudit,
    loadTraces,
    selectTrace,
    setUserActive,
    setUserQuota,
    revokeTokens,
    clear,
  };
});
