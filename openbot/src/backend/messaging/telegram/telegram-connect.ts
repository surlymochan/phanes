import type { ConnectTelegramChatInput } from "@openbot/contracts/ipc";
import type { Effect } from "effect";
import type { MessagingOperationFailed } from "../messaging-service";

/** The account service half of the OpenBot Telegram bot. Signal holds the bot token. */
export interface TelegramAppPort {
  /** A one-use code that links the next chat that adds the bot to this host. */
  createLink(): Effect.Effect<{ botUsername: string; code: string }, MessagingOperationFailed>;
  /** Unlinks a chat from this host in the account service, so Signal stops routing it here. */
  unlink(chatId: string): Effect.Effect<void, MessagingOperationFailed>;
  openExternal(url: string): Promise<void>;
}

/** The `t.me` link that adds the bot to a group, or opens a direct chat with it, with the code. */
export function telegramLinkUrl(botUsername: string, code: string, place: ConnectTelegramChatInput["place"]): string {
  const url = new URL(`https://t.me/${encodeURIComponent(botUsername)}`);
  url.searchParams.set(place === "group" ? "startgroup" : "start", code);
  return url.toString();
}
