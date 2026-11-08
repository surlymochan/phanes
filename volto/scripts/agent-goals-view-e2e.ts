import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "bun:test";

const projectRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const threadViewPath = join(
  projectRoot,
  "packages/ui/src/features/conversation/threads/AgentThreadView.tsx",
);
const threadView = await readFile(threadViewPath, "utf8");

describe("agent goals view", () => {

  it("lets keyboard users switch between active and completed goal groups", () => {
    assert.match(threadView, /进行中\s*\/\s*Active goals/i);
    assert.match(threadView, /已完成\s*\/\s*Completed goals/i);
    assert.match(threadView, /role=["']tablist["']/);
    assert.match(threadView, /role=["']tab["']/);
    assert.match(threadView, /onKeyDown=\{switchGoalGroup\}/);
    assert.match(threadView, /event\.key !== "ArrowLeft" && event\.key !== "ArrowRight"/);
    assert.match(threadView, /aria-controls="agent-goals-panel"/);
    assert.match(threadView, /role=["']tabpanel["']/);
  });

  it("shows a completion percentage for active goals", () => {
    assert.match(threadView, /const goals = \(\) => props\.tasks \?\?/);
    assert.match(threadView, /const activeGoals = \(\) => goals\(\)\.filter\(\(goal\) => goal\.state !== "closed"\)/);
    assert.match(threadView, /const goalCompletion = \(goal: AgentTask\)/);
    assert.match(threadView, /Math\.max\(0, Math\.min\(100, Number\(value\)\)\)/);
    assert.match(threadView, /aria-label=\{`Completion \$\{goal\.state === "closed" \? COMPLETED_GOAL_COMPLETION : `\$\{goalCompletion\(goal\)\}%`\}`\}/);
  });

  it("keeps completed goals visible with 100 percent completion", () => {
    assert.match(threadView, /const completedGoals = \(\) => goals\(\)\.filter\(\(goal\) => goal\.state === "closed"\)/);
    assert.match(threadView, /goalGroup\(\) === "active" \? activeGoals\(\) : completedGoals\(\)/);
    assert.match(threadView, /goal\.state === "closed" \? COMPLETED_GOAL_COMPLETION/);
    assert.match(threadView, /100\s*%|100%/);
  });
});
