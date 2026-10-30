import type { PartialTranslation } from "../../message";
import type { messages as source } from "../en/menu";

export const messages = {
  "menu.stopAllAgents": "모든 에이전트 중지",
  "menu.checkForUpdates": "업데이트 확인…",
  "menu.preferences": "설정…",
  "menu.actualSize": "실제 크기",
  "menu.zoomIn": "확대",
  "menu.zoomOut": "축소",
  "menu.copyLink": "링크 복사",
  "menu.trayOpen": "OpenBot 열기",
  "menu.trayQuit": "OpenBot 종료",
  "menu.trayTooltip": "OpenBot",
} as const satisfies PartialTranslation<typeof source>;
