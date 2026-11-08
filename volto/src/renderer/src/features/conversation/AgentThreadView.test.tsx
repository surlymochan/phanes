import { fireEvent, render, screen } from "@solidjs/testing-library";
import type { AgentTask } from "@volto/contracts/ipc";
import type { AgentProfile } from "@volto/ui/data";
import { AgentThreadView } from "@volto/ui/features/conversation/threads/AgentThreadView";
import { describe, expect, it } from "vitest";

// The goals of an agent in the thread of one of its side threads: the goals that run now, the goals
// that are done, and the completion of each. The thread reads the goals from the snapshot of the
// agent, so the goals that are done stay in the list next to the goals that run now.

const AGENT: AgentProfile = {
  id: "agent-1",
  name: "Mia",
  title: "Researcher",
  description: "Reads the things.",
  notifications: false,
  provider: "codex",
  model: "gpt-6-luna",
  reasoningEffort: "medium",
  threadId: null,
  avatarSeed: "mia",
  avatarHue: null,
  avatarUrl: null,
  time: "",
  preview: "",
};

function goal(overrides: Partial<AgentTask> & Pick<AgentTask, "id" | "title" | "state">): AgentTask {
  return {
    agentId: AGENT.id,
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

const runningGoal = goal({ id: "goal-running", title: "Ship the release", state: "running", progress: "42% done" });
const closedGoal = goal({
  id: "goal-closed",
  title: "Write the notes",
  state: "closed",
  outcome: "completed",
  closedAt: "2026-01-02T00:00:00.000Z",
  updatedAt: "2026-01-02T00:00:00.000Z",
});

function renderThread(tasks: readonly AgentTask[]) {
  return render(() => (
    <AgentThreadView
      task={tasks[0] ?? null}
      tasks={tasks}
      limit={2}
      agent={AGENT}
      agents={[AGENT]}
      messages={[]}
      onSend={() => Promise.resolve(true)}
      onStop={() => undefined}
      onOpenLink={() => undefined}
      onPreview={() => undefined}
      onAttachmentAction={() => undefined}
      onRespondApproval={() => Promise.resolve(false)}
    />
  ));
}

describe("the goals of an agent in a side thread", () => {
  it("shows the goal that runs now with its completion", () => {
    renderThread([runningGoal, closedGoal]);
    const panel = document.getElementById("agent-goals-panel");
    expect(panel).not.toBeNull();
    expect(panel?.textContent).toContain(runningGoal.title);
    expect(panel?.textContent).toContain("42%");
    // The goal that is done is in its own group, so the group on screen does not show it.
    expect(panel?.textContent).not.toContain(closedGoal.title);
  });

  it("shows a progress that is not a whole percent with the percent it names", () => {
    const decimalGoal = goal({
      id: "goal-decimal",
      title: "Refine the notes",
      state: "running",
      progress: "42.5% done",
    });
    renderThread([decimalGoal]);
    const panel = document.getElementById("agent-goals-panel");
    expect(panel?.textContent).toContain("42.5%");
    const completion = panel?.querySelector(".agent-goal-completion");
    expect(completion?.getAttribute("aria-label")).toBe("Completion 42.5%");
  });

  it("moves between the groups with the keyboard, and keeps the goals that are done", async () => {
    renderThread([runningGoal, closedGoal]);
    const active = document.getElementById("agent-goals-active");
    const completed = document.getElementById("agent-goals-completed");
    expect(active).not.toBeNull();
    expect(active?.getAttribute("aria-selected")).toBe("true");
    expect(completed?.getAttribute("aria-selected")).toBe("false");

    if (!active) return;
    await fireEvent.keyDown(active, { key: "ArrowRight" });
    expect(completed?.getAttribute("aria-selected")).toBe("true");
    const panel = document.getElementById("agent-goals-panel");
    expect(panel?.textContent).toContain(closedGoal.title);
    expect(panel?.textContent).toContain("100%");
    expect(panel?.textContent).not.toContain(runningGoal.title);
  });

  it("shows the open task alone when the host has no snapshot", () => {
    render(() => (
      <AgentThreadView
        task={runningGoal}
        limit={2}
        agent={AGENT}
        agents={[AGENT]}
        messages={[]}
        onSend={() => Promise.resolve(true)}
        onStop={() => undefined}
        onOpenLink={() => undefined}
        onPreview={() => undefined}
        onAttachmentAction={() => undefined}
        onRespondApproval={() => Promise.resolve(false)}
      />
    ));
    const panel = document.getElementById("agent-goals-panel");
    expect(panel?.textContent).toContain(runningGoal.title);
    expect(panel?.textContent).not.toContain(closedGoal.title);
    expect(screen.getByRole("tab", { name: /Active goals/ })).toBeTruthy();
  });
});
