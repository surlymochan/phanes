import type { AgentEvent, RoutineCalendar, RoutineCalendarInput, TeamRealtimeEvent } from "@openbot/contracts/ipc";

/** What the schedule reads: the routine calendar of a server, and the changes that make it stale. */
export interface ScheduleSource {
  calendar(input: RoutineCalendarInput, serverId: string): Promise<RoutineCalendar>;
  /** Calls `stale` after a change on the server that can add, move or end a run. Returns the unsubscribe. */
  onStale(serverId: string, stale: () => void): () => void;
}

/** A host event that can add, move or end a run. */
export function changesSchedule(event: AgentEvent | TeamRealtimeEvent): boolean {
  return (
    event.type === "routines-changed" ||
    event.type === "channel-routines-changed" ||
    event.type === "agents-changed" ||
    // A routine run ends as a turn: its slot changes from planned to its outcome.
    (event.type === "turn-completed" && event.origin === "routine")
  );
}

/** The schedule on desktop: main reads the calendar of any server. Read on each call: tests and stories replace `window.openbot` per case. */
export function desktopScheduleSource(): ScheduleSource {
  return {
    calendar: (input, serverId) => window.openbot.agent.routineCalendar(input, serverId),
    onStale(serverId, stale) {
      const unsubscribe = window.openbot.agent.onScopedEvent((scoped) => {
        if (scoped.serverId === serverId && changesSchedule(scoped.event)) stale();
      });
      const reconnect = window.openbot.servers.onEvent((servers) => {
        if (servers.some((server) => server.id === serverId && server.state === "online")) stale();
      });
      return () => {
        unsubscribe();
        reconnect();
      };
    },
  };
}
