import { expect, test } from "bun:test";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import type { IncomingMessage } from "node:http";
import { join, resolve } from "node:path";
import { Readable } from "node:stream";
import type { AgentAccessLevel } from "@volto/contracts/ipc";
import { runCauseEffect } from "../src/backend/effect-boundary";
import { MailboxStore } from "../src/backend/mailbox-store";
import { VoltoDatabase } from "../src/backend/volto-database";
import type { RouteOutcome } from "../src/main/team-api/request-context";
import { routeNibiruInbound } from "../src/main/team-api/route-nibiru-inbound";

test("authenticated inbound route delivers once and checks access", async () => {
  const output = resolve(import.meta.dirname, "../.volto-build/nibiru-a2a-inbound-entry-e2e");
  await mkdir(output, { recursive: true });
  const root = await mkdtemp(join(output, "profile-"));
  const database = new VoltoDatabase(root);
  const mailbox = new MailboxStore(root, join(root, "shared"), database);
  try {
    await runCauseEffect(mailbox.initialize());
    const envelope = {
      version: 1,
      taskId: "task-1",
      sender: "remote",
      recipient: "agent-a",
      body: "Do the task",
      idempotencyKey: "external-1",
    };
    const send = async (body: unknown, level: AgentAccessLevel = "chat", hidden = new Set<string>()) => {
      const request = Object.assign(Readable.from([Buffer.from(JSON.stringify(body))]), {
        headers: {},
        method: "POST",
        url: "/v1/a2a/nibiru/tasks",
        // biome-ignore lint/nursery/noUnsafeTypeAssertion: the route only reads the body stream; a Readable stands in for IncomingMessage.
      }) as IncomingMessage;
      let result: object | null = null;
      const context = {
        request,
        method: "POST",
        url: new URL("http://127.0.0.1/v1/a2a/nibiru/tasks"),
        agentLevel: () => level,
        json: (_status: number, value: object | null) => {
          result = value;
          const outcome: RouteOutcome = "handled";
          return outcome;
        },
      };
      await routeNibiruInbound(
        context,
        {
          mailbox,
          agents: {
            listAgents: () =>
              // biome-ignore lint/nursery/noUnsafeTypeAssertion: the route reads only the agent id.
              [{ id: "agent-a" }] as ReturnType<
                import("../src/main/team-api/dependencies").TeamApiAgents["listAgents"]
              >,
          },
        },
        hidden,
      );
      return result;
    };
    const first = await send(envelope);
    expect(await send(envelope)).toEqual(first);
    const deliveries = mailbox.listQueue("agent-a").deliveries;
    expect(deliveries).toHaveLength(1);
    const deliveryId = deliveries[0]?.id ?? "";
    expect(mailbox.getDelivery(deliveryId)?.delivery.text).toBe("Do the task");
    expect(mailbox.getDelivery(deliveryId)?.a2a).toEqual({ taskId: "task-1" });
    await expect(send({ ...envelope, recipient: "missing" })).rejects.toMatchObject({ status: 404 });
    await expect(send(envelope, "view")).rejects.toMatchObject({ status: 403 });
    await expect(send(envelope, "chat", new Set(["agent-a"]))).rejects.toMatchObject({ status: 404 });
    await expect(send({ ...envelope, version: 2 })).rejects.toMatchObject({ status: 400 });
    expect(mailbox.listQueue("agent-a").deliveries).toHaveLength(1);
  } finally {
    database.close();
    await rm(root, { recursive: true, force: true });
  }
});
