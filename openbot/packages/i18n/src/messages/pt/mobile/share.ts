import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/mobile/share";

export const messages = {
  "mobile.share.signInTitle": "Entre na sua conta para compartilhar",
  "mobile.share.signInDescription":
    "Leia o código QR no OpenBot do seu computador. Depois, você poderá escolher o agente que recebe o que você compartilhou.",
  "mobile.share.reading": "Lendo o que você compartilhou…",
  "mobile.share.readFailed": "O OpenBot não conseguiu ler o que você compartilhou. Compartilhe novamente.",
  "mobile.share.content": "Para compartilhar",
  "mobile.share.footer": "O chat abre com isto no campo de mensagem. Nada é enviado até você enviar.",
  "mobile.share.lastUsed": "Usado por último",
  "mobile.share.noAgents": "Nenhum agente",
  "mobile.share.chooseAgent": "Escolha um agente",
  "mobile.share.action": "Compartilhar com {name}",
} as const satisfies PartialTranslation<typeof source>;
