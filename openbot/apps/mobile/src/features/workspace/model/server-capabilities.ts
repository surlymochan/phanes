import { TEAM_BROWSER_VIEW_CAPABILITY } from "@openbot/contracts/team-protocol/browser-view-v1";
import type { TeamProtocolV1Capability } from "@openbot/contracts/team-protocol/v1";
import type { MobileTextKey } from "@openbot/i18n/mobile";
import { currentText } from "@/shared/lib/text";

/** The capabilities that each connected server advertised in its compatibility answer. */
export type ServerCapabilities = ReadonlyMap<string, readonly string[]>;

/** The host serves the sections of the chat list. */
export const SIDEBAR_LAYOUT_CAPABILITY = "sidebar-layout" satisfies TeamProtocolV1Capability;
/** The host lists its browser tabs to a member. */
const BROWSER_CONTROL_CAPABILITY = "browser-control" satisfies TeamProtocolV1Capability;

/** False for no server and for a server that has not answered its compatibility read. */
export function hasCapability(
  capabilities: ServerCapabilities,
  serverId: string | null | undefined,
  capability: string,
): boolean {
  return Boolean(serverId && capabilities.get(serverId)?.includes(capability));
}

/**
 * Refuses before the request. A host too old to know a route answers 404, so the phone shows
 * `unsupported` instead.
 */
export function requireCapability(
  capabilities: ServerCapabilities,
  serverId: string,
  capability: string,
  unsupported: MobileTextKey,
): void {
  if (!hasCapability(capabilities, serverId, capability)) throw new Error(currentText().t(unsupported));
}

/** The host lists its tabs to a member (`browser-control`) and streams one of them (`browser-view`). */
export function supportsBrowserView(capabilities: readonly string[]): boolean {
  return capabilities.includes(BROWSER_CONTROL_CAPABILITY) && capabilities.includes(TEAM_BROWSER_VIEW_CAPABILITY);
}
