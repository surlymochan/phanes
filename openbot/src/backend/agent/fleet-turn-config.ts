import { readFileSync } from "node:fs";
import { isAbsolute } from "node:path";
import { Effect } from "effect";
import { z } from "zod";
import { FleetTurnGate } from "./fleet-turn-gate";
import { FleetTurnCommandTransport } from "./fleet-turn-transport";

const configSchema = z.object({
  journalDirectory: z.string().min(1).refine(isAbsolute),
  command: z.tuple([z.string().min(1).refine(isAbsolute)]).rest(z.string()),
});

let cachedPath: string | null = null;
let cachedGate: FleetTurnGate | null = null;

/** The explicit local configuration is shared by all Codex clients in this process. */
export function configuredFleetTurnGate(): FleetTurnGate | null {
  const path = process.env.OPENBOT_FLEET_CAPACITY_CONFIG?.trim();
  if (!path) {
    if (cachedGate) throw new Error("Fleet capacity configuration cannot be removed while OpenBot is running.");
    if (process.env.OPENBOT_FLEET_CAPACITY_REQUIRED === "1") {
      throw new Error("Fleet capacity configuration is required.");
    }
    return null;
  }
  if (!isAbsolute(path)) throw new Error("Fleet capacity configuration path must be absolute.");
  if (cachedPath === path && cachedGate) return cachedGate;
  if (cachedGate) throw new Error("Fleet capacity configuration cannot change while OpenBot is running.");
  const config = configSchema.parse(JSON.parse(readFileSync(path, "utf8")));
  const [command, ...args] = config.command;
  const gate = Effect.runSync(
    FleetTurnGate.open(config.journalDirectory, new FleetTurnCommandTransport(command, args)),
  );
  cachedPath = path;
  cachedGate = gate;
  return gate;
}
