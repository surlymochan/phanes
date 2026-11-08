import type { AgentTask } from "@volto/contracts/ipc";

/**
 * The goals of an agent: the ones it works on now and the ones that are done. The view is plain
 * DOM, so the thread mounts it in a host element and a check can drive its keyboard without a
 * browser and without a second view of the same goals.
 */
export type AgentGoalGroup = "active" | "completed";

/** The completion of a goal that is done. */
export const COMPLETED_GOAL_COMPLETION = "100%";

/** The id of the element that holds the goals of the group on screen. */
export const AGENT_GOALS_PANEL_ID = "agent-goals-panel";

/** The id of the tab of each group. */
export const AGENT_GOALS_TAB_IDS: Record<AgentGoalGroup, string> = {
  active: "agent-goals-active",
  completed: "agent-goals-completed",
};

/**
 * The goals the view shows: the whole snapshot of the agent when the host has one, so the goals
 * that are done stay in the list, and the open task alone when it does not.
 */
export function agentGoalsForView(
  tasks: readonly AgentTask[] | undefined,
  task: AgentTask | null | undefined,
): readonly AgentTask[] {
  if (tasks) return tasks;
  return task ? [task] : [];
}

/** The goals in flight and the goals that are done. */
export function agentGoalGroups(goals: readonly AgentTask[]): {
  active: AgentTask[];
  completed: AgentTask[];
} {
  const active: AgentTask[] = [];
  const completed: AgentTask[] = [];
  for (const goal of goals) {
    if (goal.state === "closed") completed.push(goal);
    else active.push(goal);
  }
  return { active, completed };
}

/**
 * How far a goal is, in percent. A goal that is done is at 100. The snapshot has no completion of
 * its own yet, so the percent of the progress text is what the goal reached.
 */
export function agentGoalCompletion(goal: AgentTask): number {
  if (goal.state === "closed") return 100;
  const percent = goal.progress?.match(/(\d{1,3})\s*%/)?.[1];
  if (percent === undefined) return 0;
  return Math.max(0, Math.min(100, Number(percent)));
}

/** The completion of a goal, as the user reads it. */
export function agentGoalCompletionText(goal: AgentTask): string {
  if (goal.state === "closed") return COMPLETED_GOAL_COMPLETION;
  return `${agentGoalCompletion(goal)}%`;
}

/** The group a key moves to. The arrow keys move between the groups, Home and End pick one. */
export function nextAgentGoalGroup(group: AgentGoalGroup, key: string): AgentGoalGroup | undefined {
  if (key === "ArrowLeft" || key === "ArrowRight") return group === "active" ? "completed" : "active";
  if (key === "Home") return "active";
  if (key === "End") return "completed";
  return undefined;
}

export interface AgentGoalsViewLabels {
  /** The name of the group of the goals in flight. */
  active: string;
  /** The name of the group of the goals that are done. */
  completed: string;
  /** The name of the list of goals. */
  list: string;
  /** The text a screen reader reads for the completion of a goal. */
  completion: (value: string) => string;
}

export interface AgentGoalsViewOptions {
  /** The goals of the agent: the ones in flight and the ones that are done. */
  goals: readonly AgentTask[];
  labels: AgentGoalsViewLabels;
  /** Called when the keyboard or the mouse moves to another group. */
  onGroupChange?: ((group: AgentGoalGroup) => void) | undefined;
}

export interface AgentGoalsView {
  /** Shows the goals of the group on screen again, after the snapshot or the language changed. */
  update: (view: { goals: readonly AgentTask[]; labels: AgentGoalsViewLabels }) => void;
  /** Takes the view out of its host element. */
  destroy: () => void;
}

/**
 * The goals view of an agent: the two groups of goals, the goals of the group on screen with their
 * completion, and the keyboard that moves between the groups.
 */
export function mountAgentGoalsView(host: HTMLElement, options: AgentGoalsViewOptions): AgentGoalsView {
  const ownerDocument = host.ownerDocument;
  let goals = options.goals;
  let labels = options.labels;
  let group: AgentGoalGroup = "active";

  const list = ownerDocument.createElement("div");
  list.className = "agent-goals-groups";
  list.setAttribute("role", "tablist");
  list.setAttribute("aria-label", labels.list);

  const tabs: Record<AgentGoalGroup, HTMLButtonElement> = {
    active: createTab(ownerDocument, "active"),
    completed: createTab(ownerDocument, "completed"),
  };

  const panel = ownerDocument.createElement("div");
  panel.className = "agent-goals-panel";
  panel.id = AGENT_GOALS_PANEL_ID;
  panel.setAttribute("role", "tabpanel");

  function createTab(ownerDocument: Document, name: AgentGoalGroup): HTMLButtonElement {
    const tab = ownerDocument.createElement("button");
    tab.className = "agent-goals-tab";
    tab.id = AGENT_GOALS_TAB_IDS[name];
    tab.type = "button";
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-controls", AGENT_GOALS_PANEL_ID);
    tab.addEventListener("click", () => select(name));
    return tab;
  }

  /** The completion of a goal, as the user reads it. */
  function createRow(goal: AgentTask): HTMLElement {
    const row = ownerDocument.createElement("article");
    row.className = "agent-goal";
    const title = ownerDocument.createElement("span");
    title.className = "agent-goal-title";
    title.textContent = goal.title;
    const completion = ownerDocument.createElement("span");
    completion.className = "agent-goal-completion";
    const text = agentGoalCompletionText(goal);
    completion.setAttribute("aria-label", labels.completion(text));
    completion.textContent = text;
    row.append(title, completion);
    return row;
  }

  function render(): void {
    const groups = agentGoalGroups(goals);
    for (const name of ["active", "completed"] as const) {
      const tab = tabs[name];
      const selected = name === group;
      tab.setAttribute("aria-selected", selected ? "true" : "false");
      tab.tabIndex = selected ? 0 : -1;
      tab.textContent = `${labels[name]} (${groups[name].length})`;
    }
    panel.setAttribute("aria-labelledby", AGENT_GOALS_TAB_IDS[group]);
    panel.replaceChildren(...groups[group].map(createRow));
  }

  function select(name: AgentGoalGroup): void {
    if (name === group) return;
    group = name;
    render();
    tabs[name].focus();
    options.onGroupChange?.(name);
  }

  function onKeyDown(event: KeyboardEvent): void {
    const next = nextAgentGoalGroup(group, event.key);
    if (next === undefined) return;
    event.preventDefault();
    select(next);
  }

  list.addEventListener("keydown", onKeyDown);
  list.append(tabs.active, tabs.completed);
  host.replaceChildren(list, panel);
  render();

  return {
    update(view: { goals: readonly AgentTask[]; labels: AgentGoalsViewLabels }): void {
      goals = view.goals;
      labels = view.labels;
      render();
    },
    destroy(): void {
      list.removeEventListener("keydown", onKeyDown);
      host.replaceChildren();
    },
  };
}
