import type { VoltoDesktopApi } from "@volto/contracts/ipc";

/** What the Voice tab reaches in main: the dictation models and the dictation settings. */
export interface VoicePort {
  voice: VoltoDesktopApi["voice"];
}

/** Read on each call: tests and stories replace `window.volto` per case. */
export function voicePort(): VoicePort {
  return window.volto;
}
