// The data of the status page. `scripts/update.ts` writes `state.json` (kept on the `status-data`
// branch) and the public `status.json`; the page decodes `status.json` when it loads it.
import { Schema } from "effect";

export const SERVICE_IDS = ["accounts", "connections", "hosted", "updates", "marketplace"] as const;
export const ServiceId = Schema.Literals(SERVICE_IDS);
export type ServiceId = typeof ServiceId.Type;

export interface ServiceInfo {
  id: ServiceId;
  name: string;
  description: string;
}

export const SERVICES: readonly ServiceInfo[] = [
  { id: "accounts", name: "Accounts and sign-in", description: "Sign-in, profiles, server plans and invitations" },
  {
    id: "connections",
    name: "Connections",
    description: "Phones, browsers and teammates that connect to your computer",
  },
  { id: "hosted", name: "Hosted servers", description: "Cloud servers that OpenBot runs for you" },
  { id: "updates", name: "App updates", description: "Desktop update downloads" },
  { id: "marketplace", name: "Marketplace", description: "Agents, skills and plugins" },
];

export const serviceName = (id: ServiceId) => SERVICES.find((service) => service.id === id)?.name ?? id;

/** The result of one health check. `unknown` is a service that no run has checked yet. */
export const CheckResult = Schema.Literals(["ok", "degraded", "down", "unknown"]);
export type CheckResult = typeof CheckResult.Type;

export type StatusState = "operational" | "degraded" | "partial" | "major" | "maintenance";
export const Impact = Schema.Literals(["degraded", "partial", "major", "maintenance"]);
export type Impact = typeof Impact.Type;

export const Stage = Schema.Literals([
  "investigating",
  "identified",
  "monitoring",
  "update",
  "resolved",
  "scheduled",
  "completed",
]);
export type Stage = typeof Stage.Type;

export const IncidentUpdate = Schema.Struct({ at: Schema.String, stage: Stage, body: Schema.String });
export type IncidentUpdate = typeof IncidentUpdate.Type;

export const Incident = Schema.Struct({
  /** The GitHub issue number. */
  id: Schema.Int,
  url: Schema.String,
  title: Schema.String,
  kind: Schema.Literals(["incident", "maintenance"]),
  impact: Impact,
  components: Schema.Array(ServiceId),
  startedAt: Schema.String,
  /** Null while the incident is open. For maintenance, the planned end. */
  resolvedAt: Schema.NullOr(Schema.String),
  /** Newest first. */
  updates: Schema.Array(IncidentUpdate),
});
export type Incident = typeof Incident.Type;

/** One UTC day of checks for one service. Minutes are the time that the checks of the day cover. */
export const CheckDay = Schema.Struct({
  date: Schema.String,
  checks: Schema.Int,
  minutes: Schema.Number,
  failedMinutes: Schema.Number,
  degradedMinutes: Schema.Number,
});
export type CheckDay = typeof CheckDay.Type;

export const ServiceStatus = Schema.Struct({
  id: ServiceId,
  result: CheckResult,
  /** Oldest first, at most `HISTORY_DAYS`. */
  days: Schema.Array(CheckDay),
});
export type ServiceStatus = typeof ServiceStatus.Type;

/** `status.json`, the public file that the page reads. */
export const StatusFile = Schema.Struct({
  version: Schema.Literal(1),
  checkedAt: Schema.NullOr(Schema.String),
  services: Schema.Array(ServiceStatus),
  incidents: Schema.Array(Incident),
});
export type StatusFile = typeof StatusFile.Type;

export const ServiceState = Schema.Struct({
  result: CheckResult,
  changedAt: Schema.NullOr(Schema.String),
  failedRuns: Schema.Int,
  healthyRuns: Schema.Int,
  /** The automatic issue that is open for this service. */
  openIssue: Schema.NullOr(Schema.Int),
  days: Schema.Array(CheckDay),
});
export type ServiceState = typeof ServiceState.Type;

/** `state.json` on the `status-data` branch. Only the update workflow reads it. */
export const StateFile = Schema.Struct({
  version: Schema.Literal(1),
  checkedAt: Schema.NullOr(Schema.String),
  services: Schema.Struct({
    accounts: ServiceState,
    connections: ServiceState,
    hosted: ServiceState,
    updates: ServiceState,
    marketplace: ServiceState,
  }),
});
export type StateFile = typeof StateFile.Type;

export const decodeStatusFile = Schema.decodeUnknownSync(StatusFile);
export const decodeStateFile = Schema.decodeUnknownSync(StateFile);

export const HISTORY_DAYS = 90;
const DAY_MS = 86_400_000;
const DAY_MINUTES = DAY_MS / 60_000;

const STATE_RANK: Record<StatusState, number> = { operational: 0, maintenance: 1, degraded: 2, partial: 3, major: 4 };

export const worseState = (a: StatusState, b: StatusState): StatusState => (STATE_RANK[b] > STATE_RANK[a] ? b : a);

/** The days, oldest first, that end with the day of `now`. */
export function statusDates(count: number, now: string): string[] {
  const today = Date.parse(`${now.slice(0, 10)}T00:00:00Z`);
  return Array.from({ length: count }, (_, index) =>
    new Date(today - (count - 1 - index) * DAY_MS).toISOString().slice(0, 10),
  );
}

/** A day with no checks and no incident has no state. */
export interface StatusDay {
  date: string;
  state: StatusState | null;
  check: CheckDay | null;
  incidents: Incident[];
  /** Down minutes from the incidents of the day: all of a major outage, 30% of a partial outage. */
  outageMinutes: number;
}

/** The share of a partial outage that counts as down time, as in Atlassian Statuspage. */
const PARTIAL_WEIGHT = 0.3;

/** A day with checks that failed for 30 minutes or more is a major outage. */
const MAJOR_FAILED_MINUTES = 30;

function checkState(day: CheckDay) {
  if (day.failedMinutes >= MAJOR_FAILED_MINUTES) return "major";
  if (day.failedMinutes > 0) return "partial";
  if (day.degradedMinutes > 0) return "degraded";
  return "operational";
}

/** One entry per day for one service, from its checks and from the incidents that touch the day. */
export function serviceDays(
  service: ServiceStatus,
  incidents: readonly Incident[],
  count: number,
  now: string,
): StatusDay[] {
  const nowMs = Date.parse(now);
  const checks = new Map(service.days.map((day) => [day.date, day]));
  return statusDates(count, now).map((date) => {
    const start = Date.parse(`${date}T00:00:00Z`);
    const end = Math.min(start + DAY_MS, nowMs);
    const check = checks.get(date) ?? null;
    let state: StatusState | null = check && check.checks > 0 ? checkState(check) : null;
    const touching: Incident[] = [];
    const major: [number, number][] = [];
    const outages: [number, number][] = [];
    for (const incident of incidents) {
      if (!incident.components.includes(service.id)) continue;
      const from = Math.max(Date.parse(incident.startedAt), start);
      const to = Math.min(incident.resolvedAt ? Date.parse(incident.resolvedAt) : nowMs, end);
      if (to <= from) continue;
      touching.push(incident);
      if (incident.impact === "major") major.push([from, to]);
      if (incident.impact === "major" || incident.impact === "partial") outages.push([from, to]);
      state = worseState(state ?? "operational", incident.impact);
    }
    const majorMs = coveredMs(major);
    const outageMs = majorMs + PARTIAL_WEIGHT * (coveredMs(outages) - majorMs);
    return { date, state, check, incidents: touching, outageMinutes: outageMs / 60_000 };
  });
}

/** The time that the intervals cover, with overlaps counted once. */
function coveredMs(intervals: [number, number][]) {
  let total = 0;
  let reached = -Infinity;
  for (const [from, to] of intervals.sort((a, b) => a[0] - b[0])) {
    if (to > reached) {
      total += to - Math.max(from, reached);
      reached = to;
    }
  }
  return total;
}

/**
 * The share of time in which the service was available, or null when there is no data. A failed
 * check and an outage incident are both down time, so an outage that the checks missed also counts.
 */
export function uptimePercent(days: readonly StatusDay[]): number | null {
  let minutes = 0;
  let down = 0;
  for (const day of days) {
    const failed = day.check?.failedMinutes ?? 0;
    // A day with an incident but no checks, such as a day before the checks started, is one full day.
    const checked = day.check && day.check.checks > 0 ? day.check.minutes : day.outageMinutes > 0 ? DAY_MINUTES : 0;
    minutes += Math.max(checked, day.outageMinutes);
    down += Math.max(failed, day.outageMinutes);
  }
  return minutes === 0 ? null : 100 * (1 - down / minutes);
}

const RESULT_STATE: Record<CheckResult, StatusState | null> = {
  ok: "operational",
  degraded: "degraded",
  down: "major",
  unknown: null,
};

/** Incidents that run at `now`. Maintenance counts only inside its window. */
export function isActive(incident: Incident, now: string): boolean {
  const nowMs = Date.parse(now);
  return (
    Date.parse(incident.startedAt) <= nowMs && (incident.resolvedAt === null || Date.parse(incident.resolvedAt) > nowMs)
  );
}

/** The worst of the last check and the incidents that run now. */
export function currentState(service: ServiceStatus, incidents: readonly Incident[], now: string): StatusState | null {
  let state = RESULT_STATE[service.result];
  for (const incident of incidents) {
    if (incident.components.includes(service.id) && isActive(incident, now)) {
      state = worseState(state ?? "operational", incident.impact);
    }
  }
  return state;
}
