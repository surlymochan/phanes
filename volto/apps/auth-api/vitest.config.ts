import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    execArgv: ["--disable-warning=ExperimentalWarning"],
    include: ["test/**/*.test.ts"],
  },
});
