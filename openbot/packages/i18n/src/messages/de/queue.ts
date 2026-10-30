import type { PartialTranslation } from "../../message";
import type { messages as source } from "../en/queue";

export const messages = {
  "queue.label": "Nachrichtenwarteschlange",
  "queue.moved": "Die Nachricht wurde in der Warteschlange an Position {position} von {total} verschoben.",
  "queue.attachment": "Anhang",
  "queue.hold.named": "Wartet – {name} arbeitet in {channel}",
  "queue.hold.unnamed": "Wartet – dieser Agent arbeitet in {channel}",
  "queue.item.label": "Nachricht {position} in der Warteschlange: {preview}",
  "queue.item.labelEditing": "Nachricht {position} in der Warteschlange, in Bearbeitung: {preview}",
  "queue.item.editing": "In Bearbeitung",
  "queue.item.steerLabel": "Nachricht {position} aus der Warteschlange steuern",
  "queue.item.steerTooltip": "Nachricht steuern",
  "queue.item.steering": "Wird gesteuert",
  "queue.item.steer": "Steuern",
  "queue.item.notSteered": "Nicht gesteuert",
  "queue.item.steerFallback.providerUnsupported":
    "Dieser Anbieter kann einen laufenden Durchlauf nicht steuern. Die Nachricht wartet daher in der Warteschlange.",
  "queue.item.steerFallback.steerFailed":
    "Die Nachricht konnte nicht gesteuert werden und wartet daher in der Warteschlange.",
  "queue.item.runAsTask": "In neuem Thread ausführen",
  "queue.item.runAsTaskLabel": "Nachricht {position} aus der Warteschlange in einem neuen Thread ausführen",
  "queue.item.deleteLabel": "Nachricht {position} aus der Warteschlange löschen",
  "queue.item.deleteTooltip": "Nachricht löschen",
  "queue.item.editLabel": "Nachricht {position} in der Warteschlange bearbeiten",
  "queue.item.editTooltip": "Nachricht bearbeiten",
  "queue.deleteHeld.title": "Nachricht aus der Warteschlange löschen?",
  "queue.deleteHeld.body": "Ein anderes Gerät bearbeitet diese Nachricht. Der Agent wird sie nicht erhalten.",
  "queue.deleteHeld.keep": "Behalten",
  "queue.waiting.status": { one: "{count} Nachricht wartet", other: "{count} Nachrichten warten" },
  "queue.paused.status": {
    one: "Pausiert · {count} Nachricht behalten",
    other: "Pausiert · {count} Nachrichten behalten",
  },
  "queue.pause": "Pausieren",
  "queue.pause.tooltip":
    "Starte keine Nachricht aus der Warteschlange, bevor du sie fortsetzt. Die aktuelle Arbeit läuft weiter, und nichts geht verloren.",
  "queue.resume": "Fortsetzen",
  "queue.resume.tooltip": "Starte die Nachrichten aus der Warteschlange wieder an, in Reihenfolge",
  "queue.clearAll.label": "Alle Nachrichten in der Warteschlange löschen",
  "queue.clearAll.tooltip": "Alle Nachrichten in der Warteschlange löschen",
  "queue.clearAll.title": {
    one: "{count} Nachricht aus der Warteschlange löschen?",
    other: "{count} Nachrichten aus der Warteschlange löschen?",
  },
  "queue.clearAll.body": "Der Agent erhält sie nicht. Die aktuelle Arbeit läuft weiter.",
  "queue.clearAll.confirm": "Alle löschen",
  "queue.item.runNowLabel": "Aktuelle Arbeit stoppen und Nachricht {position} aus der Warteschlange jetzt ausführen",
  "queue.item.runNowTooltip": "Jetzt ausführen: aktuelle Arbeit stoppen und diese Nachricht als Nächstes starten",
} as const satisfies PartialTranslation<typeof source>;
