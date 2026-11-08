import { afterEach, describe, expect, test } from "bun:test";
import { createNibiruResultSender } from "../src/backend/a2a/nibiru-results";

describe("Nibiru A2A terminal result delivery", () => {
  let server: ReturnType<typeof Bun.serve> | undefined;

  afterEach(() => {
    server?.stop(true);
    server = undefined;
  });

  test("omits output containing credentials or provider diagnostics for every terminal status", async () => {
    const bodies: Array<{ output?: string }> = [];
    const logs: string[] = [];
    const token = "secret-test-token";
    const providerError = "raw provider failure: private details";
    const sender = createNibiruResultSender({
      endpoint: "https://nibiru.example/results",
      token,
      log: (message) => logs.push(message),
      fetch: (async (_input, init) => {
        bodies.push(JSON.parse(String(init?.body)));
        return new Response(null, { status: 200 });
      }) as typeof fetch,
    });

    for (const terminal of ["completed", "failed", "interrupted"] as const) {
      for (const output of [token, providerError, `${token} raw provider failure`]) {
        await sender.sendTerminalResult({
          idempotencyKey: `${terminal}-${bodies.length}`,
          taskId: `task-${terminal}`,
          terminal,
          output,
          providerError,
        });
      }
    }

    expect(bodies).toHaveLength(9);
    expect(bodies.every((body) => !Object.hasOwn(body, "output"))).toBe(true);
    const serialized = JSON.stringify({ bodies, logs });
    expect(serialized).not.toContain(token);
    expect(serialized).not.toContain("raw provider failure");
  });

  test("preserves safe output and permits another callback after retry exhaustion", async () => {
    const bodies: unknown[] = [];
    const logs: string[] = [];
    const sender = createNibiruResultSender({
      endpoint: "https://nibiru.example/results",
      token: "secret-test-token",
      maxRetries: 1,
      retryDelayMs: 0,
      log: (message) => logs.push(message),
      fetch: (async (_input, init) => {
        bodies.push(JSON.parse(String(init?.body)));
        if (bodies.length <= 2) throw new Error("secret-test-token raw provider failure");
        return new Response(null, { status: 200 });
      }) as typeof fetch,
    });
    const result = {
      idempotencyKey: "task-retry",
      taskId: "task-retry",
      terminal: "completed" as const,
      output: "safe completed result",
      providerError: "raw provider failure",
    };

    await expect(sender.sendTerminalResult(result)).rejects.toThrow("Nibiru result delivery failed after retries.");
    await Promise.all([sender.sendTerminalResult(result), sender.sendTerminalResult(result)]);
    await sender.sendTerminalResult(result);

    expect(bodies).toHaveLength(3);
    expect(bodies.every((body) => (body as { output: string }).output === result.output)).toBe(true);
    expect(logs.join("\n")).not.toContain("secret-test-token");
    expect(logs.join("\n")).not.toContain("raw provider failure");
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
