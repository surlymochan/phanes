import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/error/auth";

export const messages = {
  "error.auth.serviceUnavailable":
    "OpenBot no pudo acceder al servicio de cuentas. Comprueba que la API esté en funcionamiento e inténtalo de nuevo.",
  "error.auth.networkBlocked":
    "Un cortafuegos o proxy de esta red impidió que OpenBot accediera a {host}. Pide al administrador de la red que permita {host} e inténtalo de nuevo.",
  "error.auth.signInFirst": "Primero inicia sesión en OpenBot.",
  "error.auth.signInRequired": "Es necesario iniciar sesión.",
  "error.auth.accountChangedDuringRegister":
    "La cuenta con sesión iniciada cambió mientras se registraba este servidor.",
  "error.auth.hostCredentialUnavailable":
    "La credencial del host remoto no está disponible. Registra el host de nuevo.",
  "error.auth.codeNotVerified": "No se pudo verificar el código de inicio de sesión.",
  "error.auth.serviceError": "El servicio de cuentas devolvió un error.",
  "error.auth.serviceUnreachable":
    "OpenBot no pudo acceder al servicio de cuentas. Comprueba tu conexión e inténtalo de nuevo.",
  "error.auth.serviceTimeout": "El servicio de cuentas no respondió a tiempo. Inténtalo de nuevo.",
  "error.auth.serviceStatus": "El servicio de cuentas devolvió un error ({status}). Inténtalo de nuevo más tarde.",
  "error.auth.invalidHostedServer": "El servicio de cuentas devolvió un servidor alojado no válido.",
  "error.auth.codeNotSent": "OpenBot no pudo enviar el código de inicio de sesión.",
  "error.auth.deliveryTimeout":
    "OpenBot no pudo confirmar la entrega a tiempo. El código aún puede llegar; comprueba la entrega antes de enviarlo de nuevo.",
  "error.auth.deliveryInterrupted":
    "La conexión terminó antes de que OpenBot confirmara la entrega. Comprueba la entrega para evitar enviar otro código.",
  "error.auth.deliveryUnknown":
    "OpenBot no pudo confirmar si se envió el código de inicio de sesión. Comprueba la entrega antes de enviarlo de nuevo.",
  "error.auth.adminKeyRequired": "Introduce la clave de administración del servicio de cuentas.",
  "error.auth.adminKeyRejected": "El servicio de cuentas no aceptó la clave de administración.",
  "error.auth.sessionNotStorable":
    "OpenBot no puede guardar la sesión de inicio de sesión en este equipo, así que no puede iniciar sesión con una clave de administración.",
  "error.auth.hostedServerNoAnswer": "El servidor de cuentas no respondió al principio.",
  "error.auth.hostedServerNoClaim": "El servidor alojado no ha iniciado sesión y no tiene credencial de acceso.",
  "error.auth.hostedServerClaimMismatch": "La credencial de acceso es para otro servidor alojado.",
  "error.auth.hostedServerHostMismatch": "El host configurado no es el servidor alojado para el que se creó esta VM.",
  "error.auth.hostedServerUnknown": "El servidor de cuentas no tiene ningún registro de este servidor alojado.",
  "error.auth.invalidUser": "El servicio de cuentas devolvió un usuario no válido.",
  "error.auth.invalidHostedServerClaim": "Credencial de acceso no válida del servidor alojado.",
  "error.auth.sessionNotStored": "No se pudo guardar la sesión.",
} as const satisfies PartialTranslation<typeof source>;
