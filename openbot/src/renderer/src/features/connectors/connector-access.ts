import type { ServerSummary } from "@openbot/contracts/ipc";
import { CONNECTORS, type ConnectorId } from "@openbot/ui/features/connectors/connectors";
import { remoteAdminServer } from "../servers/server-capabilities";

/**
 * Where a server can use a connector. `local-only`: only the computer that runs OpenBot can connect
 * it, because its app or its browser runs there. `hidden`: this account does not manage the
 * server's connectors, or the host does not serve them.
 */
export type ConnectorAccess = "available" | "local-only" | "hidden";

/** The one rule for every surface: this computer runs every connector; a joined server runs the
 * connectors whose capability it serves, for an owner or admin. */
export function connectorAccess(id: ConnectorId, server: ServerSummary | undefined): ConnectorAccess {
  if (!server) return "hidden";
  if (server.kind === "local") return "available";
  const capability = CONNECTORS[id].server;
  if (capability === null) return "local-only";
  return remoteAdminServer(server, capability) ? "available" : "hidden";
}
