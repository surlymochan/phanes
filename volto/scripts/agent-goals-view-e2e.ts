// The goals of an agent: the goals it works on now, the goals that are done, and the completion of
// each. The thread reads the goals from the task snapshot of the agent, so this check drives the
// modules the app renders: the projection that reads the snapshot, and the goals view that the
// thread mounts. The keyboard moves between the two groups.
//
// Run: `bun test scripts/agent-goals-view-e2e.ts`. Bun only discovers a file whose name carries
// `.test` or `.spec`, so `scripts/agent-goals-view-e2e.ts.test.tsx` runs this file, and the command
// of the issue finds the checks with and without the path of the file.

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import type { AgentTask, AgentTasksSnapshot } from "@volto/contracts/ipc";
import {
  AGENT_GOALS_PANEL_ID,
  AGENT_GOALS_TAB_IDS,
  agentGoalCompletion,
  agentGoalCompletionText,
  agentGoalsForView,
  mountAgentGoalsView,
} from "../packages/ui/src/features/conversation/threads/agent-goals-view";

const require = createRequire(import.meta.url);
const { JSDOM }: { JSDOM: new (html: string) => { window: Window & typeof globalThis } } = require("jsdom");

/**
 * The projection of the renderer that reads the task snapshot of an agent. The scripts are type
 * checked without the renderer, whose views the checker of the scripts cannot follow, so the check
 * calls the projection through `require` and states the shape of the threads it reads.
 */
const projection: {
  conversationThreads: (input: {
    agent: { id: string; updatedAt: string };
    tasks: unknown;
    tabs: unknown;
    approvals: readonly unknown[];
    newThreadTitle: string;
    onRespondApproval: (approval: unknown, decision: string) => Promise<boolean>;
  }) => { goals: readonly AgentTask[]; open: AgentTask | null | undefined };
} = require("../src/renderer/src/features/agent-threads/conversation-threads");
const { conversationThreads } = projection;

const AGENT_ID = "agent-1";

/** A goal of the agent, as the task snapshot carries it. */
function goal(overrides: Partial<AgentTask> & Pick<AgentTask, "id" | "title" | "state">): AgentTask {
  return {
    agentId: AGENT_ID,
    threadId: `thread-${overrides.id}`,
    origin: "user",
    outcome: null,
    hold: null,
    activeTurnId: null,
    progress: null,
    error: null,
    queuedMessages: [],
    createdAt: "2026-01-01T00:00:00.000Z",
    updatedAt: "2026-01-01T00:00:00.000Z",
    closedAt: null,
    ...overrides,
  };
}

/** The goal that runs now, and the goal that is done. */
const runningGoal = goal({
  id: "goal-running",
  title: "Ship the release",
  state: "running",
  progress: "42% of the tests pass",
});
const closedGoal = goal({
  id: "goal-closed",
  title: "Write the notes",
  state: "closed",
  outcome: "completed",
  closedAt: "2026-01-02T00:00:00.000Z",
  updatedAt: "2026-01-02T00:00:00.000Z",
});

/** The snapshot of the agent: the goals that run now first, then the goal that is done. */
const snapshot: AgentTasksSnapshot = { agentId: AGENT_ID, tasks: [runningGoal, closedGoal], limit: 2 };

/** The parts of the task state of an agent that the projection reads. */
interface TaskState {
  snapshot: () => AgentTasksSnapshot | undefined;
  conversation: () => undefined;
  editable: () => boolean;
  local: () => boolean;
  stopTask: () => void;
}

/** The parts of the thread tabs of an agent that the projection reads. */
interface TabState {
  openIds: () => string[];
  hasDraft: () => boolean;
  activeId: () => string;
  hidden: () => ReadonlySet<string>;
}

/** The task state of an agent whose list is the snapshot. */
const taskState = (list: AgentTasksSnapshot): TaskState => ({
  snapshot: () => list,
  conversation: () => undefined,
  editable: () => true,
  local: () => true,
  stopTask: () => undefined,
});

/** The tabs of an agent whose open thread is the goal that runs now. */
const tabState = (openId: string): TabState => ({
  openIds: () => [openId],
  hasDraft: () => false,
  activeId: () => openId,
  hidden: () => new Set<string>(),
});

/** The goals view in a page, as the thread mounts it. */
function mountGoals(goals: readonly AgentTask[]) {
  const dom = new JSDOM("<!doctype html><html><body></body></html>");
  const { document } = dom.window;
  const host = document.createElement("div");
  document.body.append(host);
  const view = mountAgentGoalsView(host, {
    goals,
    labels: {
      active: "Active goals",
      completed: "Completed goals",
      list: "Goals",
      completion: (value) => `Completion ${value}`,
    },
  });
  const element = (id: string): HTMLElement => {
    const found = document.getElementById(id);
    assert.ok(found, `The element ${id} is missing.`);
    return found;
  };
  return {
    view,
    /** The tab of a group of goals. */
    tab: (group: "active" | "completed"): HTMLElement => element(AGENT_GOALS_TAB_IDS[group]),
    /** The goals of the group on screen. */
    rows: (): HTMLElement[] => Array.from(element(AGENT_GOALS_PANEL_ID).querySelectorAll(".agent-goal")),
    /** A key press on a group. */
    keydown: (key: string): KeyboardEvent =>
      new dom.window.KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }),
  };
}

/** The completion a goal row shows. */
function completionOf(row: HTMLElement): string {
  return row.querySelector(".agent-goal-completion")?.textContent ?? "";
}

/** The checks of this file. Each one is a test when the test runner runs the file. */
const checks: { name: string; run: () => void }[] = [
  {
    name: "reads the whole snapshot, so the goals that are done stay in the list",
    run: () => {
      const threads = conversationThreads({
        agent: { id: AGENT_ID, updatedAt: "2026-01-03T00:00:00.000Z" },
        tasks: taskState(snapshot),
        tabs: tabState(runningGoal.id),
        approvals: [],
        newThreadTitle: "New thread",
        onRespondApproval: () => Promise.resolve(false),
      });
      assert.deepEqual(
        threads.goals.map((entry) => entry.id),
        [runningGoal.id, closedGoal.id],
      );
      assert.equal(threads.open?.id, runningGoal.id);
    },
  },
  {
    name: "shows the completion of the goal that runs now",
    run: () => {
      const goals = mountGoals([runningGoal, closedGoal]);
      const rows = goals.rows();
      assert.equal(rows.length, 1);
      const [row] = rows;
      assert.ok(row, "The goal that runs now is missing.");
      assert.equal(row.querySelector(".agent-goal-title")?.textContent, runningGoal.title);
      assert.equal(completionOf(row), "42%");
      assert.equal(row.querySelector(".agent-goal-completion")?.getAttribute("aria-label"), "Completion 42%");
    },
  },
  {
    name: "moves between the groups with the keyboard, and keeps the goals that are done",
    run: () => {
      const goals = mountGoals([runningGoal, closedGoal]);
      const active = goals.tab("active");
      const completed = goals.tab("completed");
      assert.equal(active.getAttribute("aria-selected"), "true");
      assert.equal(completed.getAttribute("aria-selected"), "false");

      active.dispatchEvent(goals.keydown("ArrowRight"));
      assert.equal(completed.getAttribute("aria-selected"), "true");
      assert.equal(active.getAttribute("aria-selected"), "false");
      assert.equal(active.tabIndex, -1);
      assert.equal(completed.tabIndex, 0);
      const rows = goals.rows();
      assert.equal(rows.length, 1);
      const [row] = rows;
      assert.ok(row, "The goal that is done is missing.");
      assert.equal(row.querySelector(".agent-goal-title")?.textContent, closedGoal.title);
      assert.equal(completionOf(row), "100%");

      completed.dispatchEvent(goals.keydown("ArrowLeft"));
      assert.equal(active.getAttribute("aria-selected"), "true");
      assert.equal(goals.rows()[0]?.querySelector(".agent-goal-title")?.textContent, runningGoal.title);
    },
  },
  {
    name: "reads the completion of a goal, and holds a goal that is done at 100",
    run: () => {
      assert.equal(agentGoalCompletion(runningGoal), 42);
      assert.equal(agentGoalCompletionText(runningGoal), "42%");
      assert.equal(agentGoalCompletion(closedGoal), 100);
      assert.equal(agentGoalCompletionText(closedGoal), "100%");
      // A percent outside the bounds is not shown, and a goal without one is at the start.
      assert.equal(
        agentGoalCompletion(goal({ id: "goal-wide", title: "A goal", state: "running", progress: "420%" })),
        100,
      );
      assert.equal(agentGoalCompletion(goal({ id: "goal-none", title: "A goal", state: "running" })), 0);
    },
  },
  {
    name: "shows the open task alone when the host has no snapshot",
    run: () => {
      assert.deepEqual(agentGoalsForView(undefined, runningGoal), [runningGoal]);
      assert.deepEqual(agentGoalsForView(undefined, null), []);
      // The whole snapshot wins over the open task, so the goals that are done stay in the list.
      assert.deepEqual(agentGoalsForView([runningGoal, closedGoal], runningGoal), [runningGoal, closedGoal]);
    },
  },
];

// `bun test` runs this file as a test file: the call to `describe` works there and throws outside it,
// where the script runs the checks itself.
const { describe, it } = await import("bun:test");
let underTestRunner = false;
try {
  describe("the goals of an agent", () => {
    for (const check of checks) it(check.name, check.run);
  });
  underTestRunner = true;
} catch {
  underTestRunner = false;
}

if (!underTestRunner) {
  for (const check of checks) {
    check.run();
    process.stdout.write(`- ${check.name}\n`);
  }
}
