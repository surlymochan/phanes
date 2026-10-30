import { FOCUS_USE_CAPABILITY, FOCUS_USE_ROUTES } from "@openbot/contracts/team-protocol/focus-use-v1";
import type { RouteOutcome, TeamApiRequestContext } from "./request-context";
import { readJson } from "./request-helpers";

/**
 * The app of the calling member has focus. Frozen by `focus-use-v1`; only a hosted server serves it,
 * because only there does use keep the server running.
 */
export async function routeFocusUse(
  context: TeamApiRequestContext,
  recordFocus: ((sessionId: string) => void) | undefined,
): Promise<RouteOutcome> {
  const { method, url, capabilities, request, json, sessionId } = context;
  if (method !== "POST" || url.pathname !== FOCUS_USE_ROUTES.ping) return "unmatched";
  if (!recordFocus || !capabilities.has(FOCUS_USE_CAPABILITY)) return "unmatched";
  // `readJson` has already run the body through the focus-use wire codec.
  await readJson(request);
  recordFocus(sessionId);
  return json(200, {});
}
