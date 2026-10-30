// The routine result group of the preview: destinations and deliveries in memory. It posts nothing,
// so "send again" marks the delivery as sent at once.

import type {
  OpenBotDesktopApi,
  Routine,
  RoutineResultConnectionOption,
  RoutineResultDelivery,
  RoutineResultDestination,
} from "@openbot/contracts/ipc";

export interface MockRoutineResultsOptions {
  routineResultOptions?: RoutineResultConnectionOption[];
  /** Each routine's destination, by routine id. */
  routineResultDestinations?: Record<string, RoutineResultDestination>;
  /** Each routine's deliveries, newest first, by routine id. */
  routineResultDeliveries?: Record<string, RoutineResultDelivery[]>;
}

/** `emit` sends the agent event `routines-changed` for the agent whose results changed. */
export function mockRoutineResults(
  routines: Map<string, Routine[]>,
  options: MockRoutineResultsOptions,
  emit: (agentId: string) => void,
): OpenBotDesktopApi["routineResults"] {
  const connections = structuredClone(options.routineResultOptions ?? []);
  const destinations = new Map(Object.entries(structuredClone(options.routineResultDestinations ?? {})));
  const deliveries = new Map(Object.entries(structuredClone(options.routineResultDeliveries ?? {})));
  const owns = (agentId: string, routineId: string) =>
    (routines.get(agentId) ?? []).some((routine) => routine.id === routineId);

  return {
    options: async () => structuredClone(connections),
    destination: async ({ agentId, routineId }) =>
      owns(agentId, routineId) ? structuredClone(destinations.get(routineId) ?? null) : null,
    setDestination: async ({ agentId, routineId, destination }) => {
      if (!owns(agentId, routineId)) throw new Error("This routine no longer exists.");
      if (!destination) {
        destinations.delete(routineId);
        const now = new Date().toISOString();
        for (const delivery of deliveries.get(routineId) ?? []) {
          if (delivery.status === "pending")
            Object.assign(delivery, { status: "failed", reason: "stopped", updatedAt: now });
        }
      } else {
        const connection = connections.find((entry) => entry.connectionId === destination.connectionId);
        if (!connection) throw new Error("This messaging connection is off or removed.");
        destinations.set(routineId, {
          ...destination,
          platform: connection.platform,
          connectionName: connection.connectionName,
        });
      }
      emit(agentId);
      return structuredClone(destinations.get(routineId) ?? null);
    },
    deliveries: async ({ agentId, routineId }) =>
      owns(agentId, routineId) ? structuredClone(deliveries.get(routineId) ?? []) : [],
    resend: async ({ agentId, runId }) => {
      const now = new Date().toISOString();
      for (const routine of routines.get(agentId) ?? []) {
        const delivery = deliveries.get(routine.id)?.find((entry) => entry.runId === runId);
        if (!delivery) continue;
        Object.assign(delivery, { status: "sent", reason: null, attempts: 1, updatedAt: now, sentAt: now });
        emit(agentId);
        return;
      }
      throw new Error("This run has no answer to send again.");
    },
  };
}
