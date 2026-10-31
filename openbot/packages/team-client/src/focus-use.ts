import { FOCUS_USE_INTERVAL_MS } from "@openbot/contracts/team-protocol/focus-use-v1";

/**
 * Tells a hosted server that its app has focus (`focus-use-v1`): at once when the server becomes the
 * target, then each interval. The caller sets the server only while the app has focus, the server is
 * online and it advertises the capability, and null otherwise. A ping sent less than one interval ago
 * still counts on the host, so a quick change of focus sends no extra ping. `send` drops its own errors.
 */
export function createFocusUsePing(send: (serverId: string) => void, now = Date.now) {
  let target: string | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const sentAt = new Map<string, number>();

  function schedule() {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    const serverId = target;
    if (serverId === null) return;
    const last = sentAt.get(serverId);
    const delay = last === undefined ? 0 : Math.max(0, last + FOCUS_USE_INTERVAL_MS - now());
    timer = setTimeout(() => {
      timer = null;
      sentAt.set(serverId, now());
      send(serverId);
      schedule();
    }, delay);
  }

  return {
    setTarget(serverId: string | null) {
      if (serverId === target) return;
      target = serverId;
      schedule();
    },
    dispose() {
      target = null;
      schedule();
    },
  };
}
