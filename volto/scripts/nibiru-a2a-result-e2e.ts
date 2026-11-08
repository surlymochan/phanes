import { afterEach, describe, expect, test } from "bun:test";
import { createNibiruResultSender } from "../src/backend/a2a/nibiru-results";

describe("Nibiru A2A terminal result delivery", () => {
  let server: ReturnType<typeof Bun.serve> | undefined;

  afterEach(() => {
    server?.stop(true);
    server = undefined;
  });

  test("sends each terminal result, retries transient failures, and deduplicates confirmed callbacks", async () => {
    const requests: Array<{ body: unknown; authorization: string | null }> = [];
    const logs: string[] = [];
    let attempts = 0;
    server = Bun.serve({
      port: 0,
      async fetch(request) {
        requests.push({ body: await request.json(), authorization: request.headers.get("authorization") });
        attempts += 1;
        return Response.json({ accepted: true }, { status: attempts === 1 ? 503 : 200 });
      },
    });

    const sender = createNibiruResultSender({
      endpoint: `http://127.0.0.1:${server.port}/results`,
      token: "secret-test-token",
      retryDelayMs: 0,
      log: (message) => logs.push(message),
    });
    const terminalResults = ["completed", "failed", "interrupted"] as const;

    for (const terminal of terminalResults) {
      await sender.sendTerminalResult({
        idempotencyKey: `task-${terminal}`,
        taskId: `task-${terminal}`,
        terminal,
        output: `safe ${terminal} result`,
        providerError: "raw provider failure: private details",
      });
    }

    // A repeated callback after confirmation must use the same idempotency key and send nothing.
    await sender.sendTerminalResult({
      idempotencyKey: "task-completed",
      taskId: "task-completed",
      terminal: "completed",
      output: "safe completed result",
    });

    expect(attempts).toBe(4); // first request failed; retry succeeded; two other terminal results
    expect(requests).toHaveLength(4);
    expect(requests.map(({ body }) => (body as { terminal: string }).terminal)).toEqual([
      "completed",
      "completed",
      "failed",
      "interrupted",
    ]);
    expect(requests.every(({ authorization }) => authorization === "Bearer secret-test-token")).toBe(true);
    const serialized = JSON.stringify(requests.map(({ body }) => body));
    expect(serialized).not.toContain("secret-test-token");
    expect(serialized).not.toContain("raw provider failure");
    expect(logs.join("\n")).not.toContain("secret-test-token");
    expect(logs.join("\n")).not.toContain("raw provider failure");
  });
});
