import type { PartialTranslation } from "../../message";
import type { messages as source } from "../en/uiBlock";

export const messages = {
  "uiBlock.status.pending": "Richiede la tua risposta",
  "uiBlock.status.answered": "Risposto",
  "uiBlock.status.expired": "Scaduto",
  "uiBlock.status.closed": "Chiuso",
  "uiBlock.outcome.expired": "Nessuna risposta data in tempo",
  "uiBlock.outcome.closed": "Chiuso senza risposta",
  "uiBlock.confirm.holdHint":
    "Tieni premuto il pulsante per confermare, oppure premilo per confermare in un secondo passaggio",
  "uiBlock.confirm.prompt": "Confermi: {action}?",
  "uiBlock.confirm.accept": "Conferma",
  "uiBlock.confirm.previewLabel": "Anteprima",
  "uiBlock.choice.submit": "Invia",
  "uiBlock.choice.shortcutHint": "Premi da {first} a {last} per scegliere",
  "uiBlock.choice.selectedCount": { one: "{count} selezionato", other: "{count} selezionati" },
  "uiBlock.choice.noneSelected": "Nessuna selezione",
  "uiBlock.quick.label": "Risposte rapide",
  "uiBlock.quick.textLabel": "La tua risposta",
  "uiBlock.quick.textPlaceholder": "O scrivi la tua risposta",
  "uiBlock.quick.send": "Invia",
  "uiBlock.form.submit": "Invia",
  "uiBlock.form.required": "Questo campo è obbligatorio",
  "uiBlock.form.choose": "Scegli un'opzione",
  "uiBlock.form.notSet": "Non impostato",
  "uiBlock.form.invalid": "Alcune risposte non sono corrette. Controllale e riprova.",
  "uiBlock.skip": "Salta",
  "uiBlock.error.answerFailed": "Impossibile inviare la tua risposta. Riprova.",
} as const satisfies PartialTranslation<typeof source>;
