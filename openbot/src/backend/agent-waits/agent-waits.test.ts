// @vitest-environment node
import type { AgentWait } from "@openbot/contracts/ipc";
import { describe, expect, it } from "vitest";
import { STALE_WAIT_MS, wokenWaitsPromptText } from "./agent-waits";

// Failure mode: a routine woke the same wait every 30 minutes for six hours, and each run only
// waited again, so the user never learned that the request was blocked.
function wait(createdAt: string): AgentWait {
  return {
    id: "wait-1",
    agentId: "agent-supervisor",
    note: "Hold PR1399 until PR1476 is approved and published.",
    requestMessageId: "message-1",
    requestText: "Decide who owns PR1399.",
    routineId: "routine-1",
    createdAt,
    wokenAt: null,
    wakeDeliveryId: null,
  };
}

const opened = "2026-10-20T20:41:07.814Z";

describe("woken waits", () => {
  it("asks for no report while a wait is new", () => {
    const now = new Date(Date.parse(opened) + STALE_WAIT_MS - 1);
    expect(wokenWaitsPromptText([wait(opened)], now)).not.toContain("This wait is open for");
  });

  it("asks for a report to the user when a wait is stale", () => {
    const now = new Date("2026-10-21T02:45:00.009Z");
    const text = wokenWaitsPromptText([wait(opened)], now) ?? "";
    expect(text).toContain("This wait is open for 6 hours.");
    expect(text).toContain("tell the user now what blocks it, who must act, and the one action that unblocks it");
    expect(text).toContain("do not call openbot.routine_no_update in this run");
  });

  it("does not treat an unreadable open time as stale", () => {
    expect(wokenWaitsPromptText([wait("not a time")], new Date())).not.toContain("This wait is open for");
  });
});
