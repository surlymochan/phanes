// Runs agent tasks end to end in this worktree's running dev app, through the real preload bridge,
// IPC, agent service and provider. It changes the data of the profile, so start the app with
// `bun run dev` (never `--shared`), and pass the id of an agent whose provider is signed in and runs
// turns at the same time (not a custom ACP agent):
// `bun scripts/agent-tasks-e2e.ts --allow-mutations --agent=<agent id>`.
//
// The file is also the test of the completion of a goal: `bun test ./scripts/agent-tasks-e2e.ts`
// runs the checks that need no app (the bounds of the completion, and the IPC decoder that keeps a
// completion outside them out) and runs the live check only when the app of this worktree and an
// agent id are there. Without them the live check is skipped, so the command passes on a machine
// that has no signed-in provider.
//
// It checks the task limit and its hold, the completion of a task that runs, of a task that finished
// and of a task that was stopped, a raised limit that starts the waiting task, Stop, a provider
// switch queued while a task runs, and that the task list reports the `error` field. Each task turn
// is a real provider turn, so keep the prompts short. At the end it stops and closes the tasks it
// started and gives the agent its task limit back.
// Writes .volto-build/agent-tasks-e2e/report.json.
import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { agentProviderDescriptor } from "@volto/contracts/agent-providers";
import {
  AGENT_TASK_COMPLETION_MAX,
  type AgentSummary,
  type AgentTask,
  type AgentTasksSnapshot,
  type ClientAgentSummary,
  isAgentModelOption,
  isAgentSummary,
  isAgentTaskCompletion,
  type VoltoDesktopApi,
} from "@volto/contracts/ipc";
import { createVoltoLogger } from "@volto/logging";
import { decodeAgentTasks } from "../src/preload/conversation-decoding";
import { assertMutationAllowed, connectToDevApp } from "./dev-automation/cdp-client";
import { readDevInstanceRecords } from "./dev-automation/instance-registry";

// The page functions below run in the app window, where the preload bridge sets this.
declare global {
  interface Window {
    volto: VoltoDesktopApi;
  }
}

const OUT = resolve(import.meta.dirname, "../.volto-build/agent-tasks-e2e");
const argument = (name: string) => process.argv.find((arg) => arg.startsWith(`--${name}=`))?.slice(`--${name}=`.length);
// The agent of the live check. Empty when the caller named none, which only the live check needs.
const agent = argument("agent") ?? "";

// Long enough that the first task still runs while the script reads the second one. It calls a tool,
// so the task reports the progress that the completion of its goal follows.
const LONG_PROMPT = "Call openbot.list_agents, then write a numbered list of 40 short facts about rivers.";
const SHORT_PROMPT = "Reply with the single word ready. Do not use tools.";
const WAIT_MS = 120_000;

interface Report {
  agentId: string;
  provider?: string;
  limitOne?: { first: string; second: string; secondHold: string | null; limit: number };
  raisedLimit?: { second: string; limit: number };
  stop?: { states: string[]; outcomes: Array<string | null> };
  providerSwitch?: {
    refused: boolean;
    target: string | null;
    provider?: string;
    pendingProvider?: string | null;
    pendingModel?: string | null;
  };
  errorField?: { present: boolean };
  completion?: { running: number | null; completed: number | null; stopped: number | null; invalidRejected: boolean };
  restored?: { taskLimit: number | null };
  passed: boolean;
}

const report: Report = { agentId: agent, passed: false };
const started: string[] = [];

/** A task of the list, with the fields the decoder of the preload reads. */
const taskWithCompletion = (completion: unknown) => ({
  id: "agent-task-1",
  agentId: "agent-1",
  threadId: "openbot-thread-1",
  origin: "user",
  title: "A task",
  state: "idle",
  outcome: null,
  hold: null,
  activeTurnId: null,
  progress: null,
  completion,
  error: null,
  queuedMessages: [],
  createdAt: "2026-01-01T00:00:00.000Z",
  updatedAt: "2026-01-01T00:00:00.000Z",
  closedAt: null,
});

/**
 * The checks that need no app: the completion of a task is an integer from 0 to 100, and the decoder
 * of the preload keeps a completion outside those bounds out of the renderer.
 */
const checkCompletionBounds = (): void => {
  const snapshot = (completion: unknown) => ({ agentId: "agent-1", tasks: [taskWithCompletion(completion)], limit: 2 });
  assert.equal(isAgentTaskCompletion(0), true);
  assert.equal(isAgentTaskCompletion(AGENT_TASK_COMPLETION_MAX), true);
  assert.equal(isAgentTaskCompletion(AGENT_TASK_COMPLETION_MAX + 1), false);
  assert.equal(isAgentTaskCompletion(-1), false);
  assert.equal(isAgentTaskCompletion(1.5), false);
  assert.equal(isAgentTaskCompletion("50"), false);
  // The decoder of the preload rejects the same values, so a completion outside the bounds never
  // reaches the renderer.
  assert.throws(() => decodeAgentTasks(snapshot(AGENT_TASK_COMPLETION_MAX + 1)), /Invalid task list response\./);
  assert.throws(() => decodeAgentTasks(snapshot(-1)), /Invalid task list response\./);
  assert.throws(() => decodeAgentTasks(snapshot(1.5)), /Invalid task list response\./);
  // A completion inside the bounds reaches the renderer.
  assert.equal(decodeAgentTasks(snapshot(0)).tasks[0]?.completion, 0);
  assert.equal(decodeAgentTasks(snapshot(50)).tasks[0]?.completion, 50);
  assert.equal(decodeAgentTasks(snapshot(AGENT_TASK_COMPLETION_MAX)).tasks[0]?.completion, AGENT_TASK_COMPLETION_MAX);
};

/** The live check: the tasks of a real agent, through the real app. */
const runLiveScenario = async (): Promise<void> => {
  assert.ok(agent !== "", "Pass --agent=<agent id> of an agent whose provider is signed in.");
  const instances = readDevInstanceRecords().filter(
    (record) => record.service === "app" && resolve(record.projectRoot) === resolve(process.cwd()),
  );
  assert.equal(instances.length, 1, "Start one dev app for this worktree first.");
  const instance = instances[0];
  assert.ok(instance);
  assertMutationAllowed({
    command: "agent-tasks-e2e",
    allowMutations: process.argv.includes("--allow-mutations"),
    instanceNamed: true,
    target: instance.instanceId,
  });
  const session = await connectToDevApp(instance.remoteDebuggingPort, createVoltoLogger("agent-tasks-e2e"), {
    expectedRendererPort: instance.rendererPort,
    ownerPid: instance.pid,
  });
  const page = session.page;

  function listTasks(): Promise<AgentTasksSnapshot> {
    return page.evaluate((id) => window.volto.agent.listTasks(id), agent);
  }

  async function readAgent(): Promise<AgentSummary> {
    const summary = await page.evaluate(async (id) => {
      const found = (await window.volto.agent.listAgents()).find((candidate) => candidate.id === id);
      if (!found) throw new Error(`Unknown agent: ${id}`);
      return found;
    }, agent);
    if (!isAgentSummary(summary)) throw new Error(`The agent ${agent} has a provider that this build does not know.`);
    return summary;
  }

  function setTaskLimit(taskLimit: number | null): Promise<ClientAgentSummary> {
    return page.evaluate(({ id, limit }) => window.volto.agent.updateAgent({ agentId: id, taskLimit: limit }), {
      id: agent,
      limit: taskLimit,
    });
  }

  async function startTask(text: string): Promise<AgentTask> {
    const task = await page.evaluate(
      ({ id, prompt }) =>
        window.volto.agent.startTask({ agentId: id, text: prompt, clientMessageId: crypto.randomUUID() }),
      { id: agent, prompt: text },
    );
    started.push(task.id);
    return task;
  }

  /** Reads the task list until `ready` accepts it. The app pushes no event to this script, so it reads again. */
  async function waitForTasks(
    label: string,
    ready: (snapshot: AgentTasksSnapshot) => boolean,
  ): Promise<AgentTasksSnapshot> {
    const deadline = Date.now() + WAIT_MS;
    for (;;) {
      const snapshot = await listTasks();
      if (ready(snapshot)) return snapshot;
      if (Date.now() > deadline) throw new Error(`Timed out: ${label}. Tasks: ${JSON.stringify(snapshot.tasks)}`);
      await page.waitForTimeout(250);
    }
  }

  const taskIn = (snapshot: AgentTasksSnapshot, taskId: string): AgentTask => {
    const task = snapshot.tasks.find((candidate) => candidate.id === taskId);
    assert.ok(task, `Task ${taskId} is not in the list.`);
    return task;
  };

  const taskCompletion = (task: AgentTask): number | null => task.completion ?? null;

  const before = await readAgent();
  report.provider = before.provider;
  assert.ok(
    agentProviderDescriptor(before.provider).concurrentTurns,
    `${before.provider} runs one turn at a time. Choose an agent on another provider.`,
  );
  const originalLimit = before.taskLimit ?? null;

  try {
    // 1. With a limit of 1, the second task waits for the first one, and says why.
    await setTaskLimit(1);
    const first = await startTask(LONG_PROMPT);
    const progressing = await waitForTasks(
      "the first task reports progress",
      (snapshot) => taskIn(snapshot, first.id).state === "running" && taskIn(snapshot, first.id).progress !== null,
    );
    const runningCompletion = taskCompletion(taskIn(progressing, first.id));
    assert.ok(
      typeof runningCompletion === "number" &&
        Number.isInteger(runningCompletion) &&
        runningCompletion > 0 &&
        runningCompletion < AGENT_TASK_COMPLETION_MAX,
      `A running task with progress must report an integer completion from 1 to 99. Got ${String(runningCompletion)}.`,
    );
    const second = await startTask(SHORT_PROMPT);
    const held = await waitForTasks(
      "the second task waits",
      (snapshot) => taskIn(snapshot, second.id).state === "queued" && taskIn(snapshot, first.id).state === "running",
    );
    report.limitOne = {
      first: taskIn(held, first.id).state,
      second: taskIn(held, second.id).state,
      secondHold: taskIn(held, second.id).hold,
      limit: held.limit,
    };
    assert.equal(held.limit, 1);
    assert.equal(taskIn(held, second.id).hold, "task-limit");

    assert.throws(
      () =>
        decodeAgentTasks({
          ...held,
          tasks: held.tasks.map((task) =>
            task.id === first.id ? { ...task, completion: AGENT_TASK_COMPLETION_MAX + 1 } : task,
          ),
        }),
      /Invalid task list response\./,
      "A task completion above 100 must be rejected by the preload IPC decoder.",
    );

    // 2. A higher limit starts the waiting task while the first one still runs.
    await setTaskLimit(2);
    const raised = await waitForTasks(
      "the second task starts",
      (snapshot) => taskIn(snapshot, second.id).state !== "queued",
    );
    report.raisedLimit = { second: taskIn(raised, second.id).state, limit: raised.limit };
    assert.equal(raised.limit, 2);

    const completed = await waitForTasks(
      "the short task completes",
      (snapshot) => taskIn(snapshot, second.id).state === "idle" && taskIn(snapshot, second.id).outcome === "completed",
    );
    const completedCompletion = taskCompletion(taskIn(completed, second.id));
    assert.equal(
      completedCompletion,
      AGENT_TASK_COMPLETION_MAX,
      "A completed task must report 100 percent completion.",
    );
    report.completion = {
      running: runningCompletion,
      completed: completedCompletion,
      stopped: null,
      invalidRejected: true,
    };

    // 3. A provider switch while a task runs is queued for the next turn.
    const models = await page.evaluate(() => window.volto.agent.listModels());
    const target = models.filter(isAgentModelOption).find((model) => model.provider !== before.provider) ?? null;
    const running = await listTasks();
    if (target && taskIn(running, first.id).state === "running") {
      const outcome = await page.evaluate(
        async ({ id, provider, model }) => {
          const agent = await window.volto.agent.updateAgent({ agentId: id, provider, model });
          return {
            refused: false,
            provider: agent.provider,
            pendingProvider: agent.pendingRuntime?.provider ?? null,
            pendingModel: agent.pendingRuntime?.model ?? null,
          };
        },
        { id: agent, provider: target.provider, model: target.id },
      );
      report.providerSwitch = { ...outcome, target: `${target.provider}/${target.id}` };
      assert.equal(outcome.refused, false, "The provider switch is queued while a task runs.");
      assert.equal(outcome.provider, before.provider, "The running provider stays until the task ends.");
      assert.equal(outcome.pendingProvider, target.provider);
      assert.equal(outcome.pendingModel, target.id);
      await page.evaluate(
        ({ id, provider, model }) => window.volto.agent.updateAgent({ agentId: id, provider, model }),
        { id: agent, provider: before.provider, model: before.model },
      );
    } else {
      report.providerSwitch = { refused: false, target: null };
    }

    // 4. Stop ends each task's turn as interrupted.
    for (const taskId of [first.id, second.id]) {
      await page.evaluate(({ id, task }) => window.volto.agent.stopTask({ agentId: id, taskId: task }), {
        id: agent,
        task: taskId,
      });
    }
    const stopped = await waitForTasks("both tasks stop", (snapshot) =>
      [first.id, second.id].every((taskId) => taskIn(snapshot, taskId).state === "idle"),
    );
    report.stop = {
      states: [first.id, second.id].map((taskId) => taskIn(stopped, taskId).state),
      outcomes: [first.id, second.id].map((taskId) => taskIn(stopped, taskId).outcome),
    };
    // The second task can finish its short turn before Stop reaches it.
    assert.equal(taskIn(stopped, first.id).outcome, "interrupted");

    // A task that was stopped keeps the percent its goal reached: the goal did not finish.
    const stoppedCompletion = taskCompletion(taskIn(stopped, first.id));
    assert.notEqual(
      stoppedCompletion,
      AGENT_TASK_COMPLETION_MAX,
      "A stopped task must not report 100 percent completion.",
    );
    if (report.completion) report.completion.stopped = stoppedCompletion ?? null;

    // 5. The list reports the stored error of each task: null after a turn that did not fail.
    report.errorField = { present: stopped.tasks.every((task) => "error" in task) };
    assert.ok(report.errorField.present);
    report.passed = true;
  } finally {
    for (const taskId of started) {
      await page
        .evaluate(({ id, task }) => window.volto.agent.stopTask({ agentId: id, taskId: task }), {
          id: agent,
          task: taskId,
        })
        .catch(() => undefined);
    }
    if (started.length > 0) {
      await waitForTasks("the started tasks stop", (snapshot) =>
        started.every((taskId) => taskIn(snapshot, taskId).state !== "running"),
      ).catch(() => undefined);
    }
    for (const taskId of started) {
      await page
        .evaluate(({ id, task }) => window.volto.agent.closeTask({ agentId: id, taskId: task }), {
          id: agent,
          task: taskId,
        })
        .catch(() => undefined);
    }
    const restored = await setTaskLimit(originalLimit);
    report.restored = { taskLimit: restored.taskLimit ?? null };
    mkdirSync(OUT, { recursive: true });
    writeFileSync(resolve(OUT, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
    await session.close();
  }

  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  if (!report.passed) process.exitCode = 1;
};

// `bun test` runs this file as a test file: the call to `test` works there and throws outside it,
// where the script runs the live scenario itself.
const { test } = await import("bun:test");
let underTestRunner = false;
try {
  test("the completion of a task stays between 0 and 100", () => {
    checkCompletionBounds();
  });
  underTestRunner = true;
} catch {
  underTestRunner = false;
}

if (underTestRunner) {
  // The live check needs the app of this worktree and an agent whose provider is signed in.
  test.skipIf(agent === "")("the live task list reports the completion of a goal", async () => {
    await runLiveScenario();
  });
} else {
  checkCompletionBounds();
  await runLiveScenario();
}
