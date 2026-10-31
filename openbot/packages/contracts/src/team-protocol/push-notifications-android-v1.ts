// Frozen optional push-notifications-android-v1 wire contract.
//
// What it grants, recorded here because freezing it makes it permanent: a signed-in member's Android
// phone gives the host its Firebase Cloud Messaging token when it takes notifications, a secret that
// only the phone and the host know, and the events the member wants to hear about. While the phone
// says it is away, the host sends a notification for each such event of an agent this member can see,
// through the OpenBot relay and Firebase Cloud Messaging, as `push-notifications-v1` does for an
// iPhone. The host seals the agent name and the text with keys made from the secret, so the relay and
// Google cannot read them. The relay checks that the session of the phone is still open on the account
// service before each send. An Android phone has no Live Activity, so a registration always has a
// token. The host keeps a registration for the session that sent it, also after it restarts, and
// forgets it when the phone removes it, when the member is removed or disabled, or when the relay or
// Google refuses it. Widening any of it needs a second capability string.
import {
  type AdminDecoder,
  adminRoute,
  boolean,
  count,
  empty,
  fields,
  identifier,
  nullable,
  type OptionalRouteCodec,
  oneOf,
  string,
} from "./admin-wire";
import {
  PUSH_NOTIFICATION_SOUNDS,
  type PushNotificationLevel,
  type PushNotificationSound,
} from "./push-notifications-v1";

export const ANDROID_PUSH_NOTIFICATIONS_CAPABILITY = "push-notifications-android-v1";

export const ANDROID_PUSH_NOTIFICATIONS_ROUTES = {
  register: "/v1/push-notifications/android-registration",
  remove: "/v1/push-notifications/android-registration/remove",
} as const;

export interface AndroidPushNotificationRegistration {
  /** The phone's id for this host. The host puts it into the sealed notification for the tap. */
  serverId: string;
  /** The Firebase Cloud Messaging registration token of the app. */
  token: string;
  /** 32 random bytes as base64url. */
  secret: string;
  /** The interface language of the phone. The host uses English for a language it does not have. */
  locale: string;
  /** `true` while the app is not on screen. Only then does the host send notifications. */
  away: boolean;
  level: PushNotificationLevel;
  /** No notification before this time, in seconds since 1970, or `null`. */
  mutedUntil: number | null;
  /** Whether the notification shows the start of a reply or a question, or only what happened. */
  preview: boolean;
  /**
   * The sound of notifications. "default" is the Android notification sound, and with "none"
   * notifications are silent.
   */
  sound: PushNotificationSound;
}

/** Firebase gives tokens of letters, digits, `-`, `_` and `:`. The limit leaves room for a longer format. */
export const ANDROID_PUSH_TOKEN_PATTERN = /^[A-Za-z0-9_:-]{32,4096}$/u;

const pattern =
  (expression: RegExp, maximum: number): AdminDecoder =>
  (value) => {
    const text = string(maximum)(value);
    if (typeof text !== "string" || !expression.test(text)) throw new Error("Invalid push notification value.");
    return text;
  };

const pushToken = pattern(ANDROID_PUSH_TOKEN_PATTERN, 4096);
const secret = pattern(/^[A-Za-z0-9_-]{43}$/u, 43);

export const ANDROID_PUSH_NOTIFICATIONS_CODECS: ReadonlyMap<string, OptionalRouteCodec> = new Map([
  [
    ANDROID_PUSH_NOTIFICATIONS_ROUTES.register,
    adminRoute(
      fields({
        serverId: identifier,
        token: pushToken,
        secret,
        locale: string(35),
        away: boolean,
        level: oneOf("all", "needs-me"),
        mutedUntil: nullable(count),
        preview: boolean,
        sound: oneOf(...PUSH_NOTIFICATION_SOUNDS),
      }),
      empty,
    ),
  ],
  [ANDROID_PUSH_NOTIFICATIONS_ROUTES.remove, adminRoute(empty, empty)],
]);
