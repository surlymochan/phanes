import {
  type AndroidNotificationRelayPush,
  NOTIFICATION_SEALED_LIMIT,
  NOTIFICATION_SOUNDS,
  type NotificationSound,
} from "@openbot/contracts/notification-relay";
import { type DynamicRecord, isDynamicRecord, isString } from "@openbot/contracts/runtime-values";
import { ANDROID_PUSH_TOKEN_PATTERN } from "@openbot/contracts/team-protocol/push-notifications-android-v1";
import { Effect, Schema } from "effect";
import { importPKCS8, SignJWT } from "jose";

/**
 * Forwards a sealed Android notification from a host to Firebase Cloud Messaging. The host sealed the
 * content with keys that only the phone has, so this service cannot read it. It stores nothing and
 * logs nothing: the token, the host and the bytes live only for the request.
 *
 * This service builds the Firebase message itself: a data message with the sealed bytes and the sound
 * only, never text. The app opens the seal before Android shows the notification, and a seal that
 * the app cannot open shows the app name only. So a host credential alone, without the keys of the
 * phone, cannot make it show text.
 */

/** The Firebase service account key that may send Cloud Messaging messages for the app. */
export interface FcmConfig {
  projectId: string;
  clientEmail: string;
  /** The PKCS #8 private key of the service account, as PEM text. */
  privateKey: string;
  privateKeyId: string | null;
}

export type FcmRelayResult = "sent" | "gone" | "rejected" | "unavailable";

const TOKEN_URL = "https://oauth2.googleapis.com/token";
const MESSAGING_SCOPE = "https://www.googleapis.com/auth/firebase.messaging";
/** Google gives an access token for one hour. A new one is made 5 minutes before it ends. */
const ASSERTION_LIFETIME_SECONDS = 60 * 60;
const TOKEN_MARGIN_SECONDS = 5 * 60;
/** Firebase keeps a notification this long for a phone that is off, as Apple does. */
const NOTIFICATION_LIFETIME = "86400s";
const REQUEST_TIMEOUT_MS = 5_000;
/** Firebase answers these for a token that no longer reaches the app. */
const GONE_ERRORS = new Set(["UNREGISTERED", "SENDER_ID_MISMATCH"]);

type Fetch = (input: string, init: RequestInit) => Promise<Response>;

class FcmProviderError extends Schema.TaggedError<FcmProviderError>()("FcmProviderError", {}) {}

const AccessToken = Schema.Struct({ access_token: Schema.String, expires_in: Schema.Number });
const FcmErrorBody = Schema.Struct({
  error: Schema.Struct({
    details: Schema.optional(Schema.Array(Schema.Struct({ errorCode: Schema.optional(Schema.String) }))),
  }),
});

export class FcmRelaySender {
  readonly #config: FcmConfig;
  readonly #fetch: Fetch;
  readonly #now: () => number;
  #key: Awaited<ReturnType<typeof importPKCS8>> | null = null;
  #token: { value: string; expiresAt: number } | null = null;

  constructor(config: FcmConfig, fetch: Fetch, now: () => number = Date.now) {
    this.#config = config;
    this.#fetch = fetch;
    this.#now = now;
  }

  readonly sendNotification = Effect.fn("FcmRelaySender.sendNotification")(function* (
    this: FcmRelaySender,
    push: AndroidNotificationRelayPush,
  ): Effect.fn.Return<FcmRelayResult> {
    const token = yield* this.#accessToken().pipe(Effect.catch(() => Effect.succeed(null)));
    if (!token) return "unavailable";
    const response = yield* Effect.tryPromise({
      try: (signal) =>
        this.#fetch(
          `https://fcm.googleapis.com/v1/projects/${encodeURIComponent(this.#config.projectId)}/messages:send`,
          {
            method: "POST",
            headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
            body: JSON.stringify(notificationMessage(push)),
            signal: AbortSignal.any([signal, AbortSignal.timeout(REQUEST_TIMEOUT_MS)]),
          },
        ),
      catch: () => new FcmProviderError({}),
    }).pipe(Effect.catch(() => Effect.succeed(null)));
    if (!response) return "unavailable";
    if (response.ok) return "sent";
    // Google refused the access token, as after a key change. The next send makes a new one.
    if (response.status === 401) {
      this.#token = null;
      return "unavailable";
    }
    if (response.status === 429 || response.status >= 500) return "unavailable";
    const errors = yield* Effect.tryPromise({
      try: () => response.json(),
      catch: () => new FcmProviderError({}),
    }).pipe(
      Effect.flatMap(Schema.decodeUnknownEffect(FcmErrorBody)),
      Effect.map((value) =>
        (value.error.details ?? []).flatMap((detail) => (detail.errorCode ? [detail.errorCode] : [])),
      ),
      Effect.catch(() => Effect.succeed<readonly string[]>([])),
    );
    return response.status === 404 || errors.some((error) => GONE_ERRORS.has(error)) ? "gone" : "rejected";
  }).bind(this);

  /** Only resolved values cross Worker requests; pending I/O remains with its invocation. */
  readonly #accessToken = Effect.fn("FcmRelaySender.accessToken")(function* (
    this: FcmRelaySender,
  ): Effect.fn.Return<string, FcmProviderError> {
    const now = Math.floor(this.#now() / 1000);
    if (this.#token && now < this.#token.expiresAt - TOKEN_MARGIN_SECONDS) return this.#token.value;
    this.#key ??= yield* Effect.tryPromise({
      try: () => importPKCS8(this.#config.privateKey, "RS256"),
      catch: () => new FcmProviderError({}),
    });
    const key = this.#key;
    const { clientEmail, privateKeyId } = this.#config;
    const assertion = yield* Effect.tryPromise({
      try: () =>
        new SignJWT({ scope: MESSAGING_SCOPE })
          .setProtectedHeader({ alg: "RS256", typ: "JWT", ...(privateKeyId ? { kid: privateKeyId } : {}) })
          .setIssuer(clientEmail)
          .setAudience(TOKEN_URL)
          .setIssuedAt(now)
          .setExpirationTime(now + ASSERTION_LIFETIME_SECONDS)
          .sign(key),
      catch: () => new FcmProviderError({}),
    });
    const response = yield* Effect.tryPromise({
      try: (signal) =>
        this.#fetch(TOKEN_URL, {
          method: "POST",
          headers: { "content-type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({
            grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
            assertion,
          }).toString(),
          signal: AbortSignal.any([signal, AbortSignal.timeout(REQUEST_TIMEOUT_MS)]),
        }),
      catch: () => new FcmProviderError({}),
    });
    if (!response.ok) return yield* new FcmProviderError({});
    const value = yield* Effect.tryPromise({
      try: () => response.json(),
      catch: () => new FcmProviderError({}),
    }).pipe(
      Effect.flatMap(Schema.decodeUnknownEffect(AccessToken)),
      Effect.mapError(() => new FcmProviderError({})),
    );
    this.#token = { value: value.access_token, expiresAt: now + Math.floor(value.expires_in) };
    return value.access_token;
  });
}

/**
 * A data message, so that Android gives it to the app also when the app does not run. The app opens
 * `sealed` (`OpenBotMessagingService.kt` in the mobile app) and plays `sound`. High priority lets
 * the app run at once also when the phone saves power.
 */
function notificationMessage(push: AndroidNotificationRelayPush) {
  return {
    message: {
      token: push.token,
      data: { sealed: push.sealed, ...(push.sound === null ? {} : { sound: push.sound }) },
      android: { priority: "HIGH", ttl: NOTIFICATION_LIFETIME },
    },
  };
}

/** Reads one notification from a host request body, or returns `null` when it is not valid. */
export function readAndroidNotificationRelayPush(body: DynamicRecord): AndroidNotificationRelayPush | null {
  const { token, sessionId, sealed, sound, timestamp } = body;
  if (!isString(token) || !ANDROID_PUSH_TOKEN_PATTERN.test(token)) return null;
  if (!isString(sessionId) || !sessionId || sessionId.length > 128) return null;
  if (!isString(sealed) || !sealed || sealed.length > NOTIFICATION_SEALED_LIMIT || !/^[A-Za-z0-9_-]+$/u.test(sealed)) {
    return null;
  }
  if (!isSound(sound)) return null;
  if (typeof timestamp !== "number" || !Number.isSafeInteger(timestamp) || timestamp <= 0) return null;
  return { token, sessionId, sealed, sound, timestamp };
}

function isSound(value: unknown): value is NotificationSound | null {
  return value === null || NOTIFICATION_SOUNDS.some((sound) => sound === value);
}

/** Reads the service account key file that Firebase gives, or returns `null` when it is not one. */
export function readFcmServiceAccount(value: string): FcmConfig | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    return null;
  }
  if (!isDynamicRecord(parsed) || parsed.type !== "service_account") return null;
  const { project_id: projectId, client_email: clientEmail, private_key: privateKey, private_key_id: keyId } = parsed;
  if (!isString(projectId) || !projectId || !isString(clientEmail) || !clientEmail) return null;
  if (!isString(privateKey) || !privateKey.includes("PRIVATE KEY")) return null;
  return { projectId, clientEmail, privateKey, privateKeyId: isString(keyId) && keyId ? keyId : null };
}

let shared: { serviceAccount: string; sender: FcmRelaySender } | null = null;

/** One sender for each key, so the key import and the access token are reused. `null` for a bad key. */
export function sharedFcmSender(serviceAccount: string): FcmRelaySender | null {
  if (shared?.serviceAccount === serviceAccount) return shared.sender;
  const config = readFcmServiceAccount(serviceAccount);
  if (!config) return null;
  const sender = new FcmRelaySender(config, (input, init) => fetch(input, init));
  shared = { serviceAccount, sender };
  return sender;
}
