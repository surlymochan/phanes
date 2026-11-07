// Run isolated processes with legacy settings. No account or provider service is contacted.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const reportDirectory = join(projectRoot, ".volto-build/environment-compatibility-e2e");

if (process.argv.includes("--probe")) {
  const { configuredCliPath } = await import("../src/backend/cli");
  const { configuredFleetTurnGate } = await import("../src/backend/agent/fleet-turn-config");
  const { agentDisplayEnvironment } = await import("../src/main/agent-display");
  assert.throws(() => configuredFleetTurnGate(), /Fleet capacity configuration is required/u);
  const { githubAppConfig } = await import("../src/main/github-connector-config");
  const { devRuntimeDirectory } = await import("../src/main/development-runtime-directory");
  const { takeHostedServerEnvironment } = await import("../src/main/hosted-server-bootstrap");
  const { loadDevelopmentEnvironment, developmentChildEnvironment } = await import("./development-environment");
  const { readSlackDevelopmentValues } = await import("./dev-slack-tunnels");
  const childEnvironment = developmentChildEnvironment(process.env, "app");
  assert.equal(childEnvironment.OPENBOT_DEV_SLACK_SIGNING_SECRET, undefined);
  assert.equal(childEnvironment.VOLTO_DEV_SLACK_SIGNING_SECRET, undefined);
  const hosted = takeHostedServerEnvironment(process.env, true, "linux");
  const development = await loadDevelopmentEnvironment(reportDirectory, process.env, { readOnly: true });
  process.stdout.write(
    JSON.stringify({
      cli: configuredCliPath("codex"),
      display: agentDisplayEnvironment("linux"),
      github: githubAppConfig(),
      registry: devRuntimeDirectory("linux"),
      hosted,
      claimRemoved: process.env.OPENBOT_HOSTED_CLAIM === undefined && process.env.VOLTO_HOSTED_CLAIM === undefined,
      api: development.VOLTO_AUTH_API_URL,
      slack: readSlackDevelopmentValues("", process.env).VOLTO_DEV_SLACK_SIGNING_SECRET,
    }),
  );
} else {
  await mkdir(reportDirectory, { recursive: true });
  const cleanEnvironment = Object.fromEntries(
    Object.entries(process.env).filter(
      ([key]) => !key.startsWith("VOLTO_") && !key.startsWith("OPENBOT_") && !key.startsWith("DOTENV_"),
    ),
  );
  const legacy = {
    OPENBOT_CODEX_PATH: "/fixture/old-codex",
    OPENBOT_FLEET_CAPACITY_REQUIRED: "1",
    OPENBOT_AGENT_DISPLAY: ":9",
    OPENBOT_GITHUB_CLIENT_ID: "LegacyClient",
    OPENBOT_GITHUB_APP_SLUG: "legacy-app",
    OPENBOT_DEV_REGISTRY_DIR: join(reportDirectory, "registry"),
    OPENBOT_HOSTED_SERVER: "1",
    OPENBOT_HOSTED_HOST_ID: "11111111-1111-4111-8111-111111111111",
    OPENBOT_HOSTED_CLAIM: "fixture_claim",
    OPENBOT_AUTH_API_URL: "https://legacy.invalid",
    OPENBOT_DEV_SLACK_SIGNING_SECRET: "fixture_slack",
  };
  const cases = [
    { name: "legacy-only", current: {}, cli: "/fixture/old-codex", api: "https://legacy.invalid" },
    {
      name: "current-wins",
      current: { VOLTO_CODEX_PATH: "/fixture/new-codex", VOLTO_AUTH_API_URL: "https://current.invalid" },
      cli: "/fixture/new-codex",
      api: "https://current.invalid",
    },
    { name: "empty-current-wins", current: { VOLTO_CODEX_PATH: "", VOLTO_AUTH_API_URL: "" }, cli: null, api: "" },
  ];
  for (const scenario of cases) {
    const child = spawnSync(process.execPath, [fileURLToPath(import.meta.url), "--probe"], {
      cwd: projectRoot,
      env: { ...cleanEnvironment, ...legacy, ...scenario.current },
      encoding: "utf8",
      timeout: 30_000,
    });
    assert.equal(child.status, 0, child.stderr);
    const result = JSON.parse(child.stdout);
    assert.equal(result.cli, scenario.cli);
    assert.deepEqual(result.display, { DISPLAY: ":9" });
    assert.equal(result.api, scenario.api);
    assert.deepEqual(result.github, { clientId: "LegacyClient", slug: "legacy-app" });
    assert.equal(result.registry, legacy.OPENBOT_DEV_REGISTRY_DIR);
    assert.deepEqual(result.hosted, { hostId: legacy.OPENBOT_HOSTED_HOST_ID, claim: "fixture_claim" });
    assert.equal(result.claimRemoved, true);
    assert.equal(result.slack, "fixture_slack");
  }

  // Inventory every direct production read. Execute each actual fallback expression with
  // old-only, both-prefix and empty-current environments; writes and deletes are excluded.
  const reads: { file: string; name: string }[] = [];
  async function inspect(directory: string): Promise<void> {
    for (const entry of await readdir(join(projectRoot, directory), { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) {
        if (!["node_modules", ".volto-build", ".git"].includes(entry.name)) await inspect(path);
        continue;
      }
      if (!/\.(?:ts|tsx|js|mjs)$/u.test(path) || /(?:\.test\.|-e2e\.|-smoke\.)/u.test(path)) continue;
      const source = (await readFile(join(projectRoot, path), "utf8")).replace(/\s+/gu, " ");
      assert.ok(!/(?:env|environment)\.volto_[A-Z_]/u.test(source), `Invalid lowercase prefix in ${path}`);
      const pattern = /([\w.#]+\.(?:VOLTO_[A-Z_0-9]+|EXPO_PUBLIC_VOLTO_[A-Z_0-9]+))/gu;
      for (const match of source.matchAll(pattern)) {
        const start = match.index;
        const access = match[0];
        if (!/(?:\benv|\benvironment|Environment)\./u.test(access)) continue;
        if (/delete\s*$/u.test(source.slice(Math.max(0, start - 8), start))) continue;
        if (/^\s*(?:\?\?=|=(?!=))/u.test(source.slice(start + access.length))) continue;
        const legacyAccess = access.replace("VOLTO_", "OPENBOT_");
        assert.ok(
          source.slice(start).startsWith(`${access} ?? ${legacyAccess}`),
          `Missing fallback: ${path}: ${access}`,
        );
        const name = access.slice(access.lastIndexOf(".") + 1);
        const oldName = name.replace("VOLTO_", "OPENBOT_");
        // Use the expression text from the source. Replace only the receiver for isolation.
        const expression = source
          .slice(start, start + `${access} ?? ${legacyAccess}`.length)
          .replaceAll(access.slice(0, access.lastIndexOf(".")), "fixture");
        const read = new Function("fixture", `return ${expression}`);
        assert.equal(read({ [oldName]: "legacy" }), "legacy");
        assert.equal(read({ [oldName]: "legacy", [name]: "current" }), "current");
        assert.equal(read({ [oldName]: "legacy", [name]: "" }), "");
        reads.push({ file: path, name });
      }
    }
  }
  for (const directory of [
    "src",
    "scripts",
    "packages/logging",
    "apps/mobile",
    "apps/auth-api",
    "remote/api",
    "resources/cli",
  ])
    await inspect(directory);
  const report = { status: "passed", processCases: cases.map(({ name }) => name), directReads: reads.length, reads };
  await writeFile(join(reportDirectory, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
  process.stdout.write(
    `Environment compatibility passed: ${cases.length} isolated processes, ${reads.length} production reads.\n`,
  );
}
