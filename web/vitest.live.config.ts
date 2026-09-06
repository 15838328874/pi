import { defineConfig } from "vitest/config";

/**
 * Config for the opt-in live check only. `npm test` uses vite.config.ts and its
 * default include pattern (`*.test.ts`), which does not match `*.live.ts`, so
 * these never run without a server up.
 */
export default defineConfig({
  test: {
    include: ["tests/**/*.live.ts"],
    environment: "node",
    testTimeout: 30_000,
    hookTimeout: 30_000,
  },
});
