// A Slack or Discord install that the browser client starts for a joined server (`connectors-v1`)
// returns to `/slack/connect` or `/discord/connect`, which have the same origin as the client. The
// client marks the install's nonce in local storage. When the page finds the mark, it gives the
// sealed grant to the client tab on a broadcast channel and opens no `openbot://` link. The client
// tab answers that it took the grant and sends it on to the host; only the host can open it.

import { isBoundedString } from "./ipc-bounded-values";
import { isDynamicRecord, isNumber, isOneOf } from "./runtime-values";

export const WEB_MESSAGING_INSTALL_CHANNEL = "openbot:messaging-install";

/** The same as the host's connect link: the host forgets the install's key after this time. */
export const WEB_MESSAGING_INSTALL_TTL_MS = 15 * 60_000;

export const WEB_MESSAGING_INSTALL_PLATFORMS = ["slack", "discord"] as const;
export type WebMessagingInstallPlatform = (typeof WEB_MESSAGING_INSTALL_PLATFORMS)[number];

export const WEB_MESSAGING_INSTALL_KEY_PREFIX = "openbot:messaging-install:";

/** The local storage key that marks an install the client started. */
export function webMessagingInstallKey(nonce: string): string {
  return `${WEB_MESSAGING_INSTALL_KEY_PREFIX}${nonce}`;
}

export function encodeWebMessagingInstallMark(platform: WebMessagingInstallPlatform, now: number): string {
  return JSON.stringify({ platform, expiresAt: now + WEB_MESSAGING_INSTALL_TTL_MS });
}

/** The platform of a mark that has not expired, otherwise null. */
export function decodeWebMessagingInstallMark(value: string | null, now: number): WebMessagingInstallPlatform | null {
  if (value === null) return null;
  let mark: unknown;
  try {
    mark = JSON.parse(value);
  } catch {
    return null;
  }
  return isDynamicRecord(mark) &&
    isOneOf(WEB_MESSAGING_INSTALL_PLATFORMS, mark.platform) &&
    isNumber(mark.expiresAt) &&
    mark.expiresAt > now
    ? mark.platform
    : null;
}

/** What the return page sends. The same limits as the `connectors-v1` complete route. */
export interface WebMessagingInstallGrant {
  platform: WebMessagingInstallPlatform;
  nonce: string;
  grant: string;
}

export function decodeWebMessagingInstallGrant(value: unknown): WebMessagingInstallGrant | null {
  return isDynamicRecord(value) &&
    isOneOf(WEB_MESSAGING_INSTALL_PLATFORMS, value.platform) &&
    isBoundedString(value.nonce, 128) &&
    isBoundedString(value.grant, 16_384)
    ? { platform: value.platform, nonce: value.nonce, grant: value.grant }
    : null;
}

/** What the client tab answers when it took the grant of `nonce`. */
export function webMessagingInstallReceipt(nonce: string): { received: string } {
  return { received: nonce };
}

/** The nonce of a client tab's answer, otherwise null. */
export function decodeWebMessagingInstallReceipt(value: unknown): string | null {
  return isDynamicRecord(value) && isBoundedString(value.received, 128) ? value.received : null;
}
