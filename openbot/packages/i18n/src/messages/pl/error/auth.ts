import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/error/auth";

export const messages = {
  "error.auth.serviceUnavailable":
    "OpenBot nie może połączyć się z usługą kont. Sprawdź, czy API działa, i spróbuj ponownie.",
  "error.auth.networkBlocked":
    "Zapora lub serwer proxy w tej sieci blokuje połączenie OpenBot z {host}. Poproś administratora sieci o zezwolenie na {host} i spróbuj ponownie.",
  "error.auth.signInFirst": "Najpierw zaloguj się do OpenBot.",
  "error.auth.signInRequired": "Wymagane jest zalogowanie.",
  "error.auth.accountChangedDuringRegister": "Zalogowane konto zmieniło się podczas rejestrowania tego serwera.",
  "error.auth.hostCredentialUnavailable": "Poświadczenie zdalnego hosta jest niedostępne. Zarejestruj hosta ponownie.",
  "error.auth.codeNotVerified": "Nie udało się zweryfikować kodu logowania.",
  "error.auth.serviceError": "Usługa kont zwróciła błąd.",
  "error.auth.serviceUnreachable":
    "OpenBot nie może połączyć się z usługą kont. Sprawdź połączenie i spróbuj ponownie.",
  "error.auth.serviceTimeout": "Usługa kont nie odpowiedziała na czas. Spróbuj ponownie.",
  "error.auth.serviceStatus": "Usługa kont zwróciła błąd ({status}). Spróbuj ponownie później.",
  "error.auth.invalidHostedServer": "Usługa kont zwróciła nieprawidłowy serwer hostowany.",
  "error.auth.codeNotSent": "OpenBot nie mógł wysłać kodu logowania.",
  "error.auth.deliveryTimeout":
    "OpenBot nie zdołał na czas potwierdzić dostarczenia. Kod może jeszcze dotrzeć; sprawdź skrzynkę, zanim wyślesz go ponownie.",
  "error.auth.deliveryInterrupted":
    "Połączenie zakończyło się, zanim OpenBot potwierdził dostarczenie. Sprawdź skrzynkę, aby nie wysyłać kolejnego kodu.",
  "error.auth.deliveryUnknown":
    "OpenBot nie mógł potwierdzić, czy kod logowania został wysłany. Sprawdź skrzynkę, zanim wyślesz go ponownie.",
  "error.auth.adminKeyRequired": "Wpisz klucz administratora usługi kont.",
  "error.auth.adminKeyRejected": "Usługa kont nie przyjęła klucza administratora.",
  "error.auth.sessionNotStorable":
    "OpenBot nie może zapisać sesji logowania na tym komputerze, więc nie może zalogować się kluczem administratora.",
  "error.auth.hostedServerNoAnswer": "Serwer kont nie odpowiedział na początku.",
  "error.auth.hostedServerNoClaim": "Serwer hostowany nie jest zalogowany i nie ma poświadczenia dostępu.",
  "error.auth.hostedServerClaimMismatch": "Poświadczenie dostępu dotyczy innego serwera hostowanego.",
  "error.auth.hostedServerHostMismatch":
    "Skonfigurowany host to nie serwer hostowany, dla którego utworzono tę maszynę wirtualną.",
  "error.auth.hostedServerUnknown": "Serwer kont nie ma wpisu o tym serwerze hostowanym.",
  "error.auth.invalidUser": "Usługa kont zwróciła nieprawidłowego użytkownika.",
  "error.auth.invalidHostedServerClaim": "Nieprawidłowe poświadczenie dostępu serwera hostowanego.",
  "error.auth.sessionNotStored": "Nie udało się zapisać sesji.",
} as const satisfies PartialTranslation<typeof source>;
