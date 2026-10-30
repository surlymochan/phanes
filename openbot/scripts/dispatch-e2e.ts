// Drives dispatch_work through a real dev app and a signed-in agent. The stub openswarm command
// accepts the run only after approval, then returns a terminal result. Start one worktree dev app first:
// `bun run dev`; then run `bun scripts/dispatch-e2e.ts --agent=<agent id> --allow-mutations`.
// This adds one test message to the agent's chat. It writes .openbot-build/dispatch-e2e.json.
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { chmod, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import type { AgentApproval, OpenBotDesktopApi, ProviderDetectionSettings } from "@openbot/contracts/ipc";
import { createOpenBotLogger } from "@openbot/logging";
import { assertMutationAllowed, connectToDevApp } from "./dev-automation/cdp-client";
import { readDevInstanceRecords } from "./dev-automation/instance-registry";

declare global {
  interface Window {
    openbot: OpenBotDesktopApi;
    __dispatchE2EWait?: { done: boolean; failure: string | null };
    __dispatchE2EInterval?: number;
    __dispatchE2EApproval?: AgentApproval;
    __dispatchE2EUnsubscribe?: () => void;
  }
}

const OUT = resolve(import.meta.dirname, "../.openbot-build/dispatch-e2e.json");
const runId = `dispatch-e2e-${randomUUID()}`;
const messageKey = createHash("sha256").update(`local:${runId}`).digest("hex").slice(0, 32);
const marker = `DISPATCH_E2E_${runId.replaceAll("-", "_")}`;
const agentId = process.argv.find((argument) => argument.startsWith("--agent="))?.slice("--agent=".length) ?? "";
const stubDirectory = resolve(import.meta.dirname, `../.openbot-build/${runId}`);
const stubBinary = join(stubDirectory, "openswarm");
interface DispatchE2EReport {
  runId: string;
  agentId: string;
  checks: Array<{ name: string; passed: boolean; detail?: unknown }>;
  passed: boolean;
}

const result: DispatchE2EReport = {
  runId,
  agentId,
  checks: [],
  passed: false,
};
const logger = createOpenBotLogger("dispatch-e2e");
const check = (name: string, passed: boolean, detail?: unknown) => result.checks.push({ name, passed, detail });
let session: Awaited<ReturnType<typeof connectToDevApp>> | null = null;
let originalSettings: ProviderDetectionSettings | null = null;

function shellLiteral(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

function stubSource(): string {
  const resultJson = JSON.stringify(
    {
      id: runId,
      status: "done",
      branch: `openswarm/${runId}`,
      commit: "d15ca11d15ca11d15ca11d15ca11d15ca11d15ca11d",
      result: { summary: marker },
      verify: { rc: 0, cmd: "bun run test:dispatch", tail: "DISPATCH_E2E_VERIFY_OK", seconds: 1, failing: [] },
    },
    null,
    2,
  );
  const acknowledgementJson = JSON.stringify(
    {
      ok: true,
      id: runId,
      state: "queued",
      lane: "M1",
      watch: `openswarm watch ${runId}`,
      result: `${stubDirectory}/runs/${runId}/result.json`,
    },
    null,
    2,
  );
  const root = shellLiteral(stubDirectory);
  const id = shellLiteral(runId);
  const json = shellLiteral(resultJson);
  const acknowledgement = shellLiteral(acknowledgementJson);
  return `#!/bin/sh
set -eu
root=${root}
run_id=${id}
acknowledgement=${acknowledgement}
run_dir="$root/runs/$run_id"
result_file="$run_dir/result.json"
mkdir -p "$run_dir"
case "\${1-}" in
  run|lane)
    printf '%s\\n' "$@" > "$root/arguments.txt"
    printf '{"id":"%s","state":"running","result":null}\\n' "$run_id" > "$run_dir/status.json"
    printf '%s\\n' 'login shell configured'
    printf '%s\\n' "$acknowledgement"
    printf '%s\\n' ${json} > "$result_file"
    ;;
  fleet)
    if [ "\${2-}" != "_run-info" ] || [ "\${3-}" != "$run_id" ]; then exit 2; fi
    if [ -f "$root/release-result" ] && [ -f "$result_file" ]; then
      result=$(cat "$result_file")
      printf 'login shell configured\\n{\\n  "id": "%s",\\n  "state": "finished",\\n  "result": %s\\n}\\n' "$run_id" "$result"
    else
      printf 'login shell configured\\n{\\n  "id": "%s",\\n  "state": "running",\\n  "result": null\\n}\\n' "$run_id"
    fi
    ;;
  stop)
    printf '%s\\n' 'login shell configured'
    printf '{\\n  "ok": true,\\n  "id": "%s",\\n  "state": "stopped"\\n}\\n' "$run_id"
    ;;
  *) exit 2 ;;
esac
`;
}

async function readMessages() {
  assert.ok(session);
  return session.page.evaluate(
    (id) => window.openbot.agent.readConversationPage({ agentId: id, anchor: { type: "latest" }, limit: 100 }),
    agentId,
  );
}

try {
  assert.ok(agentId, "Pass --agent=<id> for an agent with a signed-in provider.");
  await mkdir(resolve(import.meta.dirname, "../.openbot-build"), { recursive: true });
  await mkdir(stubDirectory, { recursive: true });
  await writeFile(stubBinary, stubSource(), { mode: 0o700 });
  await chmod(stubBinary, 0o700);

  const instances = readDevInstanceRecords().filter(
    (record) => record.service === "app" && resolve(record.projectRoot) === resolve(process.cwd()),
  );
  assert.equal(instances.length, 1, "Start one `bun run dev` app for this worktree first.");
  const instance = instances[0];
  assert.ok(instance);
  assertMutationAllowed({
    command: "dispatch-e2e",
    allowMutations: process.argv.includes("--allow-mutations"),
    instanceNamed: true,
    target: instance.instanceId,
  });
  session = await connectToDevApp(instance.remoteDebuggingPort, logger, {
    expectedRendererPort: instance.rendererPort,
    ownerPid: instance.pid,
  });

  originalSettings = await session.page.evaluate(() => window.openbot.providerDetection.getSettings());
  await session.page.evaluate(
    ({ settings, binaryPath }) =>
      window.openbot.providerDetection.setSettings({
        ...settings,
        openswarm: { binaryPath, defaultLane: "", defaultHost: "local" },
      }),
    { settings: originalSettings, binaryPath: stubBinary },
  );

  const agent = await session.page.evaluate(
    async (id) => (await window.openbot.agent.listAgents()).find((candidate) => candidate.id === id) ?? null,
    agentId,
  );
  assert.ok(agent, `The dev app has no agent with id ${agentId}.`);
  const automation = await session.page.evaluate(() => window.openbot.getApprovalAutomation());
  assert.notEqual(
    automation.autoApproveOverrides[agentId],
    true,
    "Use a test agent without an explicit auto-approve grant. The test does not change that grant.",
  );
  await session.page.evaluate((id) => {
    delete window.__dispatchE2EApproval;
    window.__dispatchE2EUnsubscribe = window.openbot.agent.onEvent((event) => {
      if (
        event.type === "approval" &&
        event.approval.agentId === id &&
        event.approval.command?.includes('"tool": "dispatch_work"')
      )
        window.__dispatchE2EApproval = event.approval;
    });
  }, agentId);
  const card = [
    "# Dispatch E2E",
    "",
    "## Goal",
    "Exercise approval for a version bump and the dispatch result delivery path. The executor is a test stub. Do not edit files.",
    "",
    "## Files",
    "None.",
    "",
    "## Constraints",
    "Do not edit files, commit, push, merge, or publish.",
    "",
    "## Acceptance",
    `Return the marker ${marker} in the summary.`,
    "",
    "## Verify",
    "Return DISPATCH_E2E_VERIFY_OK with rc 0.",
  ].join("\n");
  const userText = [
    "Run the dispatch E2E flow in this chat.",
    "Use openbot.tool_search with the query dispatch_work, describe openbot.dispatch_work, then call openbot.tool_call for that tool.",
    "Set risk=true. Wait for the approval card to be answered; do not ask for approval with another tool.",
    `Use title ${JSON.stringify(`Dispatch E2E ${runId}`)} and this exact card:`,
    card,
    "After the tool accepts the run, reply DISPATCH_E2E_STARTED and end this turn.",
    `When the result arrives in a later turn, review it and reply with its status, branch, commit, summary and verify result. Include the exact marker ${marker}.`,
  ].join("\n\n");
  await session.page.evaluate(
    ({ id, text }) => window.openbot.agent.sendMessage({ agentId: id, text, clientMessageId: crypto.randomUUID() }),
    { id: agentId, text: userText },
  );

  await session.page.waitForFunction(() => Boolean(window.__dispatchE2EApproval), undefined, { timeout: 120_000 });
  const startedBeforeApproval = existsSync(join(stubDirectory, "arguments.txt"));
  check("risky work does not start before approval", !startedBeforeApproval);
  assert.equal(startedBeforeApproval, false, "The executor must not start before approval.");
  await session.page.evaluate(async () => {
    const approval = window.__dispatchE2EApproval;
    if (!approval) throw new Error("The dispatch approval is missing.");
    await window.openbot.agent.respondToApproval({ requestId: approval.requestId, decision: "accept" });
  });
  await writeFile(join(stubDirectory, "release-result"), "approved\n");

  await session.page.evaluate(
    ({ id, key, expectedMarker }) => {
      const state: { done: boolean; failure: string | null } = { done: false, failure: null };
      window.__dispatchE2EWait = state;
      const check = async () => {
        const page = await window.openbot.agent.readConversationPage({
          agentId: id,
          anchor: { type: "latest" },
          limit: 100,
        });
        const resultIndex = page.messages.findIndex(
          (message) => message.id === `dispatch-${key}-result` && message.itemType === "dispatch",
        );
        const review = page.messages
          .slice(resultIndex + 1)
          .find(
            (message) =>
              message.author === "assistant" && message.status === "completed" && message.text.includes(expectedMarker),
          );
        if (resultIndex >= 0 && review) {
          state.done = true;
        } else {
          const userIndex = page.messages.findIndex(
            (message) => message.author === "user" && message.text.includes(expectedMarker),
          );
          const reply = page.messages
            .slice(userIndex + 1)
            .findLast((message) => message.author === "assistant" && message.status === "completed");
          if (reply && !reply.text.includes("DISPATCH_E2E_STARTED")) state.failure = reply.text;
        }
        if (state.done || state.failure) window.clearInterval(window.__dispatchE2EInterval);
      };
      void check();
      window.__dispatchE2EInterval = window.setInterval(() => void check(), 500);
    },
    { id: agentId, key: messageKey, expectedMarker: marker },
  );
  await session.page.waitForFunction(
    () => Boolean(window.__dispatchE2EWait?.done || window.__dispatchE2EWait?.failure),
    undefined,
    { timeout: 180_000, polling: 100 },
  );
  const waitResult = await session.page.evaluate(() => window.__dispatchE2EWait);
  if (waitResult?.failure) throw new Error(`The agent did not start dispatch: ${waitResult.failure}`);

  const conversation = await readMessages();
  const startedCard = conversation.messages.some(
    (message) => message.id === `dispatch-${messageKey}-started` && message.itemType === "dispatch",
  );
  const resultCard = conversation.messages.find(
    (message) => message.id === `dispatch-${messageKey}-result` && message.itemType === "dispatch",
  );
  const resultIndex = conversation.messages.findIndex((message) => message.id === `dispatch-${messageKey}-result`);
  const review = conversation.messages
    .slice(resultIndex + 1)
    .find(
      (message) => message.author === "assistant" && message.status === "completed" && message.text.includes(marker),
    );
  const args = await readFile(join(stubDirectory, "arguments.txt"), "utf8");
  check("openswarm accepted the detached run", args.includes("run") && args.includes("--task-file"), args);
  check("risky work starts after approval", existsSync(join(stubDirectory, "arguments.txt")));
  check("the start card is in the chat", startedCard);
  check("the terminal status card is in the same chat", resultCard !== undefined, resultCard?.text);
  check("the result woke the agent in the same chat", review !== undefined, review?.text);
  const reviewText = review?.text ?? "";
  const reviewedFields = [
    "done",
    `openswarm/${runId}`,
    "d15ca11d15ca11d15ca11d15ca11d15ca11d15ca11d",
    marker,
    "DISPATCH_E2E_VERIFY_OK",
  ].every((field) => reviewText.includes(field));
  check("the agent reported the result and verify fields", reviewedFields, reviewText);
  result.passed = result.checks.every((item) => item.passed);
} catch (error) {
  check("dispatch E2E completed", false, error instanceof Error ? `${error.name}: ${error.message}` : "unknown error");
} finally {
  if (session) {
    await session.page
      .evaluate(() => {
        window.clearInterval(window.__dispatchE2EInterval);
        window.__dispatchE2EUnsubscribe?.();
        delete window.__dispatchE2EUnsubscribe;
        delete window.__dispatchE2EApproval;
        delete window.__dispatchE2EInterval;
        delete window.__dispatchE2EWait;
      })
      .catch(() => undefined);
  }
  if (session && originalSettings) {
    await session.page
      .evaluate((settings) => window.openbot.providerDetection.setSettings(settings), originalSettings)
      .catch(() => undefined);
  }
  if (session) await session.close().catch(() => undefined);
  await rm(stubDirectory, { recursive: true, force: true }).catch(() => undefined);
  await mkdir(resolve(OUT, ".."), { recursive: true });
  await writeFile(OUT, `${JSON.stringify(result, null, 2)}\n`);
}

process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
if (!result.passed) process.exitCode = 1;
