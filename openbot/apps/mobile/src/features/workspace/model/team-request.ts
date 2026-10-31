import { runTeamEffect } from "@openbot/team-client";
import { Effect } from "effect";

/** Runs a Team API request. A failure rejects with the error of the host, not its Effect wrapper. */
export function runTeamRequest<A>(operation: Effect.Effect<A, { readonly cause: unknown }>): Promise<A> {
  return runTeamEffect(operation.pipe(Effect.mapError((error) => error.cause)));
}
