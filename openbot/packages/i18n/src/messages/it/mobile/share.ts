import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/mobile/share";

export const messages = {
  "mobile.share.signInTitle": "Accedi per condividere",
  "mobile.share.signInDescription":
    "Scansiona il codice QR in OpenBot sul computer. Poi potrai scegliere l'agente che riceve ciò che hai condiviso.",
  "mobile.share.reading": "Lettura del contenuto condiviso…",
  "mobile.share.readFailed": "OpenBot non è riuscito a leggere ciò che hai condiviso. Condividilo di nuovo.",
  "mobile.share.content": "Da condividere",
  "mobile.share.footer":
    "La chat si apre con questo contenuto nel campo del messaggio. Non viene inviato nulla finché non lo invii.",
  "mobile.share.lastUsed": "Usato per ultimo",
  "mobile.share.noAgents": "Nessun agente",
  "mobile.share.chooseAgent": "Scegli un agente",
  "mobile.share.action": "Condividi con {name}",
} as const satisfies PartialTranslation<typeof source>;
