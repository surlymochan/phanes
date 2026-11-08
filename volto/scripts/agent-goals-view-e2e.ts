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
    assert.match(threadView, /goal[^\n]*completion|completion[^\n]*goal/i);
    assert.match(threadView, /%|percent/i);
  });

  it("keeps completed goals visible with 100 percent completion", () => {
    assert.match(threadView, /completedGoals\(\)/);
    assert.match(threadView, /goal\.state === "closed" \? COMPLETED_GOAL_COMPLETION/);
    assert.match(threadView, /100\s*%|100%/);
  });
});
