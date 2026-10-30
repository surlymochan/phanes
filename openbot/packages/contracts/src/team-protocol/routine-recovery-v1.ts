// Frozen optional routine-recovery-v1 wire contract. Keep IPC types and limits out of this file.
//
// What it grants, recorded here because freezing it makes it permanent: an owner or admin of a
// server reads and sets what a routine does with the occurrences that came due while the host was
// off (`missedPolicy`), and reads a routine's run history with the records of those occurrences. The
// list and save routes carry the `events-v1` routine with `missedPolicy` added; the host answers them
// as the `events-v1` routes do, so a client with this capability uses them in place of those two.
// A run record names its missed occurrences in `missed`, and a run started by a test, a script or a
// webhook names its `source`. An agent run names the conversation message it posted in `deliveryId`,
// and a channel run names the channel message it posted in `requestMessageId`.
// Widening any of it needs a second capability string.
//
// A run instruction holds the routine instruction and the event of a webhook or a script, so it can be
// longer than the routine instruction. A run error is at most `ROUTINE_RECOVERY_RUN_ERROR_LIMIT`
// long; the host shortens a longer one.
import {
  type AdminDecoder,
  adminRoute,
  boolean,
  fields,
  identifier,
  list,
  nullable,
  type OptionalRouteCodec,
  oneOf,
  string,
} from "./admin-wire";
import { routineOwner, routineTrigger, routineTriggerInput } from "./events-v1";

export const ROUTINE_RECOVERY_CAPABILITY = "routine-recovery-v1";

export const ROUTINE_RECOVERY_ROUTES = {
  listRoutines: "/v1/admin/routine-recovery/routines/list",
  saveRoutine: "/v1/admin/routine-recovery/routines/save",
  listRuns: "/v1/admin/routine-recovery/runs",
} as const;

export const ROUTINE_RECOVERY_RUN_ERROR_LIMIT = 100_000;

const timestamp = string(64);
const limitPolicy = oneOf("wait", "skip");
const missedPolicy = oneOf("skip", "run-once");
const runLimit: AdminDecoder = (value) => {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1 || value > 100) {
    throw new Error("Invalid routine run limit.");
  }
  return value;
};
const missedCount: AdminDecoder = (value) => {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1) {
    throw new Error("Invalid missed run count.");
  }
  return value;
};
const routine = fields(
  {
    id: identifier,
    owner: routineOwner,
    name: string(256),
    instruction: string(100_000),
    active: boolean,
    timezone: string(128),
    trigger: routineTrigger,
    createdAt: timestamp,
    updatedAt: timestamp,
  },
  { limitPolicy, missedPolicy },
);
const run = fields(
  {
    id: identifier,
    routineId: identifier,
    triggerId: nullable(identifier),
    kind: oneOf("scheduled", "manual"),
    scheduledFor: timestamp,
    routineName: string(256),
    instruction: string(200_000),
    status: oneOf("queued", "running", "needs-attention", "succeeded", "failed", "interrupted", "cancelled"),
    error: nullable(string(ROUTINE_RECOVERY_RUN_ERROR_LIMIT)),
    createdAt: timestamp,
    updatedAt: timestamp,
  },
  {
    source: oneOf("test", "script", "webhook"),
    missed: fields({ count: missedCount, until: nullable(timestamp) }),
    deliveryId: nullable(identifier),
    requestMessageId: nullable(identifier),
  },
);

export const ROUTINE_RECOVERY_CODECS: ReadonlyMap<string, OptionalRouteCodec> = new Map([
  [ROUTINE_RECOVERY_ROUTES.listRoutines, adminRoute(fields({ owner: routineOwner }), list(routine, 10_000))],
  [
    ROUTINE_RECOVERY_ROUTES.saveRoutine,
    adminRoute(
      fields(
        {
          owner: routineOwner,
          name: string(256),
          instruction: string(100_000),
          active: boolean,
          timezone: string(128),
          trigger: routineTriggerInput,
        },
        { id: identifier, limitPolicy, missedPolicy },
      ),
      fields({ routine, secret: nullable(string(1_024)) }),
    ),
  ],
  [
    ROUTINE_RECOVERY_ROUTES.listRuns,
    adminRoute(fields({ owner: routineOwner, routineId: identifier }, { limit: runLimit }), list(run, 100)),
  ],
]);
