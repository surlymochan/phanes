import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
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

  test("checks migration state, retained data, and SQLite integrity after restart", () => {
    expect(script).toMatch(/schema_migrations/);
    expect(script).toMatch(/foreign_key_check/);
    expect(script).toMatch(/integrity_check/);
    expect(script).toMatch(/restart/i);
  });

  test("CI runs the focused upgrade E2E and uploads its report", () => {
    expect(workflow).toContain("bun test scripts/agent-threads-upgrade-e2e.ts");
    expect(workflow).toContain("agent-threads-upgrade-e2e/report.json");
    expect(workflow).toContain("actions/upload-artifact");
  });

  test("runs the real host upgrade and restart against nonempty upstream data", () => {
    const output = execFileSync(
      "bun",
      [resolve(import.meta.dirname, "agent-threads-upgrade-e2e.ts"), "--run-e2e"],
      { cwd: resolve(import.meta.dirname, ".."), encoding: "utf8" },
    );
    expect(output).toContain('"passed": true');
    expect(output).toContain('"hostRestarted": true');
    expect(output).toContain('"retainedSessions": 1');
  }, 30_000);
});
