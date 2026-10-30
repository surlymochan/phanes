import type { OpenBotDesktopApi } from "@openbot/contracts/ipc";

/** What the Voice tab reaches in main: the dictation models and the dictation settings. */
export interface VoicePort {
  voice: OpenBotDesktopApi["voice"];
}

/** Read on each call: tests and stories replace `window.openbot` per case. */
export function voicePort(): VoicePort {
  return window.openbot;
}
