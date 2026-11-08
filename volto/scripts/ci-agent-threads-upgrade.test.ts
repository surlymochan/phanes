import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { promisify } from "node:util";
import { describe, expect, test } from "vitest";

const script = readFileSync(new URL("./agent-threads-upgrade-e2e.ts", import.meta.url), "utf8");
const workflow = readFileSync(new URL("../.github/workflows/ci.yml", import.meta.url), "utf8");

describe("agent thread upgrade host E2E", () => {
  test("starts and restarts the real host with the upgraded upstream database", () => {
    expect(script).toMatch(/start(?:s|ing)? (?:the )?(?:real )?host|host\.start/i);
    expect(script).toMatch(/restart/i);
    expect(script).toMatch(/upstream.{0,40}(?:47|database)|(?:47|database).{0,40}upstream/i);
    expect(script).toMatch(/(?:session|task).{0,80}(?:retained|preserv|assert|equal)/i);
  });

  test("launches the application entry instead of a database-only host", () => {
    expect(script).toContain("_electron.launch(");
    expect(script).toContain("--user-data-dir=");
    expect(script).toContain('...(process.platform === "linux" ? ["--no-sandbox"] : [])');
    expect(script).toContain("volto:renderer-loaded");
    expect(script).not.toContain("new DatabaseCore(");
  });

  test("checks migration state, retained data, and SQLite integrity after restart", () => {
    expect(script).toMatch(/schema_migrations/);
    expect(script).toMatch(/foreign_key_check/);
    expect(script).toMatch(/integrity_check/);
    expect(script).toMatch(/restart/i);
  });

  test("uses the released upstream 47 schema without fork-only tables", () => {
    expect(script).toContain("const UPSTREAM_VERSION = 47;");
    expect(script).toContain('sourceTables.includes("projection_agent_threads")');
    expect(script).toContain('!sourceTables.includes("projection_agent_tasks")');
    expect(script).toContain('!sourceTables.includes("routine_backoff")');
    expect(script).toContain("AGENT_THREAD_MIGRATION_SQL");
  });

  test("CI runs the focused upgrade E2E and uploads its report", () => {
    expect(workflow).toContain(
      "bun test ./scripts/agent-threads-upgrade-e2e.ts ./scripts/ci-agent-threads-upgrade.test.ts",
    );
    expect(workflow).toContain("xvfb-run -a bun test ./scripts/agent-threads-upgrade-e2e.ts");
    expect(workflow).toContain("agent-threads-upgrade-e2e/report.json");
    expect(workflow).toContain("actions/upload-artifact");
    expect(workflow).toContain("--exclude scripts/ci-agent-threads-upgrade.test.ts");
  });

  test("runs the real host upgrade and restart against nonempty upstream data", async () => {
    const { stdout: output } = await promisify(execFile)(
      process.execPath,
      ["test", "./scripts/agent-threads-upgrade-e2e.ts"],
      { cwd: resolve(import.meta.dirname, ".."), encoding: "utf8", timeout: 240_000 },
    );
    expect(output).toContain('"passed": true');
    expect(output).toContain('"sourceVersion": 47');
    expect(output).toContain('"tasks": 1');
    expect(output).toContain('"hostRestarted": true');
    expect(output).toContain('"retainedSessions": 1');
  }, 240_000);
});
