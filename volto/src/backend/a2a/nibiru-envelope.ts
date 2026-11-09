/** The supported, bounded Nibiru task envelope (version 1). */
export interface NibiruTaskEnvelope {
  version: 1;
  taskId: string;
  sender: string;
  recipient: string;
  body: string;
  idempotencyKey: string;
}

/** Stub: the decoder is implemented in the next commit. */
export function decodeNibiruTaskEnvelope(_input: unknown): NibiruTaskEnvelope {
  throw new Error("not implemented");
}
