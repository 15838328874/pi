/**
 * Live integration check for the API client, driven against a running server.
 *
 * Opt-in on purpose: it needs `pi-py serve` listening on PI_LIVE_API (default
 * http://127.0.0.1:8300) with a throwaway database, so it is not part of
 * `npm test`. Run it with `npm run test:live`.
 *
 *   PI_DATABASE_URL=sqlite+aiosqlite:////tmp/pi-live/db.sqlite \
 *   PI_MODEL=fake/demo PI_JWT_SECRET=live-check PI_TRACER=noop \
 *   PI_REDIS_URL= PI_SANDBOX= PI_POLICY= \
 *   pi-py serve --port 8300
 *
 * What this covers that the unit tests cannot: the real fetch/ReadableStream
 * path in postSse against bytes the server actually wrote, ApiError built from a
 * real X-Request-Id header, and abort releasing the reader.
 */
import { beforeAll, describe, expect, it } from "vitest";
import { ApiError, tokenStore } from "../../src/api/client";
import {
  clearMemories,
  createSession,
  deregister,
  getMessages,
  getUsage,
  listAdminUsers,
  listAudit,
  listMemories,
  listSessions,
  listTraces,
  login,
  registerUser,
  run,
  whoami,
} from "../../src/api/endpoints";
import { toUi } from "../../src/stores/chat";

const BASE = process.env.PI_LIVE_API ?? "http://127.0.0.1:8300";

// The browser APIs the client assumes. Node 24 has a real streaming fetch, so
// only the origin needs patching - everything else is the genuine article.
const realFetch = globalThis.fetch;

beforeAll(() => {
  const store = new Map<string, string>();
  Object.defineProperty(globalThis, "sessionStorage", {
    configurable: true,
    value: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    },
  });
  globalThis.fetch = ((input: unknown, init?: RequestInit) =>
    realFetch(new URL(String(input), BASE), init)) as typeof fetch;
});

// A fresh account per run: the server enforces uniqueness and the database is
// reused between invocations.
const user = `live-${process.pid.toString(36)}-${Date.now().toString(36)}`;
const password = "password123";

describe("live API client", () => {
  it("registers and reports the 409 on a repeat", async () => {
    const created = await registerUser(user, password);
    expect(created.username).toBe(user);
    expect(created.is_admin).toBe(false);

    const dup = await registerUser(user, password).then(
      () => null,
      (err: unknown) => err,
    );
    expect(dup).toBeInstanceOf(ApiError);
    expect((dup as ApiError).status).toBe(409);
  });

  it("logs in and stores a token that /v1/me accepts", async () => {
    const session = await login(user, password);
    expect(session.token_type).toBe("bearer");
    expect(session.username).toBe(user);
    tokenStore.set(session.access_token);
    expect(await whoami()).toEqual({ username: user, is_admin: false });
  });

  it("turns a 401 into an ApiError carrying the server's request id", async () => {
    tokenStore.clear();
    const err = await whoami().then(
      () => null,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).isAuthFailure).toBe(true);
    // The middleware stamps this on every response; without it a user's bug
    // report cannot be matched to a line in the server's access log.
    expect((err as ApiError).requestId).toMatch(/^[0-9a-f]{12}$/);
    expect((err as ApiError).message).not.toBe("");
    tokenStore.set(await login(user, password).then((r) => r.access_token));
  });

  it("creates a session and lists it back", async () => {
    const created = await createSession("live 检查");
    expect(created.title).toBe("live 检查");
    expect(created.cwd).not.toBe("");
    const listed = await listSessions();
    expect(listed.sessions.map((s) => s.id)).toContain(created.id);
  });

  it("streams a real run and parses every frame the server wrote", async () => {
    const created = await createSession("stream");
    const seen: string[] = [];
    let text = "";
    let turnEnd: { turns: number; input: number; output: number } | null = null;

    for await (const ev of run(created.id, "你好")) {
      seen.push(ev.event);
      if (ev.event === "text_delta") text += ev.data.text;
      if (ev.event === "turn_end") {
        turnEnd = { turns: ev.data.turns, input: ev.data.usage.input_tokens, output: ev.data.usage.output_tokens };
      }
    }

    expect(seen[0]).toBe("start");
    expect(seen.at(-1)).toBe("done");
    expect(seen).toContain("text_delta");
    expect(text.length).toBeGreaterThan(0);
    expect(turnEnd).not.toBeNull();
    // No frame arrived that endpoints.run() did not recognise.
    expect(seen.every((e) => e !== "unknown")).toBe(true);

    // The persisted transcript is what the UI ends up showing, so fold it the
    // same way the store does and check the result survived the round trip.
    const ui = toUi((await getMessages(created.id)).messages);
    expect(ui.map((m) => m.role)).toEqual(["user", "assistant"]);
    expect(ui[1].text).toBe(text);
  });

  it("releases the reader when the run is aborted", async () => {
    const created = await createSession("abort");
    const controller = new AbortController();
    const events: string[] = [];

    const consume = (async () => {
      for await (const ev of run(created.id, "你好", { signal: controller.signal })) {
        events.push(ev.event);
        if (ev.event === "text_delta") controller.abort();
      }
    })();

    // If the reader were not cancelled in postSse's finally, this would hang
    // until the server's run timeout rather than rejecting promptly.
    const err = await consume.then(
      () => null,
      (e: unknown) => e,
    );
    expect(err).not.toBeNull();
    expect((err as DOMException).name).toBe("AbortError");
    expect(events).toContain("text_delta");
  });

  it("reports an unknown session as a 404 ApiError, not a stream", async () => {
    const err = await (async () => {
      try {
        for await (const _ of run("does-not-exist", "hi")) void _;
        return null;
      } catch (e) {
        return e;
      }
    })();
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(404);
  });

  it("reads this month's usage with the effective quota", async () => {
    const u = await getUsage();
    expect(u.month).toMatch(/^\d{4}-\d{2}$/);
    expect(Array.isArray(u.models)).toBe(true);
    expect(u.quota_tokens).toBeGreaterThan(0);
    // The stream test above burned tokens; the usage row must show them, or
    // the billing table is not wired to the run loop in this deployment.
    expect(u.used_tokens).toBeGreaterThan(0);
  });

  it("lists memories with a status, whatever the deployment", async () => {
    const m = await listMemories();
    // 'ready', 'disabled' (no PI_MILVUS_URI) or 'unavailable: …' - all are
    // valid answers from a live server, but the shape must hold.
    expect(m.status.length).toBeGreaterThan(0);
    expect(Array.isArray(m.facts)).toBe(true);
    // Clearing whatever is there is itself the endpoint under test.
    const cleared = await clearMemories();
    expect(typeof cleared.deleted).toBe("number");
    expect((await listMemories()).facts).toEqual([]);
  });

  it("refuses the admin routes to a non-admin account", async () => {
    // The gate is server-side; this account registered through the open
    // endpoint, so it must read as a plain user on every admin route.
    const forbidden = (e: unknown): ApiError => {
      expect(e).toBeInstanceOf(ApiError);
      const err = e as ApiError;
      expect(err.status).toBe(403);
      return err;
    };
    forbidden(await listAdminUsers().then(() => null, (e: unknown) => e));
    forbidden(await listAudit().then(() => null, (e: unknown) => e));
    forbidden(await listTraces().then(() => null, (e: unknown) => e));
  });

  it("erases the account on deregister and kills the token", async () => {
    const receipt = await deregister(password);
    expect(receipt.username).toBe(user);
    expect(receipt.deleted).toBe(true);
    expect(receipt.purged.account).toBe(1);

    // The token died with the account: the next request is a 401, and the
    // client wrapper drops it from storage as part of building the error.
    const err = await whoami().then(
      () => null,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).isAuthFailure).toBe(true);
    expect(tokenStore.get()).toBeNull();
  });
});
