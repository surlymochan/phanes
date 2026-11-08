import type { PartialTranslation } from "../../message";
import type { messages as source } from "../en/uiBlock";

export const messages = {
  "uiBlock.status.pending": "Benötigt deine Antwort",
  "uiBlock.status.answered": "Beantwortet",
  "uiBlock.status.expired": "Abgelaufen",
  "uiBlock.status.closed": "Geschlossen",
  "uiBlock.outcome.expired": "Keine Antwort rechtzeitig gegeben",
  "uiBlock.outcome.closed": "Ohne Antwort geschlossen",
  "uiBlock.confirm.holdHint":
    "Halte die Taste gedrückt, um zu bestätigen, oder drücke sie, um im zweiten Schritt zu bestätigen",
  "uiBlock.confirm.prompt": "Bestätigen: {action}?",
  "uiBlock.confirm.accept": "Bestätigen",
  "uiBlock.confirm.previewLabel": "Vorschau",
  "uiBlock.choice.submit": "Senden",
  "uiBlock.choice.shortcutHint": "Drücke {first} bis {last}, um auszuwählen",
  "uiBlock.choice.selectedCount": { one: "{count} ausgewählt", other: "{count} ausgewählt" },
  "uiBlock.choice.noneSelected": "Nichts ausgewählt",
  "uiBlock.quick.label": "Schnellantworten",
  "uiBlock.quick.textLabel": "Eigene Antwort",
  "uiBlock.quick.textPlaceholder": "Oder schreibe deine eigene Antwort",
  "uiBlock.quick.send": "Senden",
  "uiBlock.form.submit": "Absenden",
  "uiBlock.form.required": "Dieses Feld ist erforderlich",
  "uiBlock.form.choose": "Wähle eine Option",
  "uiBlock.form.notSet": "Nicht festgelegt",
  "uiBlock.form.invalid": "Einige Antworten sind nicht richtig. Prüfe sie und versuche es erneut.",
  "uiBlock.skip": "Überspringen",
  "uiBlock.error.answerFailed": "Deine Antwort konnte nicht gesendet werden. Versuche es erneut.",
} as const satisfies PartialTranslation<typeof source>;
