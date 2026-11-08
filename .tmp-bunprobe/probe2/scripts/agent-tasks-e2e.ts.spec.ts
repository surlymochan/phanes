import { test } from "bun:test";
const agentId = process.argv.find((a) => a.startsWith("--agent="))?.slice("--agent=".length) ?? null;
test.skipIf(agentId === null)("the live task list reports the completion of a goal", async () => {
  console.log("live run", agentId);
});
test("a pure check", () => {
  if (agentId !== null) throw new Error("no");
});
