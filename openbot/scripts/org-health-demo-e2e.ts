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
const snapshot = JSON.parse(source);
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
    assert.equal(await page.getByText("15", { exact: true }).count(), 1);
    assert.equal(await page.getByText(language === "zh" ? "未提供" : "Not provided", { exact: true }).count(), 6);
    assert.notEqual(await page.locator("#asof-short").innerText(), language === "zh" ? "加载失败" : "Loading failed");
    checks.push(`${language} valid snapshot`);
  }
  const legacy = {
    标题: "Synthetic historical snapshot",
    结论: snapshot.conclusion,
    范围: snapshot.scope,
    指标: [
      ...snapshot.metrics,
      {
        标题: "Synthetic categories",
        来源口径: "Synthetic only",
        指标: [
          { 分档: "A", 例证: "Example A" },
          { 分档: "B", 例证: "Example B" },
        ],
      },
    ],
    来源口径: snapshot.definitions,
    洞察: snapshot.insights,
    风险: snapshot.risks,
    建议: snapshot.recommendations,
  };
  await writeFile(data, JSON.stringify(legacy));
  await page.goto(`${url}/?lang=zh`);
  await page.getByText("整体技术人力成本", { exact: false }).first().waitFor();
  assert.equal(await page.locator("#scope-month").innerText(), "2026-09");
  assert.equal(await page.locator("#stage-title").innerText(), "Synthetic categories");
  assert.equal(await page.locator("#stage-note").innerText(), "Synthetic only");
  assert.equal(await page.locator("#stage-list > div").first().locator("b").innerText(), "A");
  assert.equal(await page.locator("#stage-list > div").first().locator("p").innerText(), "Example A");
  assert.equal(await page.locator("#stage-list > div").nth(1).locator("b").innerText(), "B");
  assert.equal(await page.locator("#stage-list > div").nth(1).locator("p").innerText(), "Example B");
  checks.push("legacy snapshot valid");
  const missingStage = structuredClone(legacy);
  missingStage.指标.pop();
  await writeFile(data, JSON.stringify(missingStage));
  await page.goto(`${url}/?lang=zh`);
  await page.getByText("加载失败", { exact: true }).waitFor();
  assert.equal(await page.locator("#metric-grid").innerText(), "");
  assert.equal(await page.locator("#stage-title").innerText(), "—");
  assert.equal(await page.locator("#stage-note").innerText(), "—");
  assert.equal(await page.locator("#stage-list").innerText(), "");
  checks.push("legacy snapshot without stage rejected");
  const invalid = structuredClone(snapshot);
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
    assert.equal(await page.locator("#stage-list").innerText(), "");
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
