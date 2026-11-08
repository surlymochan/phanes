import type { PartialTranslation } from "../../message";
import type { messages as source } from "../en/menu";

export const messages = {
  "menu.stopAllAgents": "Zatrzymaj wszystkich agentów",
  "menu.checkForUpdates": "Sprawdź aktualizacje…",
  "menu.preferences": "Ustawienia…",
  "menu.actualSize": "Rzeczywisty rozmiar",
  "menu.zoomIn": "Powiększ",
  "menu.zoomOut": "Pomniejsz",
  "menu.copyLink": "Kopiuj link",
  "menu.trayOpen": "Otwórz volto",
  "menu.trayQuit": "Zamknij volto",
  "menu.trayTooltip": "volto",
} as const satisfies PartialTranslation<typeof source>;
