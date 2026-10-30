import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/error/auth";

export const messages = {
  "error.auth.serviceUnavailable":
    "OpenBot non è riuscito a raggiungere il servizio account. Controlla che l'API sia in esecuzione, poi riprova.",
  "error.auth.networkBlocked":
    "Un firewall o un proxy di questa rete ha impedito a OpenBot di raggiungere {host}. Chiedi all'amministratore di rete di consentire {host}, poi riprova.",
  "error.auth.signInFirst": "Accedi prima a OpenBot.",
  "error.auth.signInRequired": "Serve l'accesso.",
  "error.auth.accountChangedDuringRegister": "L'account connesso è cambiato durante la registrazione di questo server.",
  "error.auth.hostCredentialUnavailable":
    "La credenziale dell'host remoto non è disponibile. Registra di nuovo l'host.",
  "error.auth.codeNotVerified": "Non è stato possibile verificare il codice di accesso.",
  "error.auth.serviceError": "Il servizio account ha restituito un errore.",
  "error.auth.serviceUnreachable":
    "OpenBot non è riuscito a raggiungere il servizio account. Controlla la connessione, poi riprova.",
  "error.auth.serviceTimeout": "Il servizio account non ha risposto in tempo. Riprova.",
  "error.auth.serviceStatus": "Il servizio account ha restituito un errore ({status}). Riprova più tardi.",
  "error.auth.invalidHostedServer": "Il servizio account ha restituito un server ospitato non valido.",
  "error.auth.codeNotSent": "OpenBot non è riuscito a inviare il codice di accesso.",
  "error.auth.deliveryTimeout":
    "OpenBot non ha potuto confermare la consegna in tempo. Il codice potrebbe comunque arrivare: controlla la consegna prima di inviarlo di nuovo.",
  "error.auth.deliveryInterrupted":
    "La connessione si è interrotta prima che OpenBot confermasse la consegna. Controlla la consegna per evitare di inviare un altro codice.",
  "error.auth.deliveryUnknown":
    "OpenBot non ha potuto confermare se il codice di accesso è stato inviato. Controlla la consegna prima di inviarlo di nuovo.",
  "error.auth.adminKeyRequired": "Inserisci la chiave di amministrazione del servizio account.",
  "error.auth.adminKeyRejected": "Il servizio account non ha accettato la chiave di amministrazione.",
  "error.auth.sessionNotStorable":
    "OpenBot non può salvare la sessione di accesso su questo computer, quindi non può accedere con una chiave di amministrazione.",
  "error.auth.hostedServerNoAnswer": "Il server account non ha risposto all'inizio.",
  "error.auth.hostedServerNoClaim": "Il server ospitato non ha effettuato l'accesso e non ha credenziali di accesso.",
  "error.auth.hostedServerClaimMismatch": "La credenziale di accesso è per un altro server ospitato.",
  "error.auth.hostedServerHostMismatch":
    "L'host configurato non è il server ospitato per cui è stata creata questa VM.",
  "error.auth.hostedServerUnknown": "Il server account non ha nessuna registrazione di questo server ospitato.",
  "error.auth.invalidUser": "Il servizio account ha restituito un utente non valido.",
  "error.auth.invalidHostedServerClaim": "Credenziale di accesso del server ospitato non valida.",
  "error.auth.sessionNotStored": "Impossibile salvare la sessione.",
} as const satisfies PartialTranslation<typeof source>;
