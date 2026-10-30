// Frozen optional focus-use-v1 wire contract.
//
// What it grants, recorded here because freezing it makes it permanent: a signed-in member's client
// tells the host that its app has focus (a focused desktop window, a visible and focused browser tab,
// the phone app in the foreground). The body is empty and the host answers with an empty object. The
// host counts it as use of a hosted server for a limited time after the later of the last send,
// change or typing event of the same session and the start of its focus period, so a member who only
// reads does not see the server stop. Only a hosted server advertises it, and any member can send it.
// Widening any of it needs a second capability string.
import { adminRoute, empty, type OptionalRouteCodec } from "./admin-wire";

export const FOCUS_USE_CAPABILITY = "focus-use-v1";

export const FOCUS_USE_ROUTES = { ping: "/v1/client/focus" } as const;

/** Shorter than the 5-minute client use window of the host, so the use does not lapse between two pings. */
export const FOCUS_USE_INTERVAL_MS = 4 * 60_000;

export const FOCUS_USE_CODECS: ReadonlyMap<string, OptionalRouteCodec> = new Map([
  [FOCUS_USE_ROUTES.ping, adminRoute(empty, empty)],
]);
