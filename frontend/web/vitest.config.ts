import { defineConfig } from "vitest/config";

// Separate from vite.config.ts on purpose: these are pure-function tests
// over the export builders, so they need neither the React plugin nor a DOM.
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
