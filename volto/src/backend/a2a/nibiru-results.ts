export type NibiruTerminalStatus = "completed" | "failed" | "interrupted";

export interface NibiruTerminalResult {
  idempotencyKey: string;
  taskId: string;
  terminal: NibiruTerminalStatus;
  output?: string;
  /** Provider diagnostics are deliberately never forwarded to Nibiru. */
  providerError?: string;
}

export interface NibiruResultSenderOptions {
  endpoint: string;
  token?: string;
  /** Number of retries after the initial request. Defaults to three. */
  maxRetries?: number;
  retryDelayMs?: number;
  fetch?: typeof globalThis.fetch;
  log?: (message: string) => void;
}

export interface NibiruResultSender {
  sendTerminalResult(result: NibiruTerminalResult): Promise<void>;
}

const RETRYABLE_STATUS = new Set([408, 425, 429]);

function isRetryableStatus(status: number): boolean {
  return RETRYABLE_STATUS.has(status) || status >= 500;
}

function pause(milliseconds: number): Promise<void> {
  return milliseconds <= 0 ? Promise.resolve() : new Promise((resolve) => setTimeout(resolve, milliseconds));
}

/** Sends terminal A2A results and remembers acknowledgements for this sender's lifetime. */
export function createNibiruResultSender(options: NibiruResultSenderOptions): NibiruResultSender {
  const endpoint = new URL(options.endpoint);
  if (endpoint.protocol !== "http:" && endpoint.protocol !== "https:") {
    throw new Error("Nibiru result endpoint must use HTTP or HTTPS.");
  }

  const maxRetries = options.maxRetries ?? 3;
  const retryDelayMs = options.retryDelayMs ?? 250;
  if (!Number.isInteger(maxRetries) || maxRetries < 0) throw new Error("maxRetries must be a non-negative integer.");
  if (!Number.isFinite(retryDelayMs) || retryDelayMs < 0) throw new Error("retryDelayMs must be non-negative.");

  const request = options.fetch ?? globalThis.fetch;
  const log = options.log ?? (() => undefined);
  const confirmed = new Set<string>();
  const inFlight = new Map<string, Promise<void>>();

  async function deliver(result: NibiruTerminalResult): Promise<void> {
    const payload = JSON.stringify({
      taskId: result.taskId,
      terminal: result.terminal,
      ...(result.output === undefined ? {} : { output: result.output }),
    });
    const headers: Record<string, string> = {
      "content-type": "application/json",
      "idempotency-key": result.idempotencyKey,
    };
    if (options.token) headers.authorization = `Bearer ${options.token}`;

    for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
      let response: Response;
      try {
        response = await request(endpoint, { method: "POST", headers, body: payload });
      } catch {
        if (attempt === maxRetries) {
          log(`Nibiru result delivery failed after ${attempt + 1} attempts (network error).`);
          throw new Error("Nibiru result delivery failed after retries.");
        }
        log(`Nibiru result delivery network error; retrying (${attempt + 1}/${maxRetries + 1}).`);
        await pause(retryDelayMs * 2 ** attempt);
        continue;
      }

      if (response.ok) {
        confirmed.add(result.idempotencyKey);
        log(`Nibiru result delivery confirmed (${result.terminal}).`);
        return;
      }

      if (!isRetryableStatus(response.status) || attempt === maxRetries) {
        log(`Nibiru result delivery failed (HTTP ${response.status}).`);
        throw new Error(`Nibiru result delivery failed with HTTP ${response.status}.`);
      }
      log(`Nibiru result delivery received HTTP ${response.status}; retrying (${attempt + 1}/${maxRetries + 1}).`);
      await pause(retryDelayMs * 2 ** attempt);
    }
  }

  return {
    sendTerminalResult(result) {
      if (!result.idempotencyKey) return Promise.reject(new Error("idempotencyKey is required."));
      if (!result.taskId) return Promise.reject(new Error("taskId is required."));
      if (!["completed", "failed", "interrupted"].includes(result.terminal)) {
        return Promise.reject(new Error("Invalid Nibiru terminal status."));
      }
      if (confirmed.has(result.idempotencyKey)) return Promise.resolve();

      const pending = inFlight.get(result.idempotencyKey);
      if (pending) return pending;

      const delivery = deliver(result).finally(() => inFlight.delete(result.idempotencyKey));
      inFlight.set(result.idempotencyKey, delivery);
      return delivery;
    },
  };
}
