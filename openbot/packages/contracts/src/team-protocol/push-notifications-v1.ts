// Frozen optional push-notifications-v1 wire contract.
//
// What it grants, recorded here because freezing it makes it permanent: a signed-in member's iPhone
// gives the host its Apple push token when it takes notifications, a secret that only the phone and
// the host know, and the events the member wants to hear about. While the phone says it is away, the
// host sends a notification for each such event of an agent this member can see, through the OpenBot
// relay and Apple. While the phone shows the Live Activity of this host, the host sends no
// notification, and the activity lights up the screen and plays the sound, unless the sound is
// "none"; a phone without a token gets only that. The host seals the agent name and the text with keys
// made from the secret, so the relay and Apple cannot read them. The relay checks that the session of the phone is still open on the
// account service before each send. The host keeps a registration for the session that sent it, also
// after it restarts, and forgets it when the phone removes it, when the member is removed or
// disabled, or when the relay or Apple refuses it. Widening any of it needs a second capability string.
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

export const PUSH_NOTIFICATIONS_CAPABILITY = "push-notifications-v1";

export const PUSH_NOTIFICATIONS_ROUTES = {
  register: "/v1/push-notifications/registration",
  remove: "/v1/push-notifications/registration/remove",
} as const;

/** Which agent events notify. "needs-me" keeps only the requests for input and approval. */
export type PushNotificationLevel = "all" | "needs-me";

/**
 * "openbot" is the sound that the desktop app plays when a task finishes; "warm", "mech", "bubble" and
 * "press" are the sounds of the desktop sound themes; "default" is the iOS sound.
 */
export const PUSH_NOTIFICATION_SOUNDS = ["openbot", "warm", "mech", "bubble", "press", "default", "none"] as const;
export type PushNotificationSound = (typeof PUSH_NOTIFICATION_SOUNDS)[number];

export interface PushNotificationRegistration {
  /** The phone's id for this host. The host puts it into the sealed notification for the tap. */
  serverId: string;
  /**
   * The Apple push token of the app, as hex. `null` when the phone takes no notifications: the
   * registration then gives only the events and the sound for the Live Activity.
   */
  token: string | null;
  /** Development builds receive notifications from the APNs sandbox. */
  environment: "production" | "development";
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
   * The sound of notifications and of the Live Activity of this host, which then also lights up the
   * screen. With "none", notifications are silent and the activity updates without an alert.
   */
  sound: PushNotificationSound;
  /**
   * When iOS started the Live Activity that this host updates, in seconds since 1970, or `null` when
   * the phone does not know or shows none. iOS stops its updates 8 hours later, also when the host
   * restarted and got the same token again.
   */
  liveActivityStartedAt: number | null;
}

const pattern =
  (expression: RegExp, maximum: number): AdminDecoder =>
  (value) => {
    const text = string(maximum)(value);
    if (typeof text !== "string" || !expression.test(text)) throw new Error("Invalid push notification value.");
    return text;
  };

const pushToken = pattern(/^(?:[0-9a-f]{2}){8,256}$/u, 512);
const secret = pattern(/^[A-Za-z0-9_-]{43}$/u, 43);

export const PUSH_NOTIFICATIONS_CODECS: ReadonlyMap<string, OptionalRouteCodec> = new Map([
  [
    PUSH_NOTIFICATIONS_ROUTES.register,
    adminRoute(
      fields({
        serverId: identifier,
        token: nullable(pushToken),
        environment: oneOf("production", "development"),
        secret,
        locale: string(35),
        away: boolean,
        level: oneOf("all", "needs-me"),
        mutedUntil: nullable(count),
        preview: boolean,
        sound: oneOf(...PUSH_NOTIFICATION_SOUNDS),
        liveActivityStartedAt: nullable(count),
      }),
      empty,
    ),
  ],
  [PUSH_NOTIFICATIONS_ROUTES.remove, adminRoute(empty, empty)],
]);
