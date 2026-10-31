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
  const assertLegacyDataCleared = async () => {
    for (const text of [
      "Synthetic categories",
      "Synthetic only",
      "Synthetic tier A",
      "Example A",
      "Synthetic tier B",
      "Example B",
    ]) {
      assert.equal(await page.getByText(text, { exact: true }).count(), 0);
    }
    for (const selector of ["#stage-title", "#stage-note"]) {
      assert.equal((await page.locator(selector).textContent())?.trim(), "—");
    }
    assert.equal((await page.locator("#stage-list").textContent())?.trim(), "");
  };
  for (const [language, expected, metricLabels, unavailable] of [
    [
      "zh",
      "历史快照",
      [
        "整体技术人力成本",
        "整体折算人头",
        "营收业务技术人力成本",
        "营收业务折算人头",
        "营收业务收入",
        "整体技术人力成本率",
      ],
      "未提供",
    ],
    [
      "en",
      "Historical snapshot",
      [
        "Total technical labor cost",
        "Total equivalent headcount",
        "Revenue business technical labor cost",
        "Revenue business equivalent headcount",
        "Revenue business income",
        "Overall technical labor cost ratio",
      ],
      "Not provided",
    ],
    [
      "fr",
      "Historical snapshot",
      [
        "Total technical labor cost",
        "Total equivalent headcount",
        "Revenue business technical labor cost",
        "Revenue business equivalent headcount",
        "Revenue business income",
        "Overall technical labor cost ratio",
      ],
      "Not provided",
    ],
  ] as const) {
    await page.goto(`${url}/?lang=${language}`);
    await page.getByText(expected, { exact: false }).first().waitFor();
    for (const label of metricLabels) await page.getByText(label, { exact: true }).waitFor();
    assert.equal(await page.getByText("15", { exact: true }).count(), 1);
    assert.equal(await page.getByText(unavailable, { exact: true }).count(), 6);
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
          { 分档: "Synthetic tier A", 例证: "Example A" },
          { 分档: "Synthetic tier B", 例证: "Example B" },
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
  for (const text of [
    "Synthetic categories",
    "Synthetic only",
    "Synthetic tier A",
    "Example A",
    "Synthetic tier B",
    "Example B",
  ]) {
    await page.getByText(text, { exact: true }).waitFor();
  }
  checks.push("legacy snapshot valid");
  const missingStage = structuredClone(legacy);
  missingStage.指标.pop();
  await writeFile(data, JSON.stringify(missingStage));
  await page.goto(`${url}/?lang=zh`);
  await page.getByText("加载失败", { exact: true }).waitFor();
  await assertLegacyDataCleared();
  checks.push("legacy snapshot without stage rejected");
  const emptyStage = structuredClone(legacy);
  emptyStage.指标[4].指标 = [];
  await writeFile(data, JSON.stringify(emptyStage));
  await page.goto(`${url}/?lang=zh`);
  await page.getByText("加载失败", { exact: true }).waitFor();
  await assertLegacyDataCleared();
  checks.push("legacy snapshot with empty stage rejected");
  for (const [name, mutate] of [
    ["blank stage title", (value: typeof legacy) => (value.指标[4].标题 = " ")],
    ["blank stage source", (value: typeof legacy) => (value.指标[4].来源口径 = " ")],
    ["blank stage label", (value: typeof legacy) => (value.指标[4].指标[0].分档 = " ")],
    ["blank stage example", (value: typeof legacy) => (value.指标[4].指标[0].例证 = " ")],
  ] as const) {
    const malformed = structuredClone(legacy);
    mutate(malformed);
    await writeFile(data, JSON.stringify(malformed));
    await page.goto(`${url}/?lang=zh`);
    await page.getByText("加载失败", { exact: true }).waitFor();
    await assertLegacyDataCleared();
    checks.push(`legacy snapshot with ${name} rejected`);
  }
  const rejectedAfterStage = structuredClone(legacy);
  delete rejectedAfterStage.指标[3].数值["收入万美元每月"]["年末目标"];
  await writeFile(data, JSON.stringify(rejectedAfterStage));
  await page.goto(`${url}/?lang=zh`);
  await page.getByText("加载失败", { exact: true }).waitFor();
  await assertLegacyDataCleared();
  checks.push("rejected legacy evidence cleared");
  const missingOptional = structuredClone(snapshot);
  delete missingOptional.insights;
  missingOptional.definitions = [];
  delete missingOptional.risks;
  missingOptional.recommendations = [];
  await writeFile(data, JSON.stringify(missingOptional));
  for (const [language, emptyText] of [
    ["zh", "本期无数据"],
    ["en", "No data for this period"],
  ] as const) {
    await page.goto(`${url}/?lang=${language}`);
    await page.getByText(emptyText, { exact: true }).first().waitFor();
    assert.equal(await page.getByText("15", { exact: true }).count(), 1);
    assert.equal(await page.getByText(emptyText, { exact: true }).count(), 4);
    checks.push(`${language} snapshot with optional blocks missing or empty`);
  }
  const invalid = structuredClone(snapshot);
  delete invalid.metrics[3].数值["收入万美元每月"]["年末目标"];
  await writeFile(data, JSON.stringify(invalid));
  for (const [language, expected] of [
    ["zh", "加载失败"],
    ["en", "Loading failed"],
  ] as const) {
    await page.goto(`${url}/?lang=${language}`);
    await page.getByText(expected, { exact: true }).waitFor();
    assert.equal(await page.getByText("15", { exact: true }).count(), 0);
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
