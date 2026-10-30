import type { PartialTranslation } from "../../message";
import type { messages as source } from "../en/menu";

export const messages = {
  "menu.stopAllAgents": "Stop alle agents",
  "menu.checkForUpdates": "Controleer op updates…",
  "menu.preferences": "Instellingen…",
  "menu.actualSize": "Werkelijke grootte",
  "menu.zoomIn": "Zoom in",
  "menu.zoomOut": "Zoom uit",
  "menu.copyLink": "Kopieer link",
  "menu.trayOpen": "Open OpenBot",
  "menu.trayQuit": "Stop OpenBot",
  "menu.trayTooltip": "OpenBot",
} as const satisfies PartialTranslation<typeof source>;
