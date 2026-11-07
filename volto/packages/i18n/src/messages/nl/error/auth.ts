import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/error/auth";

export const messages = {
  "error.auth.serviceUnavailable":
    "volto kan de accountservice niet bereiken. Controleer of de API draait en probeer het opnieuw.",
  "error.auth.networkBlocked":
    "Een firewall of proxy op dit netwerk blokkeert de verbinding van volto met {host}. Vraag je netwerkbeheerder om {host} toe te staan en probeer het opnieuw.",
  "error.auth.signInFirst": "Log eerst in bij volto.",
  "error.auth.signInRequired": "Inloggen is verplicht.",
  "error.auth.accountChangedDuringRegister":
    "Het ingelogde account is gewijzigd terwijl deze server werd geregistreerd.",
  "error.auth.hostCredentialUnavailable":
    "De inloggegevens van de externe host zijn niet beschikbaar. Registreer de host opnieuw.",
  "error.auth.codeNotVerified": "De inlogcode kon niet worden geverifieerd.",
  "error.auth.serviceError": "De accountservice gaf een fout.",
  "error.auth.serviceUnreachable":
    "volto kan de accountservice niet bereiken. Controleer je verbinding en probeer het opnieuw.",
  "error.auth.serviceTimeout": "De accountservice reageerde niet op tijd. Probeer het opnieuw.",
  "error.auth.serviceStatus": "De accountservice gaf een fout ({status}). Probeer het later opnieuw.",
  "error.auth.invalidHostedServer": "De accountservice gaf een ongeldige gehoste server terug.",
  "error.auth.codeNotSent": "volto kon de inlogcode niet versturen.",
  "error.auth.deliveryTimeout":
    "volto kon de bezorging niet op tijd bevestigen. De code kan nog aankomen; controleer de bezorging voordat je opnieuw verstuurt.",
  "error.auth.deliveryInterrupted":
    "De verbinding werd verbroken voordat volto de bezorging bevestigde. Controleer de bezorging om te voorkomen dat je nog een code verstuurt.",
  "error.auth.deliveryUnknown":
    "volto kon niet bevestigen of de inlogcode is verstuurd. Controleer de bezorging voordat je opnieuw verstuurt.",
} as const satisfies PartialTranslation<typeof source>;
