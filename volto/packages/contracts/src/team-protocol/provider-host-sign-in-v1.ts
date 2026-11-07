// Frozen optional provider-host-sign-in-v1 wire contract.
//
// An owner or admin of a joined server signs the host's Gemini (`antigravity`) CLI in to Google. The
// CLI prints a Google sign-in link; the host opens it in a private tab of its own browser, and the
// client shows that tab with `browser-view`. The user signs in there, and Google sends the grant to
// the CLI's own loopback listener on the host. No link or credential travels on these routes. A
// member cannot use any route; `requireAdmin` on the host is the only gate.
//
// `start` returns at once and the sign-in runs on the host. `status` answers `tabId` while the page
// is open and `result` once the sign-in ended; both are `null` while the host waits for the link.
// `cancel` ends a waiting sign-in. How the provider's connection changes also arrives as the host's
// agent status, as for `providers-v1`.
import { adminRoute, empty, fields, nullable, type OptionalRouteCodec, oneOf, string } from "./admin-wire";

export const PROVIDER_HOST_SIGN_IN_CAPABILITY = "provider-host-sign-in-v1";

export const PROVIDER_HOST_SIGN_IN_ROUTES = {
  start: "/v1/admin/providers/host-sign-in/start",
  status: "/v1/admin/providers/host-sign-in/status",
  cancel: "/v1/admin/providers/host-sign-in/cancel",
} as const;

/** The longest error that `status` sends for an ended sign-in. */
export const PROVIDER_HOST_SIGN_IN_ERROR_LIMIT = 2_000;

/** The providers these routes sign in. */
export const PROVIDER_HOST_SIGN_IN_PROVIDERS = ["antigravity"] as const;

const byProvider = fields({ provider: oneOf(...PROVIDER_HOST_SIGN_IN_PROVIDERS) });

export const PROVIDER_HOST_SIGN_IN_CODECS: ReadonlyMap<string, OptionalRouteCodec> = new Map([
  [PROVIDER_HOST_SIGN_IN_ROUTES.start, adminRoute(byProvider, empty)],
  [
    PROVIDER_HOST_SIGN_IN_ROUTES.status,
    adminRoute(
      byProvider,
      fields({
        tabId: nullable(string(128)),
        result: nullable(fields({ error: nullable(string(PROVIDER_HOST_SIGN_IN_ERROR_LIMIT)) })),
      }),
    ),
  ],
  [PROVIDER_HOST_SIGN_IN_ROUTES.cancel, adminRoute(byProvider, empty)],
]);
