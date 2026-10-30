import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/mobile/share";

export const messages = {
  "mobile.share.signInTitle": "Войдите, чтобы поделиться",
  "mobile.share.signInDescription":
    "Отсканируйте QR-код в OpenBot на компьютере. После этого вы сможете выбрать агента, который получит то, чем вы поделились.",
  "mobile.share.reading": "Чтение того, чем вы поделились…",
  "mobile.share.readFailed": "OpenBot не удалось прочитать то, чем вы поделились. Поделитесь ещё раз.",
  "mobile.share.content": "Что отправить",
  "mobile.share.footer":
    "Чат откроется с этим содержимым в поле сообщения. Ничего не будет отправлено, пока вы не отправите это.",
  "mobile.share.lastUsed": "Последний использованный",
  "mobile.share.noAgents": "Нет агентов",
  "mobile.share.chooseAgent": "Выберите агента",
  "mobile.share.action": "Поделиться: {name}",
} as const satisfies PartialTranslation<typeof source>;
