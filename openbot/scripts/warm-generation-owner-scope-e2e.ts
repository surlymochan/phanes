// Checks that the warm generation client of a provider ends only with its own provider (plan 006).
// Runs the real warm generation owner with two real providers, each with a fake ACP agent of its
// own. Both providers run one warm generation, so each has a warm client with a process. The warm
// client of the first provider ends when the main client of that provider stops, and the warm
// client of the second provider keeps running.
// `bun scripts/warm-generation-owner-scope-e2e.ts`.
// Writes .openbot-build/warm-generation-owner-scope-e2e/report.json.
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import type { AgentModelOption, AgentProviderId } from "@openbot/contracts/ipc";
import { ProfileClients } from "../src/backend/agent/profile-clients";
import { WarmGenerations } from "../src/backend/agent/warm-generations";
import type { AgentClient } from "../src/backend/agent-client";
import type { AgentCliInfo } from "../src/backend/cli";
import { runCauseEffect } from "../src/backend/effect-boundary";
import { NO_PROVIDER_CREDENTIALS, requireProviderDriver } from "../src/backend/provider-drivers";
import { installFakeAgent, readJsonl, stopClients } from "./provider-fakes/host";

const OUT = resolve(import.meta.dirname, "../.openbot-build/warm-generation-owner-scope-e2e");

/** The two providers of this check. The first one stops, the second one must keep running. */
const PROVIDERS = ["cursor", "opencode"] as const satisfies readonly AgentProviderId[];

const checks: { name: string; passed: boolean; detail?: unknown }[] = [];
function check(name: string, passed: boolean, detail?: unknown) {
  checks.push({ name, passed, ...(detail === undefined ? {} : { detail }) });
}
const report: { passed: boolean; checks: typeof checks; error?: string } = { passed: false, checks };

const root = await mkdtemp(join(tmpdir(), "openbot-warm-generation-owner-scope-"));
const clients: AgentClient[] = [];
const owners = new Map<AgentProviderId, AgentClient>();
const clis = new Map<AgentProviderId, AgentCliInfo>();
const logs = new Map<AgentProviderId, string>();

/** True while the process runs. */
function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

/** Waits for a process to end; a stopped client resolves before the kernel reaps it. */
async function ended(pid: number): Promise<boolean> {
  for (let tries = 0; tries < 200 && alive(pid); tries++) await new Promise((done) => setTimeout(done, 25));
  return !alive(pid);
}

/** The process of the warm client of a provider. The fake agent logs its process id. */
async function warmPid(provider: AgentProviderId): Promise<number> {
  const entries = await readJsonl<{ pid?: number }>(logs.get(provider) ?? "");
  return entries.find((entry) => typeof entry.pid === "number")?.pid ?? 0;
}

/** The model of a provider. The fake agent lists it. */
function model(provider: AgentProviderId): AgentModelOption {
  return {
    provider,
    id: "fast",
    name: "Fast",
    description: "",
    defaultReasoningEffort: "medium",
    supportedReasoningEfforts: ["medium"],
  };
}

const warm = new WarmGenerations({
  ownerFor: (provider) => owners.get(provider) ?? null,
  createClient: (provider) => {
    const cli = clis.get(provider);
    if (!cli) throw new Error(`The provider ${provider} has no CLI.`);
    const driver = requireProviderDriver(provider);
    const client = (driver.createProfileClient ?? driver.createClient)(cli, 10_000, NO_PROVIDER_CREDENTIALS);
    clients.push(client);
    return client;
  },
  profileClients: new ProfileClients(),
});

try {
  // Each provider gets a fake agent of its own, so each warm client runs in a process of its own.
  for (const provider of PROVIDERS) {
    const log = join(root, `${provider}.jsonl`);
    logs.set(provider, log);
    const executable = join(root, provider, "bin", provider);
    await mkdir(join(root, provider, "bin"), { recursive: true });
    await installFakeAgent(executable, "custom-acp", { FAKE_LOG: log, FAKE_TITLE: provider, FAKE_MODELS: "1" });
    const cli: AgentCliInfo = { executable, version: "1.2.3", source: "managed" };
    clis.set(provider, cli);
    const owner = requireProviderDriver(provider).createClient(cli, 10_000, NO_PROVIDER_CREDENTIALS);
    clients.push(owner);
    owners.set(provider, owner);
  }

  // Each provider runs one warm generation, so each one has a warm client.
  const answers = new Map<AgentProviderId, string>();
  for (const provider of PROVIDERS) {
    answers.set(provider, await runCauseEffect(warm.generate(provider, model(provider), "Say ok.")));
  }
  const pids = new Map<AgentProviderId, number>();
  for (const provider of PROVIDERS) pids.set(provider, await warmPid(provider));
  check(
    "each provider answers on a process of its own",
    PROVIDERS.every((provider) => {
      const answer = answers.get(provider) ?? "";
      const pid = pids.get(provider) ?? 0;
      return answer.includes(provider) && pid > 0 && alive(pid);
    }),
    { answers: Object.fromEntries(answers), pids: Object.fromEntries(pids) },
  );

  const [stopped, other] = PROVIDERS;
  const stoppedPid = pids.get(stopped) ?? 0;
  const otherPid = pids.get(other) ?? 0;
  const stoppedOwner = owners.get(stopped);
  if (!stoppedOwner) throw new Error(`The provider ${stopped} has no main client.`);

  // The main client of the first provider stops, as it does when its process is lost.
  await runCauseEffect(warm.ownerStopped(stoppedOwner));
  check("the warm client of the stopped provider ends", await ended(stoppedPid), { pid: stoppedPid });
  check("the warm client of the other provider keeps running", alive(otherPid), { pid: otherPid });

  // The shutdown stops the warm client that is left.
  await runCauseEffect(warm.dispose());
  check("the shutdown stops the warm client of the other provider", await ended(otherPid), { pid: otherPid });

  report.passed = checks.every((entry) => entry.passed);
} catch (error) {
  report.error = error instanceof Error ? (error.stack ?? error.message) : String(error);
} finally {
  await stopClients(clients);
  await mkdir(OUT, { recursive: true });
  await writeFile(join(OUT, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
  await rm(root, { recursive: true, force: true });
  for (const entry of checks) process.stdout.write(`${entry.passed ? "PASS" : "FAIL"} ${entry.name}\n`);
  if (report.error) process.stdout.write(`${report.error}\n`);
  process.stdout.write(`Report: ${join(OUT, "report.json")}\n`);
  if (!report.passed) process.exitCode = 1;
}
