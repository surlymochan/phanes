import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/mobile/share";

export const messages = {
  "mobile.share.signInTitle": "Connectez-vous pour partager",
  "mobile.share.signInDescription":
    "Scannez le code QR dans OpenBot sur votre ordinateur. Vous pourrez ensuite choisir l’agent qui reçoit ce que vous avez partagé.",
  "mobile.share.reading": "Lecture du contenu partagé…",
  "mobile.share.readFailed": "OpenBot n’a pas pu lire ce que vous avez partagé. Partagez-le à nouveau.",
  "mobile.share.content": "À partager",
  "mobile.share.footer":
    "La discussion s’ouvre avec ce contenu dans le champ de message. Rien n’est envoyé tant que vous ne l’envoyez pas.",
  "mobile.share.lastUsed": "Utilisé en dernier",
  "mobile.share.noAgents": "Aucun agent",
  "mobile.share.chooseAgent": "Choisissez un agent",
  "mobile.share.action": "Partager avec {name}",
} as const satisfies PartialTranslation<typeof source>;
