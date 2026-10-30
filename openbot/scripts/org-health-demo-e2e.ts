// Exercises the real loopback service and rendered synthetic demo in both languages.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { chromium } from "playwright-core";

const root = resolve(import.meta.dirname, "..");
const output = join(root, ".openbot-build", "org-health-demo-e2e");
const temporary = await mkdtemp(join(tmpdir(), "openbot-health-demo-"));
const data = join(temporary, "diagnosis.json");
const source = await readFile(join(root, "examples/org-health-local/data/diagnosis.json"), "utf8");
await writeFile(data, source);
const server = spawn("python3", [join(root, "examples/org-health-local/serve.py"), "--port", "0", "--data", data], {
  stdio: ["ignore", "pipe", "pipe"],
});
const checks: string[] = [];
let error: string | null = null;
let browser: Awaited<ReturnType<typeof chromium.launch>> | undefined;
try {
  const url = await new Promise<string>((done, fail) => {
    let text = "";
    const timer = setTimeout(() => fail(new Error("service startup timed out")), 30_000);
    server.stdout.on("data", (chunk) => {
      text += chunk.toString();
      const match = text.match(/http:\/\/127\.0\.0\.1:\d+/);
      if (match) {
        clearTimeout(timer);
        done(match[0]);
      }
    });
    server.once("exit", (code) => {
      clearTimeout(timer);
      fail(new Error(`service exited: ${code}`));
    });
    server.once("error", (cause) => {
      clearTimeout(timer);
      fail(cause);
    });
  });
  browser = await chromium.launch({ channel: "chrome", headless: true });
  const page = await browser.newPage();
  for (const [language, expected] of [
    ["zh", "历史快照"],
    ["en", "Historical snapshot"],
    ["fr", "Historical snapshot"],
  ] as const) {
    await page.goto(`${url}/?lang=${language}`);
    await page.getByText(expected, { exact: false }).first().waitFor();
    await page
      .getByText(language === "zh" ? "整体技术人力成本" : "Total technical labor cost", { exact: false })
      .first()
      .waitFor();
    checks.push(`${language} valid snapshot`);
  }
  const invalid = JSON.parse(source);
  delete invalid.metrics[3].数值["收入万美元每月"]["年末目标"];
  await writeFile(data, JSON.stringify(invalid));
  for (const [language, expected] of [
    ["zh", "加载失败"],
    ["en", "Loading failed"],
  ] as const) {
    await page.goto(`${url}/?lang=${language}`);
    await page.getByText(expected, { exact: true }).waitFor();
    assert.equal(await page.locator("#metric-grid").innerText(), "");
    assert.equal(await page.locator("#scope-month").innerText(), "—");
    checks.push(`${language} invalid snapshot`);
  }
  const denied = await fetch(`${url}/data/diagnosis.json`, { method: "HEAD", headers: { Host: "invalid.example" } });
  assert.equal(denied.status, 403);
  const hidden = await fetch(`${url}/serve.py`);
  assert.equal(hidden.status, 404);
  checks.push("invalid Host and private path denied");
} catch (cause) {
  error = cause instanceof Error ? cause.message : String(cause);
} finally {
  await browser?.close();
  server.kill("SIGTERM");
  await rm(temporary, { recursive: true, force: true });
  await mkdir(output, { recursive: true });
  await writeFile(join(output, "report.json"), JSON.stringify({ checks, error }, null, 2));
}
if (error) throw new Error(error);
process.stdout.write(`PASS ${checks.length} checks: ${join(output, "report.json")}\n`);
