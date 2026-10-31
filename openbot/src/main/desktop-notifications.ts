import type { AgentEvent, ServerSummary } from "@openbot/contracts/ipc";
import { agentEventCanNotify, agentEventNeedsUser } from "@openbot/team-client/agent-notifications";
import { Effect } from "effect";
import type { Notification } from "electron";
import type { NotificationPreferenceStore } from "./notification-preference-store";

/**
 * Whether the desktop notifies about a server's event while its window does not have focus. The
 * owner's phone stays quiet only for an event that this returns true for, so each event reaches one
 * device. The agent's own switch applies after this, on both.
 */
export function desktopShowsAgentEvent(
  event: AgentEvent,
  server: Pick<ServerSummary, "notificationsMuted" | "notificationLevel"> | undefined,
  enabled: boolean,
): boolean {
  if (!enabled || !server || server.notificationsMuted || server.notificationLevel === "nothing") return false;
  if (event.type === "usage-limit-reached") return true;
  if (!agentEventCanNotify(event)) return false;
  return server.notificationLevel === "all" || agentEventNeedsUser(event);
}

// Enough for every agent that can wait for the user at once, and small enough that notifications the
// operating system never reports as closed do not add up.
export const RETAINED_NOTIFICATION_LIMIT = 50;
const retained = new Set<Notification>();

/**
 * Shows a notification and keeps a reference to it. Electron removes a notification from the
 * Notification Center when its object is garbage collected, and the click on it is then lost.
 */
export function showRetainedNotification(notification: Notification): void {
  const release = () => retained.delete(notification);
  notification.on("click", release);
  notification.on("close", release);
  notification.on("failed", release);
  retained.add(notification);
  for (const oldest of retained) {
    if (retained.size <= RETAINED_NOTIFICATION_LIMIT) break;
    retained.delete(oldest);
  }
  notification.show();
}

/** Removes a shown notification from the Notification Center, because nothing waits for it any more. */
export function closeRetainedNotification(notification: Notification): void {
  retained.delete(notification);
  notification.close();
}

export interface NotificationPermissionRequest {
  platform: NodeJS.Platform;
  preference: Pick<NotificationPreferenceStore, "get" | "permissionRequested" | "markPermissionRequested">;
  showWelcome: () => void;
}

/**
 * macOS asks the user to allow notifications only when an app first shows one, and Electron has no
 * call that asks without showing. So OpenBot shows one welcome notification the first time it
 * starts, rather than let the question interrupt the first agent that needs the user.
 *
 * The request is recorded before the notification is shown: a failed write then skips the welcome,
 * where the other order would show it again on every start.
 */
export const requestNotificationPermission = Effect.fn("requestNotificationPermission")(function* ({
  platform,
  preference,
  showWelcome,
}: NotificationPermissionRequest) {
  if (platform !== "darwin" || preference.permissionRequested() || !preference.get().desktopNotifications) return;
  yield* preference.markPermissionRequested();
  showWelcome();
});

const NOTIFICATION_SETTINGS_URLS: Partial<Record<NodeJS.Platform, string>> = {
  darwin: "x-apple.systempreferences:com.apple.Notifications-Settings.extension",
  win32: "ms-settings:notifications",
};

/** The operating system page where the user allows OpenBot notifications, or null where none exists. */
export function notificationSettingsUrl(platform: NodeJS.Platform): string | null {
  return NOTIFICATION_SETTINGS_URLS[platform] ?? null;
}
