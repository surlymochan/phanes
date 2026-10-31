import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { FleetTurnReservation } from "../src/backend/agent/fleet-turn-gate";
import { FleetTurnCommandTransport } from "../src/backend/agent/fleet-turn-transport";

const reservation: FleetTurnReservation = {
  run_id: "hub-e2e",
  token: "12345678901234567890123456789012",
  backend: "codex",
  provider: null,
};
const coordinator = (script: string, timeoutMs = 2_000) =>
  new FleetTurnCommandTransport(process.execPath, ["-e", script], timeoutMs);
const checks: string[] = [];

const transport = coordinator(`
  let input = '';
  process.stdin.on('data', chunk => input += chunk);
  process.stdin.on('end', () => {
    const request = JSON.parse(input);
    if (request.run_id !== 'hub-e2e' || request.token.length !== 32) process.exit(4);
    process.stdout.write(JSON.stringify(request.op === 'acquire'
      ? {ok:true, admitted:true} : {ok:true, released:true}));
  });
`);
assert.deepEqual(await transport.acquire(reservation), { admitted: true });
await transport.release(reservation);
await coordinator('process.stdout.write(\'{"ok":true,"released":false}\')').release(reservation);
await assert.rejects(coordinator("process.stdout.write('{\"ok\":true}')").release(reservation), /release response/i);
checks.push("separate acquire and release requests reach a child process");

const retryDir = await mkdtemp(join(tmpdir(), "hub-fleet-release-"));
try {
  const attempts = join(retryDir, "attempts");
  const retrying = coordinator(`
    const fs = require('node:fs');
    const path = ${JSON.stringify(attempts)};
    const count = fs.existsSync(path) ? Number(fs.readFileSync(path, 'utf8')) : 0;
    fs.writeFileSync(path, String(count + 1));
    process.stdin.resume();
    process.stdin.on('end', () => {
      if (count === 0) process.exit(1); // deletion committed, response lost
      process.stdout.write(JSON.stringify({ok:true, released:false}));
    });
  `);
  await retrying.release(reservation);
  assert.equal(await readFile(attempts, "utf8"), "2");
  checks.push("release retries a lost acknowledgement and accepts an absent reservation");
} finally {
  await rm(retryDir, { recursive: true, force: true });
}

await assert.rejects(coordinator("process.stdout.write('{}')").acquire(reservation), /invalid response/i);
await assert.rejects(coordinator("process.stdout.write('not-json')").acquire(reservation), /invalid response/i);
await assert.rejects(coordinator("process.stdout.write('{\"ok\":true}')").acquire(reservation), /admission response/i);
checks.push("malformed responses are rejected");

await assert.rejects(
  coordinator("process.stdin.resume(); setTimeout(() => {}, 10000)", 50).acquire(reservation),
  /timed out/i,
);
await assert.rejects(coordinator("process.stdout.write('x'.repeat(70000))").acquire(reservation), /too large/i);
checks.push("lost and oversized responses fail closed");
if (process.platform !== "win32") {
  await assert.rejects(
    coordinator("process.on('SIGTERM', () => {}); process.stdin.resume(); setInterval(() => {}, 10000)", 200).acquire(
      reservation,
    ),
    /timed out/i,
  );
  checks.push("a coordinator ignoring SIGTERM is killed before the timeout rejection");
}

const directory = join(process.cwd(), ".openbot-build", "fleet-turn-transport-e2e");
await mkdir(directory, { recursive: true });
const output = join(directory, "report.json");
await writeFile(output, `${JSON.stringify({ passed: true, checks }, null, 2)}\n`);
process.stdout.write(`PASS ${output}\n`);
