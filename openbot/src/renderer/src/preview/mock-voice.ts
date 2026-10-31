import {
  DEFAULT_SPEECH_SETTINGS,
  DEFAULT_VOICE_SETTINGS,
  ELEVENLABS_VOICE_MODEL_ID,
  type OpenBotDesktopApi,
  SPEECH_MODEL_ID,
  type SpeechModelStatus,
  type SpeechSettings,
  VOICE_MODEL_IDS,
  type VoiceModelId,
  type VoiceModelStatus,
  type VoiceSettings,
  type VoiceStreamTranscript,
} from "@openbot/contracts/ipc";
import { clone, type Listener, type MockRuntime } from "./mock-support";

export interface MockVoiceOptions {
  /** The models already downloaded, and ElevenLabs when its key is saved. Parakeet when not given. */
  voiceModelsReady?: readonly VoiceModelId[];
  voiceSettings?: VoiceSettings;
  /** True when the speech model is downloaded. */
  speechModelReady?: boolean;
  speechSettings?: SpeechSettings;
}

/** The sample rate of Supertonic 3. */
const MOCK_SPEECH_SAMPLE_RATE = 44_100;

/** The dictation models, with download progress and a streamed transcript the preview makes up. */
export function createMockVoice(options: MockVoiceOptions, { emit, schedule }: MockRuntime) {
  const ready = new Set(options.voiceModelsReady ?? ["parakeet-tdt-0.6b-v3"]);
  let settings: VoiceSettings = clone(options.voiceSettings ?? DEFAULT_VOICE_SETTINGS);
  const statuses = new Map<VoiceModelId, VoiceModelStatus>(
    VOICE_MODEL_IDS.map((modelId) => [
      modelId,
      ready.has(modelId)
        ? { modelId, phase: "ready", progress: 100, message: null }
        : { modelId, phase: "missing", progress: null, message: null },
    ]),
  );
  const listeners = new Set<Listener<VoiceModelStatus>>();
  const transcriptListeners = new Set<Listener<VoiceStreamTranscript>>();
  const transfers = new Map<VoiceModelId, { settle: (status: VoiceModelStatus) => void }>();
  let speechSettings: SpeechSettings = clone(options.speechSettings ?? DEFAULT_SPEECH_SETTINGS);
  let speechStatus: SpeechModelStatus = options.speechModelReady
    ? { modelId: SPEECH_MODEL_ID, phase: "ready", progress: 100, message: null }
    : { modelId: SPEECH_MODEL_ID, phase: "missing", progress: null, message: null };
  const speechListeners = new Set<Listener<SpeechModelStatus>>();
  let speechTransfer: { settle: (status: SpeechModelStatus) => void } | null = null;
  const setSpeechStatus = (next: SpeechModelStatus) => {
    speechStatus = next;
    emit(speechListeners, clone(next));
  };
  const downloadSpeech = () =>
    new Promise<SpeechModelStatus>((resolve) => {
      if (speechStatus.phase === "ready") return resolve(clone(speechStatus));
      const transfer = { settle: resolve };
      speechTransfer = transfer;
      const advance = (progress: number) => {
        if (speechTransfer !== transfer) return;
        if (progress >= 100) {
          speechTransfer = null;
          setSpeechStatus({ modelId: SPEECH_MODEL_ID, phase: "ready", progress: 100, message: null });
          return resolve(clone(speechStatus));
        }
        setSpeechStatus({ modelId: SPEECH_MODEL_ID, phase: "downloading", progress, message: null });
        schedule(() => advance(progress + 10), 200);
      };
      advance(0);
    });
  const status = (modelId: VoiceModelId): VoiceModelStatus =>
    clone(statuses.get(modelId) ?? { modelId, phase: "missing", progress: null, message: null });
  const setStatus = (next: VoiceModelStatus) => {
    statuses.set(next.modelId, next);
    emit(listeners, next);
  };

  const download = (modelId: VoiceModelId) =>
    new Promise<VoiceModelStatus>((resolve) => {
      const current = status(modelId);
      // The cloud model has nothing to download: its key makes it ready.
      if (current.phase === "ready" || modelId === ELEVENLABS_VOICE_MODEL_ID) return resolve(current);
      const transfer = { settle: resolve };
      transfers.set(modelId, transfer);
      setStatus({ modelId, phase: "downloading", progress: 0, message: null });
      const advance = (progress: number) => {
        if (transfers.get(modelId) !== transfer) return;
        if (progress >= 100) {
          transfers.delete(modelId);
          setStatus({ modelId, phase: "ready", progress: 100, message: null });
          return resolve(status(modelId));
        }
        setStatus({ modelId, phase: "downloading", progress, message: null });
        schedule(() => advance(progress + 5), 200);
      };
      schedule(() => advance(5), 200);
    });

  const voice: OpenBotDesktopApi["voice"] = {
    getModelStatus: async () => status(settings.modelId),
    getModelStatuses: async () => VOICE_MODEL_IDS.map(status),
    prepareModel: () => download(settings.modelId),
    downloadModel: ({ modelId }) => download(modelId),
    cancelModelDownload: async ({ modelId }) => {
      const transfer = transfers.get(modelId);
      if (!transfer) return status(modelId);
      transfers.delete(modelId);
      setStatus({ modelId, phase: "missing", progress: null, message: null });
      transfer.settle(status(modelId));
      return status(modelId);
    },
    removeModel: async ({ modelId }) => {
      if (modelId === ELEVENLABS_VOICE_MODEL_ID) throw new Error("A cloud model has no files to remove.");
      transfers.get(modelId)?.settle({ modelId, phase: "missing", progress: null, message: null });
      transfers.delete(modelId);
      setStatus({ modelId, phase: "missing", progress: null, message: null });
      return status(modelId);
    },
    getSettings: async () => clone(settings),
    setSettings: async (next) => {
      settings = clone(next);
      return clone(settings);
    },
    transcribe: async () => ({ text: "Mock voice transcript" }),
    setApiKey: async ({ key }) => {
      setStatus({
        modelId: ELEVENLABS_VOICE_MODEL_ID,
        phase: key === null ? "missing" : "ready",
        progress: key === null ? null : 100,
        message: null,
      });
      return status(ELEVENLABS_VOICE_MODEL_ID);
    },
    startStream: async () => ({ streamId: crypto.randomUUID() }),
    appendStream: async ({ streamId }) => emit(transcriptListeners, { streamId, text: "Mock voice transcript" }),
    finishStream: async () => ({ text: "Mock voice transcript" }),
    cancelStream: async () => {},
    onModelStatus: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    onStreamTranscript: (listener) => {
      transcriptListeners.add(listener);
      return () => transcriptListeners.delete(listener);
    },
    getSpeechSettings: async () => clone(speechSettings),
    setSpeechSettings: async (next) => {
      speechSettings = clone(next);
      return clone(speechSettings);
    },
    getSpeechModelStatus: async () => clone(speechStatus),
    downloadSpeechModel: downloadSpeech,
    cancelSpeechModelDownload: async () => {
      const transfer = speechTransfer;
      if (!transfer) return clone(speechStatus);
      speechTransfer = null;
      setSpeechStatus({ modelId: SPEECH_MODEL_ID, phase: "missing", progress: null, message: null });
      transfer.settle(clone(speechStatus));
      return clone(speechStatus);
    },
    removeSpeechModel: async () => {
      speechTransfer?.settle({ modelId: SPEECH_MODEL_ID, phase: "missing", progress: null, message: null });
      speechTransfer = null;
      setSpeechStatus({ modelId: SPEECH_MODEL_ID, phase: "missing", progress: null, message: null });
      return clone(speechStatus);
    },
    // Silence of about the length the text would take to read.
    speak: async ({ text }) => ({
      pcm: new Uint8Array(Math.min(10, Math.max(0.5, text.length / 15)) * MOCK_SPEECH_SAMPLE_RATE * 2),
      sampleRate: MOCK_SPEECH_SAMPLE_RATE,
    }),
    warmSpeech: async () => undefined,
    // The preview host keeps no files, and its messages carry no recording.
    keepRecording: async ({ audio }) => ({
      id: `mock-voice-recording-${audio.byteLength}`,
      name: "voice.webm",
      size: audio.byteLength,
      kind: "file",
      mimeType: "audio/webm",
      previewKind: "none",
      previewUrl: null,
    }),
    onSpeechModelStatus: (listener) => {
      speechListeners.add(listener);
      return () => speechListeners.delete(listener);
    },
  };
  return { voice };
}
