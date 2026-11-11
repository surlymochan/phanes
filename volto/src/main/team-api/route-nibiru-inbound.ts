import { sourceText } from "@volto/i18n/source";
import { Effect } from "effect";
import { decodeNibiruTaskEnvelope } from "../../backend/a2a/nibiru-envelope";
import { createNibiruInbound } from "../../backend/a2a/nibiru-inbound";
import { runCauseEffect } from "../../backend/effect-boundary";
import { readBodyWithin } from "../http-body";
import type { TeamApiAgents, TeamApiMailbox } from "./dependencies";
import { HttpError } from "./http-error";
import type { RouteOutcome, TeamApiRequestContext } from "./request-context";
import { JSON_LIMIT, requireAgentLevel } from "./request-helpers";

export async function routeNibiruInbound(
  context: Pick<TeamApiRequestContext, "request" | "method" | "url" | "agentLevel" | "json" | "member">,
  { agents, mailbox }: { agents: Pick<TeamApiAgents, "listAgents">; mailbox: TeamApiMailbox },
  hidden: ReadonlySet<string>,
): Promise<RouteOutcome> {
  if (context.method !== "POST" || context.url.pathname !== "/v1/a2a/nibiru/tasks") return "unmatched";
  const body = await readBodyWithin(context.request, JSON_LIMIT);
  if (body === null) throw new HttpError(413, "Request too large.");
  let envelope: ReturnType<typeof decodeNibiruTaskEnvelope>;
  try {
    envelope = decodeNibiruTaskEnvelope(JSON.parse(body.toString("utf8")));
  } catch {
    throw new HttpError(400, "Invalid Nibiru task envelope.");
  }
  if (hidden.has(envelope.recipient) || !agents.listAgents().some((agent) => agent.id === envelope.recipient)) {
    throw new HttpError(404, sourceText("error.team.agentNotFound"));
  }
  requireAgentLevel(context, envelope.recipient, "chat");
  const inbound = createNibiruInbound({
    mailbox,
    hasAgent: (id) => agents.listAgents().some((agent) => agent.id === id),
  });
  const receipt = await runCauseEffect(
    Effect.mapError(
      inbound.accept({
        principalId: context.member.id,
        idempotencyKey: envelope.idempotencyKey,
        taskId: envelope.taskId,
        recipientAgentId: envelope.recipient,
        text: envelope.body,
      }),
      (cause) => ({ cause }),
    ),
  );
  return context.json(200, receipt);
}
