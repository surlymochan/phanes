import {
  MCP_SIGN_IN_APP_CAPABILITY,
  MCP_SIGN_IN_APP_ROUTES,
} from "@openbot/contracts/team-protocol/mcp-sign-in-app-v1";
import { sourceText } from "@openbot/i18n/source";
import { runCauseEffect } from "../../backend/effect-boundary";
import { parseMcpSignInApp, parseSaveMcpSignInApp } from "../ipc/mcp-inputs";
import type { TeamApiMcpSignIns } from "./dependencies";
import { HttpError } from "./http-error";
import type { RouteOutcome, TeamApiRequestContext } from "./request-context";
import { readJson, requireAdmin } from "./request-helpers";

const ROUTES: ReadonlySet<string> = new Set(Object.values(MCP_SIGN_IN_APP_ROUTES));

/**
 * The sign-in apps of this host's http MCP rows, set from a joined server. Frozen by
 * `mcp-sign-in-app-v1`.
 *
 * The client secret arrives in `save` and stays in this host's encrypted sign-in store; no answer
 * carries it. `requireAdmin` runs on every route, as on the MCP list itself.
 */
export async function routeMcpSignInApp(
  context: TeamApiRequestContext,
  signIns: TeamApiMcpSignIns | undefined,
): Promise<RouteOutcome> {
  const { method, url, capabilities, member, request, json } = context;
  if (method !== "POST" || !ROUTES.has(url.pathname)) return "unmatched";
  if (!signIns || !capabilities.has(MCP_SIGN_IN_APP_CAPABILITY))
    throw new HttpError(400, sourceText("error.team.mcpUnsupported"));
  requireAdmin(member);
  // `readJson` has already run the body through the `mcp-sign-in-app-v1` codec.
  const body = await readJson(request);
  switch (url.pathname) {
    case MCP_SIGN_IN_APP_ROUTES.save:
      return json(200, await runCauseEffect(signIns.saveApp(parseSaveMcpSignInApp(body))));
    case MCP_SIGN_IN_APP_ROUTES.remove:
      return json(200, await runCauseEffect(signIns.removeApp(parseMcpSignInApp(body))));
    default:
      return json(200, signIns.app(parseMcpSignInApp(body)));
  }
}
