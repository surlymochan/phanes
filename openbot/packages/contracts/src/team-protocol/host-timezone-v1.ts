// Frozen optional host-timezone-v1 wire contract.
//
// What it grants, recorded here because freezing it makes it permanent: an owner or admin of a
// server can read and set the server timezone. A routine that an agent creates with no timezone
// from the tool call or from the sender's message uses it. `timezone: null` is automatic: the host
// uses its system zone, which the snapshot gives as `systemTimezone`. The host refuses a zone that
// its runtime cannot resolve. A change does not move existing routines. A member cannot use either
// route; `requireAdmin` on the host is the only gate. Widening any of it needs a second capability
// string.
import { adminRoute, empty, fields, nullable, type OptionalRouteCodec, string } from "./admin-wire";

export const HOST_TIMEZONE_CAPABILITY = "host-timezone-v1";

export const HOST_TIMEZONE_ROUTES = {
  settings: "/v1/admin/host/timezone",
  update: "/v1/admin/host/timezone/update",
} as const;

const zone = string(128);
const snapshot = fields({ timezone: nullable(zone), systemTimezone: zone });

export const HOST_TIMEZONE_CODECS: ReadonlyMap<string, OptionalRouteCodec> = new Map([
  [HOST_TIMEZONE_ROUTES.settings, adminRoute(empty, snapshot)],
  [HOST_TIMEZONE_ROUTES.update, adminRoute(fields({ timezone: nullable(zone) }), snapshot)],
]);
