/**
 * The server timezone (`host-timezone-v1`), as the desktop main process, the preload and the
 * browser client read it. On the wire the route codec has already checked its fields; this gives
 * them their IPC types and fails closed on anything else.
 */

import { isDynamicRecord, isString } from "./runtime-values";

export interface HostTimezoneSettings {
  /** The IANA zone an owner or admin chose, or null for automatic: the host system zone. */
  timezone: string | null;
  /** The host system zone, which automatic uses. */
  systemTimezone: string;
}

export function decodeHostTimezoneSettings(value: unknown): HostTimezoneSettings {
  if (
    !isDynamicRecord(value) ||
    (value.timezone !== null && !isString(value.timezone)) ||
    !isString(value.systemTimezone)
  ) {
    throw new Error("Invalid server timezone.");
  }
  return { timezone: value.timezone, systemTimezone: value.systemTimezone };
}
