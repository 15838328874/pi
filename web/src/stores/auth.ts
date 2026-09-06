import { computed, ref } from "vue";
import { defineStore } from "pinia";
import { ApiError, errorMessage, tokenStore } from "../api/client";
import { login, logout, registerUser, whoami } from "../api/endpoints";
import type { LoginOut } from "../api/types";

/**
 * Session identity: the bearer token, who it belongs to, and the three ways to
 * change both.
 *
 * Every action reports failure through `error` instead of throwing. A login form
 * that has to try/catch around its own submit handler is how 401s end up in the
 * console and nothing on the screen.
 */
export const useAuth = defineStore("auth", () => {
  const token = ref<string | null>(tokenStore.get());
  const username = ref("");
  /**
   * Whether the account may enter the admin console. Comes from LoginOut on
   * login and from MeOut on restore - the same value the server re-derives on
   * every admin request, so it is purely a UI affordance, never an authority.
   */
  const isAdmin = ref(false);
  const busy = ref(false);
  const error = ref("");
  /**
   * False until the first restore() settles. A token in sessionStorage might be
   * valid or already revoked, and guessing either way flashes the wrong screen
   * for a frame - so the app waits on this instead.
   */
  const restored = ref(false);

  const signedIn = computed(() => token.value !== null && username.value !== "");

  function forget(): void {
    token.value = null;
    username.value = "";
    isAdmin.value = false;
    tokenStore.clear();
  }

  function applyLogin(r: LoginOut): void {
    token.value = r.access_token;
    tokenStore.set(r.access_token);
    // The stored account name, not what the form said: MySQL's default collation
    // matches case-insensitively, so the two can differ.
    username.value = r.username;
    isAdmin.value = r.is_admin;
  }

  async function attempt(fn: () => Promise<boolean>): Promise<boolean> {
    busy.value = true;
    error.value = "";
    try {
      return await fn();
    } catch (err) {
      error.value = errorMessage(err);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /**
   * Validate whatever token survived in sessionStorage.
   *
   * Never throws. A dead token is a normal outcome, not an error worth showing,
   * and an unreachable server must not lock the user out of the login form.
   */
  async function restore(): Promise<void> {
    error.value = "";
    if (token.value === null) {
      restored.value = true;
      return;
    }
    try {
      const me = await whoami();
      username.value = me.username;
      isAdmin.value = me.is_admin;
    } catch (err) {
      // client.toApiError already dropped the token from storage on a 401; this
      // only clears the in-memory copy so signedIn flips immediately.
      if (err instanceof ApiError && err.isAuthFailure) forget();
      else error.value = errorMessage(err);
    } finally {
      restored.value = true;
    }
  }

  const signIn = (name: string, password: string) =>
    attempt(async () => {
      applyLogin(await login(name, password));
      return true;
    });

  /**
   * Two round trips: register issues no token, so signing up means registering
   * and then logging in. A failed register (409 duplicate, 422 short password)
   * leaves the second call unmade.
   */
  const signUp = (name: string, password: string) =>
    attempt(async () => {
      await registerUser(name, password);
      applyLogin(await login(name, password));
      return true;
    });

  const signOut = () =>
    attempt(async () => {
      try {
        await logout();
      } catch {
        // Best effort by design. A 401 here means the server already treats the
        // token as dead, and a network failure must not trap the user inside a
        // session they are trying to leave.
      }
      forget();
      return true;
    });

  return { username, isAdmin, busy, error, restored, signedIn, forget, restore, signIn, signUp, signOut };
});
