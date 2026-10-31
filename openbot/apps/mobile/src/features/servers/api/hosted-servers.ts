import {
  type HostedServerCatalog,
  type HostedServerList,
  type HostedServerSummary,
  parseHostedServerCatalog,
  parseHostedServerList,
  parseHostedServerSummary,
} from "@openbot/contracts/hosted-servers";
import { type MobileSession, requestMobileAccount } from "@/features/auth/api/mobile-auth";
import { currentText } from "@/shared/lib/text";

export interface HostedServerCalls {
  list(): Promise<HostedServerList>;
  plans(): Promise<HostedServerCatalog>;
  wake(serverId: string): Promise<HostedServerSummary>;
  /** The owner or an admin restarts the server. The account server stops it, then starts it again. */
  restart(serverId: string): Promise<HostedServerSummary>;
}

/**
 * The account server's hosted servers, with the phone's session. The phone reads the plans and the
 * servers, and starts or restarts a server. App store rules do not allow a purchase outside the store, so
 * the purchase stays on desktop.
 */
export function hostedServerCalls(session: MobileSession): HostedServerCalls {
  const { t } = currentText();
  const serverPath = (serverId: string, action: string) =>
    `/v2/hosting/servers/${encodeURIComponent(serverId)}/${action}`;
  return {
    list: () =>
      requestMobileAccount(
        session,
        "/v2/hosting/servers/",
        {},
        parseHostedServerList,
        t("mobile.server.hosted.loadFailed"),
      ),
    plans: () =>
      requestMobileAccount(
        session,
        "/v2/hosting/plans",
        {},
        parseHostedServerCatalog,
        t("mobile.server.hosted.loadFailed"),
      ),
    wake: (serverId) =>
      requestMobileAccount(
        session,
        serverPath(serverId, "wake"),
        { method: "POST" },
        parseHostedServerSummary,
        t("mobile.server.hosted.wakeFailed"),
      ),
    // The account server asks the provider to stop the server before it answers, so this can take longer than a read.
    restart: (serverId) =>
      requestMobileAccount(
        session,
        serverPath(serverId, "restart"),
        { method: "POST" },
        parseHostedServerSummary,
        t("mobile.server.hosted.restartFailed"),
        30_000,
      ),
  };
}
