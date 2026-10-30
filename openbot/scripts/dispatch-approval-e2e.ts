// Drives the real approval registry, grant store, dispatch service, and a local executor process.
// Run: bun scripts/dispatch-approval-e2e.ts. Report: .openbot-build/dispatch-approval-e2e.json.
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { mkdir, mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { INPUT_LIMITS } from "@openbot/contracts/input-limits";
import type { AgentApproval } from "@openbot/contracts/ipc";
import { sourceText } from "@openbot/i18n/source";
import { Effect } from "effect";
import { AttentionRegistry } from "../src/backend/agent/attention-registry";
import type { AgentClient } from "../src/backend/agent-client";
import { dispatchResultMailboxText } from "../src/backend/dispatch/dispatch-delivery";
import { assertDispatchArgs, assertDispatchInputValues } from "../src/backend/dispatch/dispatch-process";
import { type DispatchRecord, DispatchService } from "../src/backend/dispatch/dispatch-service";
import { type DispatchWork, handleDispatchTool } from "../src/backend/dispatch/dispatch-tools";
import { isMissingFileError } from "../src/backend/file-errors";
import { ApprovalAutomation } from "../src/main/approval-automation-store";

class Provider extends EventEmitter implements AgentClient {
  readonly provider = "codex";
  readonly running = true;
  start(): void {}
  stop() {
    return Effect.void;
  }
  request<T>(): Effect.Effect<T> {
    return Effect.die(new Error("This check does not call a provider."));
  }
  notify(): void {}
  respond(): void {}
  respondError(): void {}
}

const out = resolve(import.meta.dirname, "../.openbot-build/dispatch-approval-e2e.json");
await mkdir(resolve(out, ".."), { recursive: true });
const root = await mkdtemp(resolve(out, "../dispatch-approval-"));
const binary = join(root, "openswarm");
const launches = join(root, "launches.txt");
const report: { passed: boolean; checks: string[]; error?: string } = { passed: false, checks: [] };
const caller = {
  agentId: "agent-approval",
  threadId: "thread-approval",
  inExecutionThread: false,
  workspaceOnly: false,
  workspacePath: root,
  idempotencyKey: "",
};
const client = new Provider();
const automation = new ApprovalAutomation({
  path: join(root, "approval.json"),
  initial: { defaultAutoApprove: true, autoApproveOverrides: {} },
  knownAgentIds: () => [caller.agentId, "agent-other"],
});
const service = new DispatchService({
  dataPath: root,
  settings: () => ({ binaryPath: binary, defaultHost: "local", defaultLane: "" }),
  redact: (text) => text,
  publish: () => Effect.void,
  failed: () => {
    throw new Error("A dispatch event could not be saved.");
  },
});
let sequence = 0;
let expectedLaunches = 0;

async function launchCount(): Promise<number> {
  try {
    return (await readFile(launches, "utf8")).trim().split("\n").filter(Boolean).length;
  } catch (error) {
    if (isMissingFileError(error)) return 0;
    throw error;
  }
}

async function exercise(
  name: string,
  input: DispatchWork,
  answer: "accept" | "decline" | "abort" | "turn-end" | "provider-lost" | "automatic" | "safe",
  workspaceSandboxed = false,
) {
  sequence += 1;
  const requestId = `request-${sequence}`;
  const signal = new AbortController();
  const raised = Promise.withResolvers<AgentApproval>();
  const registry = new AttentionRegistry({
    conversation: {
      agentForThread: () => caller.agentId,
      publicThreadId: () => caller.threadId,
      emitConversation: () => undefined,
      ensureSnapshot: () => {
        throw new Error("This check does not read a conversation.");
      },
    },
    browser: { listTabs: () => [], beginTakeover: () => Effect.void, endTakeover: () => undefined },
    hostedSites: { prepareApproval: () => Effect.succeed(null), resolveApproval: () => Effect.void },
    routines: { markNeedsAttention: () => undefined, markRunningForTurn: () => undefined },
    approvalAutomation: automation,
    workspaceSandboxed: () => workspaceSandboxed,
    emit: (event) => {
      if (event.type === "approval") raised.resolve(event.approval);
    },
    emitError: (_code, error) => {
      throw error;
    },
    emitRuntimeSnapshot: () => undefined,
  });
  let asked = false;
  const result = Effect.runPromise(
    handleDispatchTool("dispatch_work", input, { ...caller, idempotencyKey: requestId }, service, () => {
      asked = true;
      return registry.awaitToolApproval(
        client,
        {
          id: requestId,
          method: "item/tool/call",
          params: { threadId: caller.threadId, turnId: requestId },
          signal: signal.signal,
        },
        {
          requestId,
          agentId: caller.agentId,
          threadId: caller.threadId,
          turnId: requestId,
          kind: "command",
          command: "dispatch_work",
          cwd: root,
          reason: sourceText("status.agent.dispatchApproval"),
          grantRoot: null,
          permissions: null,
        },
        { requireExplicitGrant: true },
      );
    }).pipe(Effect.timeout("10 seconds")),
  );
  if (answer !== "automatic" && answer !== "safe") {
    await Promise.race([
      raised.promise,
      result.then(() => {
        throw new Error(`${name}: the dispatch did not wait for approval.`);
      }),
    ]);
    assert.equal(await launchCount(), expectedLaunches, `${name}: an executor started before approval.`);
    const pending = registry.localAttention(caller.agentId).approvals[0];
    assert.ok(pending, `${name}: the approval must use the normal pending response path.`);
    if (answer === "abort") signal.abort();
    else if (answer === "turn-end") registry.clearForTurn(caller.threadId, requestId);
    else if (answer === "provider-lost") registry.clearApprovals(client);
    else
      await Effect.runPromise(
        registry.respondToLocalAttention(caller.agentId, {
          kind: "approval",
          requestId: pending.requestId,
          decision: answer,
        }),
      );
  }
  const response = await result;
  assert.ok(response);
  const runs = answer === "accept" || answer === "automatic" || answer === "safe";
  assert.equal(response.success, runs, name);
  assert.equal(asked, answer !== "safe", name);
  if (runs) expectedLaunches += 1;
  else
    assert.equal(
      JSON.parse(response.contentItems[0]?.text ?? "{}").error,
      sourceText(
        answer === "decline" ? "error.backend.dispatchApprovalDenied" : "error.backend.dispatchApprovalMissing",
      ),
      name,
    );
  assert.equal(await launchCount(), expectedLaunches, name);
  assert.equal(registry.localAttention(caller.agentId).approvals.length, 0, `${name}: no stale approval.`);
  report.checks.push(name);
}

try {
  await writeFile(
    binary,
    `#!/bin/sh
set -eu
case "$1" in
  run|lane)
    task_file=
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --task-file) task_file=$2; shift 2 ;;
        *) shift ;;
      esac
    done
    if [ -n "$task_file" ]; then cat "$task_file" > "$(dirname "$0")/last-card.md"; fi
    printf '%s\\n' started >> "$(dirname "$0")/launches.txt"
    printf '%s\\n' 'login shell configured'
    printf '{"id":"run-%s"}\\n' "$$"
    ;;
  fleet) printf '{"id":"%s","state":"running","result":null}\\n' "$3" ;;
  *) exit 2 ;;
esac
`,
    { mode: 0o700 },
  );
  await Effect.runPromise(service.start());
  const oversizedRecord: DispatchRecord = {
    runId: "run-oversized",
    host: "local",
    agentId: caller.agentId,
    threadId: caller.threadId,
    title: "Oversized result",
    binary,
    idempotencyKey: "oversized",
    status: "failed",
    branch: "branch",
    commit: "commit",
    startedAt: new Date(0).toISOString(),
    summary: "summary",
    verify: {
      rc: 1,
      cmd: "x".repeat(8_000),
      tail: "x".repeat(150_000),
      failing: Array.from({ length: 512 }, () => "x"),
    },
    delivered: true,
  };
  const oversizedText = dispatchResultMailboxText(oversizedRecord, (text) => text);
  assert.ok(oversizedText.length <= INPUT_LIMITS.messageText);
  assert.doesNotThrow(() => JSON.parse(oversizedText));
  report.checks.push("oversized result text stays within the mailbox limit");
  const pairedDataPath = join(root, "paired-runs");
  await mkdir(pairedDataPath, { recursive: true });
  const pairedHosts: DispatchRecord["host"][] = ["local", "remote"];
  const pairedRuns = pairedHosts.map(
    (host): DispatchRecord => ({
      runId: "run-shared",
      host,
      agentId: caller.agentId,
      threadId: caller.threadId,
      title: `${host} run`,
      binary,
      idempotencyKey: `${host}-run`,
      status: "running",
      branch: `${host}-branch`,
      commit: null,
      startedAt: new Date(0).toISOString(),
      delivered: true,
    }),
  );
  await writeFile(join(pairedDataPath, "openbot-dispatch-v1.json"), JSON.stringify({ version: 1, runs: pairedRuns }));
  const pairedService = new DispatchService({
    dataPath: pairedDataPath,
    settings: () => ({ binaryPath: binary, defaultHost: "local", defaultLane: "" }),
    redact: (text) => text,
    publish: () => Effect.void,
    failed: () => {
      throw new Error("A paired dispatch result could not be saved.");
    },
  });
  await Effect.runPromise(pairedService.start());
  assert.equal((await Effect.runPromise(pairedService.status("run-shared", caller, "local"))).branch, "local-branch");
  assert.equal((await Effect.runPromise(pairedService.status("run-shared", caller, "remote"))).branch, "remote-branch");
  const ambiguousStatus = await Effect.runPromise(Effect.result(pairedService.status("run-shared", caller)));
  assert.equal(ambiguousStatus._tag, "Failure");
  if (ambiguousStatus._tag === "Failure") {
    assert.equal(ambiguousStatus.failure.reason, sourceText("error.backend.dispatchHostRequired"));
  }
  await Effect.runPromise(pairedService.abort("run-shared", caller, "remote"));
  const pairedState: { runs: { host: string; status: string }[] } = JSON.parse(
    await readFile(join(pairedDataPath, "openbot-dispatch-v1.json"), "utf8"),
  );
  assert.equal(pairedState.runs.find((record) => record.host === "local")?.status, "running");
  assert.equal(pairedState.runs.find((record) => record.host === "remote")?.status, "cancelling");
  await Effect.runPromise(pairedService.dispose());
  report.checks.push("run status and cancel updates use the host and run ID");
  assert.doesNotThrow(() =>
    assertDispatchInputValues(
      [
        { name: "--title", value: "Fix heading" },
        { name: "--cwd", value: "C:\\Users\\Jane Doe\\OpenBot Dev" },
      ],
      "C:\\Users\\Jane Doe\\openswarm.cmd",
      "win32",
    ),
  );
  assert.doesNotThrow(() =>
    assertDispatchArgs(
      "C:\\Users\\Jane Doe\\openswarm.cmd",
      [
        "run",
        "--cwd",
        "C:\\Users\\Jane Doe\\OpenBot Dev",
        "--task-file",
        "C:\\Users\\Jane Doe\\AppData\\dispatch card.md",
        "--title",
        "Fix heading",
      ],
      "win32",
    ),
  );
  assert.throws(
    () =>
      assertDispatchInputValues([{ name: "--title", value: "normal&whoami>file&normal" }], "openswarm.cmd", "win32"),
    /--title/,
  );
  assert.throws(
    () =>
      assertDispatchArgs(
        "openswarm.cmd",
        ["run", "--task-file", "C:\\tmp\\card.md", "--title", "normal&whoami>file&normal"],
        "win32",
      ),
    /--title/,
  );
  assert.throws(
    () => assertDispatchInputValues([{ name: "--card", value: "bad\0card" }], "openswarm", "linux"),
    /--card/,
  );
  report.checks.push("Windows wrappers accept spaces and name unsafe dispatch arguments");

  const longCard = `${"x".repeat(9_999)}\n${"y".repeat(10_000)}`;
  assert.equal(longCard.length, 20_000);
  await exercise(
    "multi-line 20,000-character cards reach the executor intact",
    { title: "Long task", card: longCard },
    "safe",
  );
  assert.equal(await readFile(join(root, "last-card.md"), "utf8"), longCard);
  assert.deepEqual(await readdir(join(root, "dispatch-cards")), []);

  const task = { title: "Small change", card: "Update the heading in docs/example.md." };
  await exercise("ordinary work starts without approval", task, "safe");
  const workspaceVerify = await Effect.runPromise(
    handleDispatchTool(
      "dispatch_work",
      { ...task, verify: "printf workspace-check" },
      { ...caller, workspaceOnly: true, idempotencyKey: "workspace-verify" },
      service,
      () => {
        throw new Error("A Workspace-only verify command must not request approval or start.");
      },
    ),
  );
  assert.equal(workspaceVerify?.success, false);
  assert.equal(
    JSON.parse(workspaceVerify?.contentItems[0]?.text ?? "{}").error,
    sourceText("error.backend.dispatchVerifyWorkspace"),
  );
  assert.equal(await launchCount(), expectedLaunches);
  report.checks.push("Workspace-only verify command is blocked with a reason");
  const workspaceServiceResult = await Effect.runPromise(
    Effect.result(
      service.dispatch(
        { ...task, verify: "printf workspace-check" },
        { ...caller, workspaceOnly: true, idempotencyKey: "workspace-verify-service" },
      ),
    ),
  );
  assert.equal(workspaceServiceResult._tag, "Failure");
  if (workspaceServiceResult._tag === "Failure") {
    assert.equal(workspaceServiceResult.failure.reason, sourceText("error.backend.dispatchVerifyWorkspace"));
  }
  assert.equal(await launchCount(), expectedLaunches);
  report.checks.push("dispatch service rejects Workspace-only verify commands");
  const risks = [
    "Run a database migration.",
    "Change the schema.",
    "ALTER TABLE entries ADD COLUMN label TEXT.",
    "Create a release.",
    "Bump the version.",
    "npm version patch",
    "git tag v2.0.0",
    "git push origin HEAD",
    "git merge feature",
    "git push --force-with-lease",
    "Publish the package.",
    "Change credentials.",
    "Rotate secrets.",
    "Replace tokens.",
    "Rotate the API key.",
    "Change the password.",
    "database_migration",
    "Change sandboxing.",
    "Change IPC sender validation.",
    "Change navigation policy.",
    "Change preload.",
    "Change context isolation.",
    "Edit src/main/trusted-ipc.ts.",
    "DROP DATABASE customers;",
    "Drop the table entries.",
    "TRUNCATE TABLE entries;",
    "Truncate the database.",
    "删除数据库。",
    "清空数据表。",
    "截断表。",
    "Delete production data.",
    "Remove records from prod.",
    "删除生产环境数据。",
    "清除线上记录。",
    "DEPLOY to PRODUCTION.",
    "Production deployment.",
    "Rollout to prod.",
    "Roll out to production.",
    "Rollback production.",
    "Roll back to production.",
    "部署到生产环境。",
    "生产环境回滚。",
    "chmod 777 /etc/hosts",
    'chown root "/usr/local/bin/tool"',
    "chmod -R 777 /",
    "更改系统目录权限。",
    "修改系统文件所有者。",
    "SUDO ls /tmp",
    "使用超级用户权限。",
    "以管理员权限运行命令。",
    "systemctl restart nginx",
    "service nginx restart",
    "Restart the service.",
    "Service restart.",
    "launchctl kickstart system/example",
    "重启服务。",
    "重新启动守护进程。",
    "Change DNS records.",
    "DNS changes.",
    "Update firewall rules.",
    "Firewall changes.",
    "修改DNS记录。",
    "调整防火墙规则。",
    "Change payment settings.",
    "Payment changes.",
    "Update billing details.",
    "Billing changes.",
    "修改支付配置。",
    "调整计费设置。",
    "发布软件包。",
    "推送容器镜像。",
    "twine upload dist/*",
    "Publish the image.",
  ];
  for (const card of risks) await exercise(card, { ...task, card, risk: false }, "decline");
  await exercise(
    "whole words avoid substrings",
    { ...task, card: "Document pushpin, submerged, and tokenization." },
    "safe",
  );
  await exercise(
    "new risk words avoid substrings",
    {
      ...task,
      card: "Document dropdown, deploymentPreview, rolloutPlan, sudoku, restartable, DNSCache, firewalling, repayment, billingual, and publishable.",
    },
    "safe",
  );
  await exercise(
    "local file changes and read-only checks stay harmless",
    {
      ...task,
      card: "Deploy to staging; chmod 644 ./scripts/example.sh; chown developer ./docs/example.md; read payment and billing docs; inspect DNS and firewall settings.",
    },
    "safe",
  );
  await exercise("excluded operations still ask", { ...task, card: "Do not push or publish." }, "decline");
  await exercise("risky title", { ...task, title: "Release" }, "decline");
  await exercise("risky verify flag", { ...task, verify: "git push --force" }, "decline");
  await exercise("risky model argument", { ...task, model: "release-model" }, "decline");
  await exercise("risky lane work still asks", { ...task, lane: "M3", risk: true }, "decline");
  await exercise("risky fleet work still asks", { ...task, host: "remote", risk: true }, "decline");
  await exercise("explicit risk flag", { ...task, risk: true }, "decline");
  await exercise("an inherited default still asks", { ...task, risk: true }, "accept");
  await exercise("abandoned request starts nothing", { ...task, risk: true }, "abort");
  await exercise("unanswered turn starts nothing", { ...task, risk: true }, "turn-end");
  await exercise("lost provider starts nothing", { ...task, risk: true }, "provider-lost");
  await Effect.runPromise(automation.set({ agentId: "agent-other", autoApprove: true }));
  await exercise("another agent's grant does not apply", { ...task, risk: true }, "decline");
  await Effect.runPromise(automation.set({ agentId: caller.agentId, autoApprove: true }));
  await exercise("explicit per-agent grant permits dispatch", { ...task, risk: true }, "automatic");
  await exercise("workspace sandbox still requires approval", { ...task, risk: true }, "decline", true);
  await Effect.runPromise(automation.set({ agentId: caller.agentId, autoApprove: false }));
  await exercise("revoked grant requires approval", { ...task, risk: true }, "decline");
  report.passed = true;
} catch (error) {
  report.error = error instanceof Error ? `${error.name}: ${error.message}` : "Unknown error";
} finally {
  await Effect.runPromise(service.dispose());
  await rm(root, { recursive: true, force: true });
  await writeFile(out, `${JSON.stringify(report, null, 2)}\n`);
}
// Machine-readable: the report contains test outcomes, with no provider or user data.
process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
if (!report.passed) process.exitCode = 1;
