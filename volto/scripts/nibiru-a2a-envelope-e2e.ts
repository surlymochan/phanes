import { describe, expect, test } from "bun:test";
import { decodeNibiruTaskEnvelope } from "../src/backend/a2a/nibiru-envelope";

const valid = {
  version: 1 as const,
  taskId: "task-1",
  sender: "agent-a",
  recipient: "agent-b",
  body: "Please review this task.",
  idempotencyKey: "request-1",
};

describe("Nibiru task envelope", () => {
  test("decodes a valid envelope into a separate object", () => {
    const decoded = decodeNibiruTaskEnvelope(valid);
    expect(decoded).toEqual(valid);
    expect(decoded).not.toBe(valid);
  });

  test("accepts field limits", () => {
    expect(
      decodeNibiruTaskEnvelope({
        version: 1,
        taskId: "t".repeat(256),
        sender: "s".repeat(256),
        recipient: "r".repeat(256),
        body: "b".repeat(65536),
        idempotencyKey: "i".repeat(256),
      }).body.length,
    ).toBe(65536);
  });

  test("rejects missing, empty, non-string and oversized fields", () => {
    for (const field of ["taskId", "sender", "recipient", "body", "idempotencyKey"] as const) {
      const { [field]: _, ...missing } = valid;
      expect(() => decodeNibiruTaskEnvelope(missing)).toThrow();
      for (const value of ["", "   ", null, 42, "x".repeat(field === "body" ? 65537 : 257)]) {
        expect(() => decodeNibiruTaskEnvelope({ ...valid, [field]: value })).toThrow();
      }
    }
  });

  test("rejects unknown versions and unexpected payloads", () => {
    for (const input of [
      null,
      [],
      "text",
      {},
      { ...valid, version: 2 },
      { ...valid, version: "1" },
      { ...valid, extra: true },
    ]) {
      expect(() => decodeNibiruTaskEnvelope(input)).toThrow();
    }
  });
});
