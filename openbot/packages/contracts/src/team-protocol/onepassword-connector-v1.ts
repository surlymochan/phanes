// Frozen optional onepassword-connector-v1 wire contract.
//
// An owner or admin of a joined server connects the host's 1Password from their own computer, with a
// service account token that they made on 1Password.com. The host has no screen, so the 1Password
// app and its CLI approval are not used. `connect` carries the token to the host once; no answer
// carries it. The host checks the token with 1Password before it keeps it, in its encrypted store.
// A member cannot use any route; `requireAdmin` on the host is the only gate.
//
// Every route answers the host's status. `state` is text, so a later host state does not break an
// older client: a client shows a state that it does not know as not connected. `vaultNames` are the
// vaults that the service account reads, and `loginCount` the logins that the host's browser can
// fill, null until the host has read them. `error` is the last connect failure, as a sentence.
import { adminRoute, count, empty, fields, list, nullable, type OptionalRouteCodec, string } from "./admin-wire";

export const ONEPASSWORD_CONNECTOR_CAPABILITY = "onepassword-connector-v1";

export const ONEPASSWORD_CONNECTOR_ROUTES = {
  status: "/v1/admin/connectors/onepassword/status",
  connect: "/v1/admin/connectors/onepassword/connect",
  disconnect: "/v1/admin/connectors/onepassword/disconnect",
} as const;

/** The longest token that the host accepts. A service account token is a few hundred characters. */
export const ONEPASSWORD_CONNECTOR_TOKEN_LIMIT = 4_096;
/** The host sends at most this many vault names, each cut to `ONEPASSWORD_CONNECTOR_VAULT_NAME_LIMIT`. */
export const ONEPASSWORD_CONNECTOR_VAULT_LIMIT = 500;
export const ONEPASSWORD_CONNECTOR_VAULT_NAME_LIMIT = 1_024;

const status = fields({
  state: string(64),
  vaultNames: list(string(ONEPASSWORD_CONNECTOR_VAULT_NAME_LIMIT), ONEPASSWORD_CONNECTOR_VAULT_LIMIT),
  loginCount: nullable(count),
  error: nullable(string(2_000)),
});

export const ONEPASSWORD_CONNECTOR_CODECS: ReadonlyMap<string, OptionalRouteCodec> = new Map([
  [ONEPASSWORD_CONNECTOR_ROUTES.status, adminRoute(empty, status)],
  [
    ONEPASSWORD_CONNECTOR_ROUTES.connect,
    adminRoute(fields({ token: string(ONEPASSWORD_CONNECTOR_TOKEN_LIMIT) }), status),
  ],
  [ONEPASSWORD_CONNECTOR_ROUTES.disconnect, adminRoute(empty, status)],
]);
