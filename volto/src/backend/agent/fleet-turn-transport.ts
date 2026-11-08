import { spawn } from "node:child_process";
import { z } from "zod";
import type { FleetTurnReservation, FleetTurnTransport } from "./fleet-turn-gate";

const MAX_RESPONSE_BYTES = 64 * 1024;
const responseSchema = z.object({
  ok: z.literal(true),
  admitted: z.boolean().optional(),
  released: z.boolean().optional(),
});
type FleetCommandResponse = z.infer<typeof responseSchema>;
type FleetCommandRequest =
  | ({ op: "acquire" } & FleetTurnReservation)
  | { op: "release"; run_id: string; token: string };

/** A JSON-lines boundary to the forced-command coordinator. Never invokes a shell. */
export class FleetTurnCommandTransport implements FleetTurnTransport {
  constructor(
    readonly command: string,
    readonly args: readonly string[],
    readonly timeoutMs = 15_000,
  ) {
    if (!command || timeoutMs <= 0) throw new Error("Invalid fleet coordinator command.");
  }

  async acquire(input: FleetTurnReservation): Promise<{ admitted: boolean }> {
    const response = await this.#call({ op: "acquire", ...input });
    if (!("admitted" in response) || typeof response.admitted !== "boolean") {
      throw new Error("Invalid fleet admission response.");
    }
    return { admitted: response.admitted };
  }

  async release(input: FleetTurnReservation): Promise<void> {
    for (let attempt = 0; ; attempt++) {
      try {
        const response = await this.#call({ op: "release", run_id: input.run_id, token: input.token });
        // false means this token is already absent, including when the first
        // response was lost after the coordinator committed the deletion.
        if (typeof response.released !== "boolean") throw new Error("Invalid fleet release response.");
        return;
      } catch (error) {
        if (attempt === 2) throw error;
        await new Promise((resolve) => setTimeout(resolve, 200 * (attempt + 1)));
      }
    }
  }

  async #call(input: FleetCommandRequest): Promise<FleetCommandResponse> {
    return await new Promise((resolve, reject) => {
      const child = spawn(this.command, [...this.args], { stdio: ["pipe", "pipe", "ignore"], shell: false });
      let output = "";
      let bytes = 0;
      let done = false;
      const timer = setTimeout(() => fail("Fleet coordinator timed out."), this.timeoutMs);
      const fail = (message: string) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        if (child.exitCode !== null || child.signalCode !== null) {
          reject(new Error(message));
          return;
        }
        child.kill();
        const escalation = setTimeout(() => child.kill("SIGKILL"), 1_000);
        const deadline = setTimeout(() => reject(new Error(message)), 2_000);
        child.once("close", () => {
          clearTimeout(escalation);
          clearTimeout(deadline);
          reject(new Error(message));
        });
      };
      child.on("error", () => fail("Fleet coordinator could not start."));
      child.stdin.on("error", () => fail("Fleet coordinator input failed."));
      child.stdout.on("data", (chunk: Buffer) => {
        bytes += chunk.length;
        if (bytes > MAX_RESPONSE_BYTES) return fail("Fleet coordinator response is too large.");
        output += chunk.toString("utf8");
      });
      child.on("close", (code) => {
        if (done) return;
        if (code !== 0) return fail("Fleet coordinator request failed.");
        try {
          const response = responseSchema.parse(JSON.parse(output));
          done = true;
          clearTimeout(timer);
          resolve(response);
        } catch {
          fail("Fleet coordinator returned an invalid response.");
        }
      });
      child.stdin.end(JSON.stringify(input));
    });
  }
}
