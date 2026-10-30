import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/error/auth";

export const messages = {
  "error.auth.serviceUnavailable":
    "OpenBot konnte den Kontodienst nicht erreichen. Prüfe, ob die API läuft, und versuche es erneut.",
  "error.auth.networkBlocked":
    "Eine Firewall oder ein Proxy in diesem Netzwerk hat den Zugriff von OpenBot auf {host} blockiert. Bitte deinen Netzwerkadministrator, {host} zu erlauben, und versuche es erneut.",
  "error.auth.signInFirst": "Melde dich zuerst bei OpenBot an.",
  "error.auth.signInRequired": "Eine Anmeldung ist erforderlich.",
  "error.auth.accountChangedDuringRegister":
    "Das angemeldete Konto wurde während der Registrierung dieses Servers geändert.",
  "error.auth.hostCredentialUnavailable":
    "Die Zugangsdaten des entfernten Hosts sind nicht verfügbar. Registriere den Host erneut.",
  "error.auth.codeNotVerified": "Der Anmeldecode konnte nicht überprüft werden.",
  "error.auth.serviceError": "Der Kontodienst hat einen Fehler zurückgegeben.",
  "error.auth.serviceUnreachable":
    "OpenBot konnte den Kontodienst nicht erreichen. Prüfe deine Verbindung und versuche es erneut.",
  "error.auth.serviceTimeout": "Der Kontodienst hat nicht rechtzeitig geantwortet. Versuche es erneut.",
  "error.auth.serviceStatus": "Der Kontodienst hat einen Fehler zurückgegeben ({status}). Versuche es später erneut.",
  "error.auth.invalidHostedServer": "Der Kontodienst hat einen ungültigen gehosteten Server zurückgegeben.",
  "error.auth.codeNotSent": "OpenBot konnte den Anmeldecode nicht senden.",
  "error.auth.deliveryTimeout":
    "OpenBot konnte die Zustellung nicht rechtzeitig bestätigen. Der Code kann noch eintreffen. Prüfe die Zustellung, bevor du ihn erneut sendest.",
  "error.auth.deliveryInterrupted":
    "Die Verbindung wurde beendet, bevor OpenBot die Zustellung bestätigt hat. Prüfe die Zustellung, um keinen weiteren Code zu senden.",
  "error.auth.deliveryUnknown":
    "OpenBot konnte nicht bestätigen, ob der Anmeldecode gesendet wurde. Prüfe die Zustellung, bevor du ihn erneut sendest.",
  "error.auth.adminKeyRequired": "Gib den Admin-Schlüssel des Kontodienstes ein.",
  "error.auth.adminKeyRejected": "Der Kontodienst hat den Admin-Schlüssel nicht akzeptiert.",
  "error.auth.sessionNotStorable":
    "OpenBot kann die Anmeldesitzung auf diesem Computer nicht speichern und sich daher nicht mit einem Admin-Schlüssel anmelden.",
  "error.auth.hostedServerNoAnswer": "Der Kontoserver hat am Anfang nicht geantwortet.",
  "error.auth.hostedServerNoClaim": "Der gehostete Server ist abgemeldet und hat keinen Zugangsnachweis.",
  "error.auth.hostedServerClaimMismatch": "Der Zugangsnachweis gehört zu einem anderen gehosteten Server.",
  "error.auth.hostedServerHostMismatch":
    "Der konfigurierte Host ist nicht der gehostete Server, für den diese VM erstellt wurde.",
  "error.auth.hostedServerUnknown": "Der Kontoserver hat keinen Eintrag zu diesem gehosteten Server.",
  "error.auth.invalidUser": "Der Kontodienst hat einen ungültigen Benutzer zurückgegeben.",
  "error.auth.invalidHostedServerClaim": "Ungültiger Zugangsnachweis des gehosteten Servers.",
  "error.auth.sessionNotStored": "Die Sitzung konnte nicht gespeichert werden.",
} as const satisfies PartialTranslation<typeof source>;
