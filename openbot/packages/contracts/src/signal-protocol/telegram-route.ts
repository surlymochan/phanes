// The Telegram route: how a Telegram update finds the host that answers it, and how that host
// answers without the bot token.
//
// One OpenBot bot serves every user. Its token is in Signal only: a host that had it could read and
// post in every chat of every user. Telegram posts each update to
// `https://signal.openbot.run/v1/telegram/updates/<bot ID>` with a secret header. Signal checks the
// header, reads the chat ID, and passes the update to the `ingress` socket that holds a route ticket
// for that chat. The host calls the Bot API through the same socket (`telegram-call`): Signal
// accepts only the methods below, with only the parameters below, and only for the chats routed to
// that socket.
//
// A chat is linked to a host with a one-use code. The host asks the account service for a code, and
// the user opens `https://t.me/<bot>?startgroup=<code>` (a group) or `?start=<code>` (a direct chat).
// Telegram then sends `/start <code>` in that chat. Signal asks the account service to link the chat
// to the host that asked for the code, and routes the chat to that host's socket at once.
//
// The route ticket is an ES256 JWT that `apps/auth-api` mints for a host that proves its machine
// token, with the key of the Slack route ticket and its own audience. It names only the chats that
// the account service links to that host, and it expires: the host asks for a new one each time its
// `ingress` socket connects.

export const TELEGRAM_ROUTE_AUDIENCE = "openbot-telegram-route";

/** Followed by `/<bot ID>`. */
export const TELEGRAM_UPDATES_PATH = "/v1/telegram/updates";
/** Followed by `/<file token>`. The host downloads one file of an update. */
export const TELEGRAM_FILES_PATH = "/v1/telegram/files";
/** Followed by `/<upload token>`. The host posts the bytes of one document. */
export const TELEGRAM_UPLOADS_PATH = "/v1/telegram/uploads";

/** The `ready` capability of a Signal that has a Telegram bot. Without it, a host sends no Telegram frame. */
export const TELEGRAM_CAPABILITY = "telegram";

export const TELEGRAM_ROUTE_TTL_SECONDS = 5 * 60;

/** The most chats one host can link. */
export const TELEGRAM_ROUTE_CHATS_LIMIT = 64;

/** The largest update Signal passes to a host. Signal drops a larger one. */
export const TELEGRAM_UPDATE_BYTES_LIMIT = 64 * 1024;

/** The Bot API's own limit for a file a bot downloads. Signal applies it to uploads too. */
export const TELEGRAM_FILE_BYTES_LIMIT = 20 * 1024 * 1024;

/**
 * The longest `text` of a call. Telegram allows 4,096 characters after it parses the HTML, but the
 * escapes and tags make the HTML longer. The host keeps a call's text under this many UTF-8 bytes,
 * and Signal refuses a text over this many characters: a refused frame closes the whole socket.
 */
export const TELEGRAM_TEXT_LIMIT = 32_000;

/** How long a file or upload token stays valid. */
export const TELEGRAM_FILE_TOKEN_TTL_SECONDS = 2 * 60;

/** How long a link code stays valid. */
export const TELEGRAM_LINK_CODE_TTL_SECONDS = 15 * 60;

/** A `start` parameter: Telegram allows `A-Z a-z 0-9 _ -`, at most 64 characters. */
export const TELEGRAM_LINK_CODE_PATTERN = /^[A-Za-z0-9_-]{32,64}$/u;

/** A chat ID as a decimal string. Groups and channels are negative. */
export const TELEGRAM_CHAT_ID_PATTERN = /^-?[0-9]{1,20}$/u;

/** A bot ID: the part of the bot token before the colon. */
export const TELEGRAM_BOT_ID_PATTERN = /^[0-9]{1,20}$/u;

export interface TelegramRouteClaims {
  aud: typeof TELEGRAM_ROUTE_AUDIENCE;
  // The remote host that receives the updates.
  hid: string;
  chats: TelegramRouteChat[];
  iat: number;
  exp: number;
}

export interface TelegramRouteChat {
  // The chat ID, as a decimal string.
  id: string;
  // The OpenBot bot in the chat. The production and development bots can share a chat.
  botId: string;
  // When the account service linked the chat to the host, in milliseconds. Signal keeps a chat with
  // its newest link, so a host that lost the chat cannot take it back with an older ticket.
  linkedAt: number;
}

/**
 * One link of a chat: the bot, the chat and the time of the link. Validation compares all three: the
 * production and development bots can share a chat, and one of their routes can be revoked alone.
 */
export function telegramRouteChatKey(chat: TelegramRouteChat): string {
  return `${chat.botId}:${chat.id}:${chat.linkedAt}`;
}

export interface TelegramInlineButton {
  text: string;
  /** 1 to 64 bytes. */
  callback_data: string;
}

export interface TelegramInlineKeyboard {
  inline_keyboard: TelegramInlineButton[][];
}

export interface TelegramReplyParameters {
  message_id: number;
  allow_sending_without_reply?: boolean;
}

/**
 * The Bot API calls a host can make, with exactly these parameters. Signal refuses any other key:
 * `reply_parameters.chat_id`, for example, would reply into a chat of another host.
 */
export interface TelegramCallParams {
  getMe: Record<string, never>;
  sendMessage: {
    chat_id: number;
    text: string;
    parse_mode?: "HTML";
    message_thread_id?: number;
    reply_parameters?: TelegramReplyParameters;
    reply_markup?: TelegramInlineKeyboard;
    link_preview_options?: { is_disabled: true };
  };
  editMessageText: {
    chat_id: number;
    message_id: number;
    text: string;
    parse_mode?: "HTML";
    reply_markup?: TelegramInlineKeyboard;
    link_preview_options?: { is_disabled: true };
  };
  deleteMessage: { chat_id: number; message_id: number };
  setMessageReaction: {
    chat_id: number;
    message_id: number;
    /** At most one: a bot sets one reaction on a message. Empty removes it. */
    reaction: Array<{ type: "emoji"; emoji: string }>;
  };
  /** Only for a callback query that Signal delivered to this socket. */
  answerCallbackQuery: { callback_query_id: string; text?: string; show_alert?: boolean };
  leaveChat: { chat_id: number };
  /** `chat_id` is the chat whose update named the file. */
  getFile: { chat_id: number; file_id: string };
  /** Returns an upload token. The host then posts the bytes to `TELEGRAM_UPLOADS_PATH`. */
  sendDocument: {
    chat_id: number;
    file_name: string;
    message_thread_id?: number;
    reply_parameters?: TelegramReplyParameters;
  };
}

export type TelegramCallMethod = keyof TelegramCallParams;

export const TELEGRAM_CALL_METHODS = [
  "getMe",
  "sendMessage",
  "editMessageText",
  "deleteMessage",
  "setMessageReaction",
  "answerCallbackQuery",
  "leaveChat",
  "getFile",
  "sendDocument",
] as const satisfies readonly TelegramCallMethod[];

/**
 * What Signal returns of a call: only the fields the host needs, never the Bot API's whole answer.
 * `getMe` gives `botId` and `username`; `sendMessage` gives `messageId`; `getFile` gives `fileToken`
 * and `fileSize`; `sendDocument` gives `uploadToken`. The others give nothing.
 */
export interface TelegramCallResult {
  messageId?: number;
  botId?: string;
  username?: string;
  fileToken?: string;
  fileSize?: number;
  uploadToken?: string;
}

/** A failed call. `errorCode` is the Bot API's, or 403 when Signal refused the call itself. */
export interface TelegramCallFailure {
  errorCode: number;
  /** The Bot API's description, at most 256 characters. Never message text. */
  description: string;
  /** Seconds, for a 429. */
  retryAfter?: number;
}

/** The answer of `POST TELEGRAM_UPLOADS_PATH/<token>`: JSON with `messageId`, or a failure. */
export type TelegramUploadAnswer = { ok: true; messageId: number } | ({ ok: false } & TelegramCallFailure);
