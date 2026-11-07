import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/error/messaging";

export const messages = {
  "error.messaging.notConnected": "Ten obszar roboczy Slack nie jest połączony.",
  "error.messaging.unsupported": "Ten komputer nie może połączyć się ze Slackiem.",
  "error.messaging.relayUnavailable":
    "volto nie może odbierać zdarzeń Slack na tym komputerze. Zaloguj się, nadaj temu komputerowi nazwę i spróbuj ponownie.",
  "error.messaging.discordNotConnected": "Ten serwer Discord nie jest połączony.",
  "error.messaging.discordUnsupported": "Ten komputer nie może połączyć się z Discordem.",
  "error.messaging.discordRelayUnavailable":
    "volto nie może odbierać zdarzeń Discord na tym komputerze. Zaloguj się, nadaj temu komputerowi nazwę i spróbuj ponownie.",
  "error.messaging.discordRolesUnsupported": "volto nie może jeszcze pokazać ról Discorda. Spróbuj ponownie później.",
  "error.messaging.telegramNotConnected": "Ten czat Telegram nie jest połączony.",
  "error.messaging.telegramUnsupported": "Ten komputer nie może połączyć się z Telegramem.",
  "error.messaging.telegramRelayUnavailable":
    "volto nie może połączyć się z Telegramem na tym komputerze. Zaloguj się, nadaj temu komputerowi nazwę i spróbuj ponownie.",
  "error.messaging.telegramServiceUnavailable": "volto nie może teraz połączyć Telegrama. Spróbuj ponownie później.",
  "error.messaging.memoryLimit":
    "Połączony obszar roboczy może mieć maksymalnie {limit} wspomnień. Najpierw usuń jedno.",
} as const satisfies PartialTranslation<typeof source>;
