import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/mobile/share";

export const messages = {
  "mobile.share.signInTitle": "サインインして共有",
  "mobile.share.signInDescription":
    "コンピューターの OpenBot に表示される QR コードをスキャンしてください。その後、共有した内容を受け取るエージェントを選べます。",
  "mobile.share.reading": "共有された内容を読み込んでいます…",
  "mobile.share.readFailed": "OpenBot は共有された内容を読み込めませんでした。もう一度共有してください。",
  "mobile.share.content": "共有する内容",
  "mobile.share.footer": "チャットが開き、この内容がメッセージ欄に入ります。送信するまで何も送られません。",
  "mobile.share.lastUsed": "前回使用",
  "mobile.share.noAgents": "エージェントがありません",
  "mobile.share.chooseAgent": "エージェントを選択",
  "mobile.share.action": "{name} と共有",
} as const satisfies PartialTranslation<typeof source>;
