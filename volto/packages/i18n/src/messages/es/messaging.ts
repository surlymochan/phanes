import type { PartialTranslation } from "../../message";
import type { messages as source } from "../en/messaging";

export const messages = {
  "messaging.help.invalid_token":
    "Slack ya no acepta volto en este espacio de trabajo. Puede que se haya desinstalado. Conecta el espacio de trabajo de nuevo.",
  "messaging.help.secret_storage_unavailable":
    "volto no puede leer los tokens guardados en este equipo. Desconecta el espacio de trabajo y vuelve a conectarlo.",
  "messaging.help.relay_unavailable":
    "volto no puede recibir eventos de Slack en este equipo. Inicia sesión, asigna un nombre a este equipo en los ajustes del servidor y mantén volto abierto.",
  "messaging.discordHelp.invalid_token":
    "Discord ya no acepta volto en este servidor de Discord. Puede que se haya eliminado. Conecta el servidor de Discord de nuevo.",
  "messaging.discordHelp.secret_storage_unavailable":
    "volto no puede leer los tokens guardados en este equipo. Desconecta el servidor de Discord y vuelve a conectarlo.",
  "messaging.discordHelp.relay_unavailable":
    "volto no puede recibir eventos de Discord en este equipo. Inicia sesión, asigna un nombre a este equipo en los ajustes del servidor y mantén volto abierto.",
} as const satisfies PartialTranslation<typeof source>;
