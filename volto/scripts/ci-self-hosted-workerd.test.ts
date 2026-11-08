import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const repositoryRoot = resolve(import.meta.dirname, "..");
const packageJson = JSON.parse(readFileSync(resolve(repositoryRoot, "package.json"), "utf8")) as {
  scripts?: Record<string, string>;
};
const workflow = readFileSync(resolve(repositoryRoot, ".github/workflows/ci.yml"), "utf8");
const apiJob = workflow.match(/^  api:\n([\s\S]*?)(?=^  [a-zA-Z0-9_-]+:|\s*$)/m)?.[1] ?? "";
const workerdScript = "apps/auth-api/scripts/issue-9-local-workerd-e2e.ts";
const reportPath = ".volto-build/issue-9-local-workerd-e2e/report.json";

describe("self-hosted workerd CI check", () => {
  it("exposes a package command for the existing local workerd E2E script", () => {
    expect(existsSync(resolve(repositoryRoot, workerdScript))).toBe(true);
    expect(packageJson.scripts?.["test:self-hosted-workerd"]).toBe(`bun ${workerdScript}`);
  });

  it("runs the package command in the API CI job", () => {
    expect(apiJob).toContain("run: bun run test:self-hosted-workerd");
  });

  it("uploads the JSON report even when the check fails", () => {
    const reportUpload = apiJob.match(
      new RegExp(`- name: Upload self-hosted workerd E2E report\\n([\\s\\S]*?)(?=\\n      - name:|$)`),
    )?.[1];

    expect(reportUpload).toContain("if: always()");
    expect(reportUpload).toContain(`path: ${reportPath}`);
  });
});
