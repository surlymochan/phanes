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
const threadTabsPath = join(
  projectRoot,
  "packages/ui/src/features/conversation/threads/ThreadTabs.tsx",
);

const [threadView, threadTabs] = await Promise.all([
  readFile(threadViewPath, "utf8"),
  readFile(threadTabsPath, "utf8"),
]);

describe("agent goals view", () => {

  it("lets keyboard users switch between active and completed goal groups", () => {
    const source = `${threadView}\n${threadTabs}`;
    assert.match(source, /进行中|Active goals/i);
    assert.match(source, /已完成|Completed goals/i);
    assert.match(source, /role=["']tablist["']|role=["']radiogroup["']/);
    assert.match(source, /role=["']tab["']|role=["']radio["']/);
  });

  it("shows a completion percentage for active goals", () => {
    assert.match(threadView, /goal[^\n]*completion|completion[^\n]*goal/i);
    assert.match(threadView, /%|percent/i);
  });

  it("keeps completed goals visible with 100 percent completion", () => {
    assert.match(threadView, /completedGoals|completed goals|已完成/i);
    assert.match(threadView, /100\s*%|100%/);
  });
});
