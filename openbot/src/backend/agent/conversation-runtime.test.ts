// @vitest-environment node

import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { AgentSummary, ConversationSnapshot } from "@openbot/contracts/ipc";
import { isDynamicRecord, isString } from "@openbot/contracts/runtime-values";
import { afterEach, describe, expect, it } from "vitest";
import { AgentStore } from "../agent-store";
import { runCauseEffect } from "../effect-boundary";
import { OpenBotDatabase } from "../openbot-database";
import { ConversationRuntime } from "./conversation-runtime";

const roots: string[] = [];

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe("ConversationRuntime", () => {
  it("closes the turn row when the completion arrives after the conversation was already written", async () => {
    const { database, runtime, agent } = await createRuntime();
    const running = runningSnapshot(agent, "turn-1");
    runtime.emitConversation(running, "turn.started", { turnId: "turn-1" });
    // The mailbox sync writes the cleared active turn before the completion arrives, so the
    // conversation the completion carries is the one the database already holds.
    const cleared = completedSnapshot(running);
    runtime.emitConversation(cleared);

    const before = database.connection
      .prepare("SELECT status, completed_at FROM projection_turns WHERE turn_id = ?")
      .get("turn-1");
    expect(before).toMatchObject({ status: "running", completed_at: null });

    runtime.emitConversation(cleared, "turn.completed", { turnId: "turn-1", status: "completed" });

    const after = database.connection
      .prepare("SELECT status, completed_at FROM projection_turns WHERE turn_id = ?")
      .get("turn-1");
    expect(after).toMatchObject({ status: "completed" });
    expect(isDynamicRecord(after) && isString(after.completed_at)).toBe(true);
    expect(
      database.connection
        .prepare("SELECT payload_json FROM projection_thread_activities WHERE activity_type = 'turn.completed'")
        .get(),
    ).toEqual({ payload_json: '{"turnId":"turn-1","status":"completed"}' });
    database.close();
  });

  it("closes the turn row of a completion that arrives with a new conversation", async () => {
    const { database, runtime, agent } = await createRuntime();
    const running = runningSnapshot(agent, "turn-2");
    runtime.emitConversation(running, "turn.started", { turnId: "turn-2" });
    runtime.emitConversation(completedSnapshot(running), "turn.completed", {
      turnId: "turn-2",
      status: "completed",
    });

    expect(
      database.connection.prepare("SELECT status, completed_at FROM projection_turns WHERE turn_id = ?").get("turn-2"),
    ).toMatchObject({ status: "completed" });
    database.close();
  });
});

function runningSnapshot(agent: AgentSummary, turnId: string): ConversationSnapshot {
  return {
    agentId: agent.id,
    threadId: agent.threadId,
    activeTurnId: turnId,
    revision: 0,
    messages: [
      {
        id: `answer-${turnId}`,
        author: "assistant",
        text: "The work is done.",
        status: "streaming",
        createdAt: "2026-09-07T12:00:00.000Z",
        turnId,
      },
    ],
  };
}

function completedSnapshot(snapshot: ConversationSnapshot): ConversationSnapshot {
  return {
    ...snapshot,
    activeTurnId: null,
    messages: snapshot.messages.map((message) => ({ ...message, status: "completed" })),
  };
}

async function createRuntime(): Promise<{
  database: OpenBotDatabase;
  runtime: ConversationRuntime;
  agent: AgentSummary;
}> {
  const root = await mkdtemp(join(tmpdir(), "openbot-conversation-runtime-"));
  roots.push(root);
  const home = await mkdtemp(join(tmpdir(), "openbot-conversation-runtime-home-"));
  roots.push(home);
  const database = new OpenBotDatabase(root);
  await runCauseEffect(database.initialize());
  const agent = testAgent();
  database.replaceAgents("agents-import", [agent], "agents.imported");
  const store = new AgentStore(root, home, database);
  await runCauseEffect(store.initialize());
  return {
    database,
    runtime: new ConversationRuntime(
      store,
      () => {},
      () => store.list(),
    ),
    agent,
  };
}

function testAgent(): AgentSummary {
  return {
    id: "chief",
    provider: "codex",
    name: "Chief",
    title: "Coordinator",
    description: "",
    notifications: true,
    model: "gpt-5.6-luna",
    reasoningEffort: "medium",
    threadId: "openbot-thread-chief",
    workspacePath: "/tmp/openbot-chief",
    preview: "42",
    updatedAt: "2026-08-18T10:00:01.000Z",
    avatarSeed: "chief",
    avatarHue: null,
    avatarUrl: null,
  };
}
