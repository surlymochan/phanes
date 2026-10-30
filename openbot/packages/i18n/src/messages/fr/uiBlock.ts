import type { PartialTranslation } from "../../message";
import type { messages as source } from "../en/uiBlock";

export const messages = {
  "uiBlock.status.pending": "Attend votre réponse",
  "uiBlock.status.answered": "Répondu",
  "uiBlock.status.expired": "Expiré",
  "uiBlock.status.closed": "Fermé",
  "uiBlock.outcome.expired": "Aucune réponse donnée à temps",
  "uiBlock.outcome.closed": "Fermé sans réponse",
  "uiBlock.confirm.holdHint":
    "Maintenez le bouton enfoncé pour confirmer, ou appuyez une fois pour confirmer à la deuxième étape",
  "uiBlock.confirm.prompt": "Confirmer : {action} ?",
  "uiBlock.confirm.accept": "Confirmer",
  "uiBlock.confirm.previewLabel": "Aperçu",
  "uiBlock.choice.submit": "Envoyer",
  "uiBlock.choice.shortcutHint": "Appuyez de {first} à {last} pour choisir",
  "uiBlock.choice.selectedCount": { one: "{count} sélectionné", other: "{count} sélectionnés" },
  "uiBlock.choice.noneSelected": "Rien de sélectionné",
  "uiBlock.quick.label": "Réponses rapides",
  "uiBlock.quick.textLabel": "Votre propre réponse",
  "uiBlock.quick.textPlaceholder": "Ou écrivez votre propre réponse",
  "uiBlock.quick.send": "Envoyer",
  "uiBlock.form.submit": "Envoyer",
  "uiBlock.form.required": "Ce champ est obligatoire",
  "uiBlock.form.choose": "Choisissez une option",
  "uiBlock.form.notSet": "Non défini",
  "uiBlock.form.invalid": "Certaines réponses sont incorrectes. Vérifiez-les et réessayez.",
  "uiBlock.skip": "Ignorer",
  "uiBlock.error.answerFailed": "Impossible d’envoyer votre réponse. Réessayez.",
} as const satisfies PartialTranslation<typeof source>;
