import { expect, test } from "bun:test";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { Effect } from "effect";
import { createNibiruInbound } from "../src/backend/a2a/nibiru-inbound";
import { runCauseEffect } from "../src/backend/effect-boundary";
import { MailboxStore } from "../src/backend/mailbox-store";
import { VoltoDatabase } from "../src/backend/volto-database";

test("local transport delivers once across retries and rejects unknown agents without writes", async () => {
  const output = resolve(import.meta.dirname, "../.volto-build/nibiru-a2a-inbound-e2e");
  await mkdir(output, { recursive: true });
  const root = await mkdtemp(join(output, "profile-"));
  let database = new VoltoDatabase(root);
  let mailbox = new MailboxStore(root, join(root, "shared"), database);
  try {
    await runCauseEffect(mailbox.initialize());
    const connect = () => createNibiruInbound({ mailbox, hasAgent: (id) => id === "agent-a" });
    let transport = connect();
    const request = {
      principalId: "member-1",
      idempotencyKey: "external-1",
      taskId: "task-1",
      recipientAgentId: "agent-a",
      text: "Do the task",
    };
    const send = (input: typeof request) => Effect.runPromise(transport.accept(input));
    const [first, duplicate] = await Promise.all([send(request), send(request)]);
    expect(duplicate).toEqual(first);
    const deliveryId = first.deliveries[0]?.id;
    if (!deliveryId) throw new Error("The request has no delivery.");
    expect(mailbox.listQueue("agent-a").deliveries).toHaveLength(1);
    expect(mailbox.getDelivery(deliveryId)?.delivery.text).toBe("Do the task");
    expect(mailbox.getDelivery(deliveryId)?.a2a).toEqual({ taskId: "task-1" });
    database.close();
    database = new VoltoDatabase(root);
    mailbox = new MailboxStore(root, join(root, "shared"), database);
    await runCauseEffect(mailbox.initialize());
    transport = connect();
    expect(await send(request)).toEqual(first);
    expect(mailbox.listQueue("agent-a").deliveries).toHaveLength(1);
    expect(mailbox.getDelivery(deliveryId)?.a2a).toEqual({ taskId: "task-1" });
    const before = database.readMailboxState();
    const writesBefore = database.connection.prepare("SELECT total_changes() AS count").get();
    await expect(send({ ...request, idempotencyKey: "unknown", recipientAgentId: "missing" })).rejects.toThrow(
      "Unknown A2A recipient.",
    );
    expect(database.readMailboxState()).toEqual(before);
    expect(database.connection.prepare("SELECT total_changes() AS count").get()).toEqual(writesBefore);
    await writeFile(
      join(output, "report.json"),
      JSON.stringify({ duplicate: "passed", restart: "passed", unknown: "passed" }),
    );
  } finally {
    database.close();
    await rm(root, { recursive: true, force: true });
  }
});
