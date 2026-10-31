import type { OnePasswordConnectorStatus, OpenBotDesktopApi } from "@openbot/contracts/ipc";
import { sourceText } from "@openbot/i18n/source";

/** What the 1Password page reaches: one 1Password connection, of this computer or of a joined server. */
export type OnePasswordConnectorPort = Omit<
  OpenBotDesktopApi["onePasswordConnector"],
  "hostStatus" | "hostConnectWithToken" | "hostDisconnect"
>;

/** Read on each call: tests and stories replace `window.openbot` per case. */
export function onePasswordConnectorPort(): OnePasswordConnectorPort {
  return window.openbot.onePasswordConnector;
}

/** The actions of a joined server's connection (`onepassword-connector-v1`): a token connects it. */
export type HostOnePasswordActions = Pick<OnePasswordConnectorPort, "status" | "connectWithToken" | "disconnect">;

/** How often a joined server's connection is read while the host connects or still counts its logins. */
const HOST_POLL_MS = 2_000;
/** A read of the logins that fails leaves no count, so the reads stop. Opening the page reads again. */
const HOST_POLL_LIMIT = 15;

/**
 * A port to a joined server's connection. The host sends no event, so `onChanged` reads the status
 * while the host connects or the login count is not known, and stops after `HOST_POLL_LIMIT` reads
 * or when a read fails. Each status read asks the host to read its logins again when they are old.
 * The host has no screen, so the CLI and the 1Password app are not used: their actions fail.
 */
export function polledOnePasswordConnectorPort(actions: HostOnePasswordActions): OnePasswordConnectorPort {
  const listeners = new Set<(status: OnePasswordConnectorStatus) => void>();
  let timer: ReturnType<typeof setTimeout> | null = null;
  let reads = 0;
  const stop = () => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
  };
  const seen = (status: OnePasswordConnectorStatus) => {
    for (const listener of listeners) listener(status);
    const waiting = status.state === "connecting" || (status.state === "connected" && status.loginCount === null);
    if (!waiting) {
      stop();
      reads = 0;
    } else if (timer === null && listeners.size > 0 && reads < HOST_POLL_LIMIT)
      timer = setTimeout(() => {
        timer = null;
        reads += 1;
        void actions.status().then(seen, () => undefined);
      }, HOST_POLL_MS);
    return status;
  };
  const unsupported = () => Promise.reject(new Error(sourceText("error.team.connectorsUnsupported")));
  return {
    status: async () => seen(await actions.status()),
    checkSetup: async () => {
      reads = 0;
      return seen(await actions.status());
    },
    installCli: unsupported,
    openApp: unsupported,
    connect: unsupported,
    connectWithToken: async (token) => {
      reads = 0;
      return seen(await actions.connectWithToken(token));
    },
    cancel: async () => seen(await actions.status()),
    disconnect: async () => seen(await actions.disconnect()),
    onChanged: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0) stop();
      };
    },
  };
}

/** The connection of the joined server that `serverId` names, through main. */
export function hostOnePasswordConnectorPort(serverId: () => string | undefined): OnePasswordConnectorPort {
  const server = () => {
    const id = serverId();
    if (!id) throw new Error(sourceText("error.team.connectorsUnsupported"));
    return id;
  };
  const api = () => window.openbot.onePasswordConnector;
  return polledOnePasswordConnectorPort({
    status: async () => api().hostStatus(server()),
    connectWithToken: async (token) => api().hostConnectWithToken({ token }, server()),
    disconnect: async () => api().hostDisconnect(server()),
  });
}
