import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/status/agent";

export const messages = {
  "status.agent.dispatchApproval":
    "Dieser Code-Auftrag enthält einen riskanten Vorgang. Prüfe vor der Freigabe seine Karte und seine Argumente.",
  "status.agent.dispatchStarted": "Code-Arbeit gestartet: {title}. Host: {host}. Lauf: {runId}.",
  "status.agent.dispatchFinished":
    "Code-Arbeit beendet: {title}. Host: {host}. Ergebnis: {status}. Prüfe das Ergebnis, bevor du Erfolg meldest.",
  "status.agent.dispatchNoResult": "Der openswarm-Läufer endete ohne Ergebnisdatei.",
  "status.agent.claudeWriteOutside":
    "In {path} schreiben, außerhalb des Agentenarbeitsbereichs, des geteilten Ordners und der temporären Ordner.",
  "status.agent.contextCleared": "Kontext geleert. Hier beginnt ein neuer Chat.",
  "status.agent.modelSwitchedForLimit": "Nutzungslimit erreicht. Zu {model} gewechselt.",
  "status.agent.modelReturnedAfterLimit": "Nutzungslimit beendet. Zurück zu {model} gewechselt.",
  "status.agent.pausedHourBudget":
    "Token-Budget erreicht: {used} Tokens in der letzten Stunde (Limit {limit}). Neue Arbeit wartet, bis du fortfährst.",
  "status.agent.pausedTurnBudget":
    "Nach einem Schritt gestoppt: Dieser Durchlauf hat {used} Tokens verwendet (Limit {limit}). Die Arbeit bleibt erhalten. Fahre fort, um weiterzumachen.",
  "status.agent.pausedPlanLimit":
    "Das Plan-Limit hat diesen Durchlauf gestoppt. Die Arbeit bleibt erhalten und läuft weiter, wenn das Limit endet.",
  "status.agent.modelSwitchedForError": "Anbieterfehler. Zu {model} gewechselt.",
  "status.agent.modelReturnedAfterError": "Nach dem Anbieterfehler zurück zu {model} gewechselt.",
  "status.agent.marketplaceSuggested": "Eine Marketplace-App wurde vorgeschlagen: {app}.",
  "status.agent.channelMemberAdded": "{agent} hat {member} zum Kanal hinzugefügt. {person} hat darum gebeten.",
  "status.agent.channelMemberRemoved": "{agent} hat {member} aus dem Kanal entfernt. {person} hat darum gebeten.",
  "status.agent.channelInstructionsChanged":
    "{agent} hat die Anweisungen des Kanals geändert. {person} hat darum gebeten.",
  "status.agent.channelRoutineCreated": "{agent} hat die Routine {routine} erstellt. {person} hat darum gebeten.",
  "status.agent.channelRoutineUpdated": "{agent} hat die Routine {routine} geändert. {person} hat darum gebeten.",
  "status.agent.channelRoutineDeleted": "{agent} hat die Routine {routine} gelöscht. {person} hat darum gebeten.",
  "status.agent.screenCaptureScreen": "Den Hauptbildschirm aufnehmen und das Bild an das Modell des Agenten senden.",
  "status.agent.screenCaptureChannel":
    "Das Bild erscheint im Kanal {channel}, und jedes Mitglied dieses Servers, das den Kanal liest, kann es sehen.",
  "status.agent.screenCaptureDisplay": "Den Bildschirm {name} aufnehmen und das Bild an das Modell des Agenten senden.",
  "status.agent.screenCaptureWindow": "Das Fenster {name} aufnehmen und das Bild an das Modell des Agenten senden.",
  "status.agent.screenCaptureRegion":
    "Den Bildschirmbereich bei {x}, {y}, {width} × {height} Punkten aufnehmen und das Bild an das Modell des Agenten senden.",
  "status.agent.screenCaptureProgress": "Bildschirmaufnahme wird angefragt…",
  "status.agent.screenCaptureReviewing": "Bildschirmaufnahme wird geprüft…",
  "status.agent.starting": "Lokale Agenten-CLIs werden gestartet…",
  "status.agent.memoriesSearching": "Erinnerungen werden durchsucht…",
  "status.agent.memoriesSearched": {
    one: "Erinnerungen durchsucht: {count} gefunden",
    other: "Erinnerungen durchsucht: {count} gefunden",
  },
  "status.agent.memoriesUpdating": "Erinnerungen werden aktualisiert…",
  "status.agent.skillUsing": "Fähigkeit {name} wird verwendet…",
  "status.agent.skillReading": "Fähigkeit wird gelesen…",
  "status.agent.memoryChanges": "Erinnerungen: {saved} gespeichert, {updated} aktualisiert, {forgotten} vergessen.",
  "status.agent.memoryChangesWithConflicts":
    "Erinnerungen: {saved} gespeichert, {updated} aktualisiert, {forgotten} vergessen, {notChanged} nicht geändert, weil ein anderer Thread sie zuerst geändert hat.",
} as const satisfies PartialTranslation<typeof source>;
