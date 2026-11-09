/** The supported, bounded Nibiru task envelope (version 1). */
export interface NibiruTaskEnvelope {
  version: 1;
  taskId: string;
  sender: string;
  recipient: string;
  body: string;
  idempotencyKey: string;
}

const fieldLimits = {
  taskId: 256,
  sender: 256,
  recipient: 256,
  body: 65536,
  idempotencyKey: 256,
} as const;

/** Decode an untrusted value once, before passing it to internal services. */
export function decodeNibiruTaskEnvelope(input: unknown): NibiruTaskEnvelope {
  if (input === null || typeof input !== "object" || Array.isArray(input)) {
    throw new Error("Invalid Nibiru task envelope");
  }
  const prototype = Object.getPrototypeOf(input);
  if (prototype !== Object.prototype && prototype !== null) {
    throw new Error("Invalid Nibiru task envelope");
  }
  const fields = new Map(Object.entries(input));
  const keys = Reflect.ownKeys(input);
  if (
    keys.length !== 6 ||
    keys.some((key) => typeof key !== "string" || (key !== "version" && !Object.hasOwn(fieldLimits, key)))
  ) {
    throw new Error("Invalid Nibiru task envelope fields");
  }
  if (fields.get("version") !== 1) throw new Error("Unsupported Nibiru task envelope version");

  function field(name: keyof typeof fieldLimits): string {
    const value = fields.get(name);
    if (typeof value !== "string" || value.trim().length === 0 || value.length > fieldLimits[name]) {
      throw new Error(`Invalid Nibiru task envelope ${name}`);
    }
    return value;
  }
  return {
    version: 1,
    taskId: field("taskId"),
    sender: field("sender"),
    recipient: field("recipient"),
    body: field("body"),
    idempotencyKey: field("idempotencyKey"),
  };
}
