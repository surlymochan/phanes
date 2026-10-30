import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/error/auth";

export const messages = {
  // Errors from the OpenBot account service.
  "error.auth.serviceUnavailable":
    "OpenBot n’a pas pu joindre le service de compte. Vérifiez que l’API fonctionne, puis réessayez.",
  "error.auth.networkBlocked":
    "Un pare-feu ou un proxy de ce réseau a empêché OpenBot de joindre {host}. Demandez à votre administrateur réseau d’autoriser {host}, puis réessayez.",
  "error.auth.signInFirst": "Connectez-vous d’abord à OpenBot.",
  "error.auth.signInRequired": "Une connexion est requise.",
  "error.auth.accountChangedDuringRegister": "Le compte connecté a changé pendant l’enregistrement de ce serveur.",
  "error.auth.hostCredentialUnavailable":
    "L’identifiant de l’hôte distant n’est pas disponible. Enregistrez à nouveau l’hôte.",
  "error.auth.codeNotVerified": "Impossible de vérifier le code de connexion.",
  "error.auth.serviceError": "Le service de compte a renvoyé une erreur.",
  "error.auth.serviceUnreachable":
    "OpenBot n’a pas pu joindre le service de compte. Vérifiez votre connexion, puis réessayez.",
  "error.auth.serviceTimeout": "Le service de compte n’a pas répondu à temps. Réessayez.",
  "error.auth.serviceStatus": "Le service de compte a renvoyé une erreur ({status}). Réessayez plus tard.",
  "error.auth.invalidHostedServer": "Le service de compte a renvoyé un serveur hébergé invalide.",
  "error.auth.codeNotSent": "OpenBot n’a pas pu envoyer le code de connexion.",
  "error.auth.deliveryTimeout":
    "OpenBot n’a pas pu confirmer l’envoi à temps. Le code peut encore arriver ; vérifiez sa réception avant de le renvoyer.",
  "error.auth.deliveryInterrupted":
    "La connexion s’est terminée avant qu’OpenBot confirme l’envoi. Vérifiez la réception pour ne pas envoyer un autre code.",
  "error.auth.deliveryUnknown":
    "OpenBot n’a pas pu confirmer si le code de connexion a été envoyé. Vérifiez sa réception avant de le renvoyer.",
  "error.auth.adminKeyRequired": "Saisissez la clé d’administration du service de compte.",
  "error.auth.adminKeyRejected": "Le service de compte n’a pas accepté la clé d’administration.",
  "error.auth.sessionNotStorable":
    "OpenBot ne peut pas enregistrer la session de connexion sur cet ordinateur, il ne peut donc pas se connecter avec une clé d’administration.",
  "error.auth.hostedServerNoAnswer": "Le serveur de compte n’a pas répondu au début.",
  "error.auth.hostedServerNoClaim": "Le serveur hébergé est déconnecté et n’a aucun justificatif d’accès.",
  "error.auth.hostedServerClaimMismatch": "Le justificatif d’accès concerne un autre serveur hébergé.",
  "error.auth.hostedServerHostMismatch":
    "L’hôte configuré n’est pas le serveur hébergé pour lequel cette VM a été créée.",
  "error.auth.hostedServerUnknown": "Le serveur de compte n’a aucun enregistrement de ce serveur hébergé.",
  "error.auth.invalidUser": "Le service de compte a renvoyé un utilisateur invalide.",
  "error.auth.invalidHostedServerClaim": "Justificatif d’accès de serveur hébergé non valide.",
  "error.auth.sessionNotStored": "La session n’a pas pu être enregistrée.",
} as const satisfies PartialTranslation<typeof source>;
