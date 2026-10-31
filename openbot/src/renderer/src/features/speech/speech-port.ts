import type { OpenBotDesktopApi } from "@openbot/contracts/ipc";

/**
 * What spoken replies reach in main: the speech model and settings, the synthesis, the finished
 * answer, and the recording a voice turn keeps with its message.
 */
export interface SpeechPort {
  voice: Pick<
    OpenBotDesktopApi["voice"],
    | "getSpeechSettings"
    | "getSpeechModelStatus"
    | "downloadSpeechModel"
    | "onSpeechModelStatus"
    | "speak"
    | "warmSpeech"
    | "keepRecording"
  >;
  agent: Pick<OpenBotDesktopApi["agent"], "readConversationPage" | "discardDraftAttachment">;
}

/** Read on each call: tests and stories replace `window.openbot` per case. */
export function speechPort(): SpeechPort {
  return window.openbot;
}
