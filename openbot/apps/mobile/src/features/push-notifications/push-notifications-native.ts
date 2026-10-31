import type { PushNotificationsNative } from "./push-notifications-native.types";

/**
 * The app gets notifications on iOS and Android, where `push-notifications-native.ios.ts` and
 * `push-notifications-native.android.ts` replace this file. The tests do not load `expo-notifications`.
 */
export const pushNotificationsNative: PushNotificationsNative | null = null;
