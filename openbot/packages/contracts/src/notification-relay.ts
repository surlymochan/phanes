/** The sounds of the app bundle (`NOTIFICATION_SOUND_FILES`), and `default` for the system sound. */
export const NOTIFICATION_SOUNDS = ["openbot", "warm", "mech", "bubble", "press", "default"] as const;
export type NotificationSound = (typeof NOTIFICATION_SOUNDS)[number];

/**
 * One iOS notification that a host asks the OpenBot account service to send through Apple Push
 * Notification service. The host sealed the agent name and the text with keys that only the phone
 * knows, and the app opens them before iOS shows the notification. The service builds the Apple
 * payload itself, with the app name as the only text, so a host credential alone, without the keys of
 * the phone, cannot make a phone show text.
 */
export interface NotificationRelayPush {
  /** The Apple push token of the app, as hex. */
  token: string;
  /** Development builds receive notifications from the APNs sandbox. */
  environment: "production" | "development";
  /**
   * The account session of the phone on this host. The service sends only while the session is open,
   * so a phone that signed out or that the owner removed gets nothing more.
   */
  sessionId: string;
  /** The sealed notification, base64url. */
  sealed: string;
  /** The sound that iOS plays, or `null` for none. */
  sound: NotificationSound | null;
  /** When the host made the notification, in seconds. */
  timestamp: number;
}

/**
 * One Android notification that a host asks the OpenBot account service to send through Firebase
 * Cloud Messaging. The service sends a data message with the sealed bytes and the sound only, and the
 * app opens the seal before Android shows the notification. As for iOS, a host credential alone,
 * without the keys of the phone, cannot make a phone show text.
 */
export interface AndroidNotificationRelayPush {
  /**
   * The Firebase Cloud Messaging registration token of the app (`ANDROID_PUSH_TOKEN_PATTERN` of
   * `push-notifications-android-v1`).
   */
  token: string;
  /** The account session of the phone on this host, as for `NotificationRelayPush`. */
  sessionId: string;
  /** The sealed notification, base64url. */
  sealed: string;
  /** The sound that the app plays, or `null` for none. */
  sound: NotificationSound | null;
  /** When the host made the notification, in seconds. */
  timestamp: number;
}

/**
 * A sealed notification above this size would not fit the 4 KB Apple payload with its envelope.
 * Firebase Cloud Messaging data messages have the same 4 KB limit.
 */
export const NOTIFICATION_SEALED_LIMIT = 3_600;
export const NOTIFICATION_TOKEN_PATTERN = /^(?:[0-9a-f]{2}){8,256}$/u;
/**
 * The sound files in the app bundle, at the level of an alert. `openbot` is the sound of the desktop
 * app when a task finishes (`completion-sound.ts`), with the same sweep and envelope. The others are
 * the `success` cue of each desktop sound theme (`cuelume`; `warm` is its `default` theme), rendered
 * offline.
 */
export const NOTIFICATION_SOUND_FILES: Readonly<Record<Exclude<NotificationSound, "default">, string>> = {
  openbot: "openbot-task.wav",
  warm: "openbot-warm.wav",
  mech: "openbot-mech.wav",
  bubble: "openbot-bubble.wav",
  press: "openbot-press.wav",
};
