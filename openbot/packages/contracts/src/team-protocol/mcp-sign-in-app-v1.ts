// Frozen optional mcp-sign-in-app-v1 wire contract.
//
// An owner or admin of a joined server gives one of the host's http MCP rows an OAuth app that they
// registered with its authorization server themselves, for a server that offers no dynamic
// registration. The host keeps the app in its encrypted sign-in store, and the next sign-in on the
// host (`mcp-sign-in-v1`) uses it. A member cannot use any route; `requireAdmin` on the host is the
// only gate.
//
// The client secret travels once, in `save`, and no response contains it: `secretSaved` says only
// whether one is on file. `clientSecret: null` keeps the secret on file and an empty string removes
// it. `redirectUrl` is the exact callback the host's loopback listener answers, which the
// administrator registers with the app; it is `null` when the host has no sign-in store. Every route
// answers the row's app state after the change; `app` is `null` when the row has none.
import { adminRoute, boolean, fields, identifier, nullable, type OptionalRouteCodec, string } from "./admin-wire";

export const MCP_SIGN_IN_APP_CAPABILITY = "mcp-sign-in-app-v1";

export const MCP_SIGN_IN_APP_ROUTES = {
  get: "/v1/admin/mcp-servers/sign-in-app",
  save: "/v1/admin/mcp-servers/sign-in-app/save",
  remove: "/v1/admin/mcp-servers/sign-in-app/remove",
} as const;

const byRow = fields({ mcpServerId: identifier });
const state = fields({
  redirectUrl: nullable(string(2_048)),
  app: nullable(fields({ clientId: string(1_024), secretSaved: boolean, scope: string(2_048) })),
});

export const MCP_SIGN_IN_APP_CODECS: ReadonlyMap<string, OptionalRouteCodec> = new Map([
  [MCP_SIGN_IN_APP_ROUTES.get, adminRoute(byRow, state)],
  [
    MCP_SIGN_IN_APP_ROUTES.save,
    adminRoute(
      fields({
        mcpServerId: identifier,
        clientId: string(1_024),
        clientSecret: nullable(string(4_096)),
        scope: string(2_048),
      }),
      state,
    ),
  ],
  [MCP_SIGN_IN_APP_ROUTES.remove, adminRoute(byRow, state)],
]);
