import { describe, expect, it } from "vitest";
import { dispatchResultMailboxText } from "./dispatch-delivery";
import { type DispatchRecord, redactDispatchRecord } from "./dispatch-service";

const SECRET = "nonpattern-secret-value";
const redact = (text: string) => text.replaceAll(SECRET, "[MASKED]");

function resultRecord(): DispatchRecord {
  return {
    runId: "run-1",
    host: "local",
    agentId: "agent-1",
    threadId: "thread-1",
    title: "Task",
    binary: "openswarm",
    idempotencyKey: "idempotency-1",
    status: "done",
    branch: null,
    commit: null,
    startedAt: "2026-10-26T00:00:00.000Z",
    summary: `Result contains ${SECRET}`,
    verify: {
      cmd: `check ${SECRET}`,
      tail: `output ${SECRET}`,
      failing: [`failure ${SECRET}`],
    },
    delivered: false,
  };
}

describe("dispatch result redaction", () => {
  it("redacts summary and verify fields before the dispatch record is saved", () => {
    const safe = redactDispatchRecord(resultRecord(), redact);

    expect(JSON.stringify(safe)).not.toContain(SECRET);
    expect(safe.summary).toBe("Result contains [MASKED]");
    expect(safe.verify).toEqual({
      cmd: "check [MASKED]",
      tail: "output [MASKED]",
      failing: ["failure [MASKED]"],
    });
  });

  it("redacts the mailbox JSON text before it is enqueued", () => {
    const text = dispatchResultMailboxText(resultRecord(), redact);

    expect(text).not.toContain(SECRET);
    expect(JSON.parse(text)).toMatchObject({
      summary: "Result contains [MASKED]",
      verify: {
        cmd: "check [MASKED]",
        tail: "output [MASKED]",
        failing: ["failure [MASKED]"],
      },
    });
  });
});
