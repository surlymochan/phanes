// Reproduces issue #9 against the local Cloudflare Vite runtime (workerd/Miniflare).
// Run from `volto`: bun apps/auth-api/scripts/issue-9-local-workerd-e2e.ts
// The report is written to .volto-build/issue-9-local-workerd-e2e/report.json.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { lstat, mkdir, mkdtemp, rm, symlink, unlink, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { networkInterfaces } from "node:os";
import { join, resolve } from "node:path";
import { isDynamicRecord, isString } from "@volto/contracts/runtime-values";
import { createDevelopmentTicketKeyPair } from "../../../scripts/development-secrets";

const ROOT = resolve(import.meta.dirname, "../../..");
const APP = join(ROOT, "apps/auth-api");
const REPORT = join(ROOT, ".volto-build/issue-9-local-workerd-e2e/report.json");
const STARTUP_TIMEOUT_MS = 45_000;
const token = "issue-9-local-server-claims-admin-token";
const email = `issue-9-${crypto.randomUUID()}@example.test`;
const requests: Array<{ path: string; status: number }> = [];
const logs: string[] = [];
const starts: number[] = [];
let persistDir: string | undefined;
let stateLink: string | undefined;
let cleaned = false;
let persistentSessionRead = false;
let accountReadable = false;
let child: ReturnType<typeof spawn> | undefined;
let error: string | undefined;

async function freePort(): Promise<number> {
  const server = createServer();
  await new Promise<void>((done, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", done);
  });
  const address = server.address();
  assert.ok(address && typeof address === "object");
  await new Promise<void>((done, reject) => server.close((cause) => (cause ? reject(cause) : done())));
  return address.port;
}

function cleanEnvironment(): NodeJS.ProcessEnv {
  const environment = { ...process.env };
  for (const name of ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_KEY", "CLOUDFLARE_EMAIL", "CLOUDFLARE_ACCOUNT_ID"])
    delete environment[name];
  const tickets = createDevelopmentTicketKeyPair();
  return {
    ...environment,
    VOLTO_API_HOST: "127.0.0.1",
    SERVER_CLAIMS_ADMIN_TOKEN: token,
    REMOTE_TICKET_KEY_ID: "openbot-remote-1",
    REMOTE_TICKET_PRIVATE_JWK: tickets.privateJwk,
    REMOTE_TICKET_PUBLIC_JWKS: tickets.publicJwks,
    EMAIL_SMTP_HOST: "",
    EMAIL_SMTP_PORT: "",
    EMAIL_SMTP_USERNAME: "",
    EMAIL_SMTP_PASSWORD: "",
    EMAIL_FROM: "",
  };
}

async function request(base: string, path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(new URL(path, base), { ...init, signal: AbortSignal.timeout(5_000) });
  requests.push({ path, status: response.status });
  return response;
}

/** The error code of an error answer. */
function errorCodeOf(body: unknown): string | null {
  if (!isDynamicRecord(body) || !isDynamicRecord(body.error)) return null;
  return isString(body.error.code) ? body.error.code : null;
}

/** A string field of an answer. */
function stringField(body: unknown, key: string): string | null {
  if (!isDynamicRecord(body)) return null;
  const field = body[key];
  return isString(field) ? field : null;
}

/** The email of the signed-in user in a redeemed answer. */
function redeemedEmail(body: unknown): string | null {
  if (!isDynamicRecord(body) || !isDynamicRecord(body.user)) return null;
  return isString(body.user.email) ? body.user.email : null;
}

/** The local Worker must answer on the loopback address only, so a machine on the network cannot reach it. */
async function assertLoopbackOnly(port: number): Promise<void> {
  const addresses = Object.values(networkInterfaces())
    .flatMap((entries) => entries ?? [])
    .filter((entry) => entry.family === "IPv4" && !entry.internal);
  for (const address of addresses) {
    let answered = false;
    try {
      const response = await fetch(`http://${address.address}:${port}/health/live`, {
        signal: AbortSignal.timeout(2_000),
      });
      answered = response.ok;
    } catch {
      // A refused or dropped connection is the expected answer on a non-loopback address.
    }
    assert.equal(
      answered,
      false,
      `The local Worker answered on ${address.address}, so it does not listen on the loopback address only.`,
    );
  }
}

async function waitForWorker(base: string): Promise<void> {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (child?.exitCode !== null && child?.exitCode !== undefined) {
      throw new Error(`Vite exited with code ${child.exitCode}: ${logs.join("").slice(-8_000)}`);
    }
    try {
      const response = await fetch(new URL("/health/live", base), { signal: AbortSignal.timeout(1_000) });
      if (response.ok) return;
    } catch {
      // Keep waiting while Vite starts workerd.
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Local Worker did not become ready: ${logs.join("").slice(-8_000)}`);
}

async function stopWorker(): Promise<void> {
  if (!child || child.exitCode !== null) return;
  const stopping = child;
  stopping.kill("SIGTERM");
  await new Promise<void>((done) => {
    const timeout = setTimeout(() => {
      stopping.kill("SIGKILL");
      done();
    }, 5_000);
    stopping.once("exit", () => {
      clearTimeout(timeout);
      done();
    });
  });
  child = undefined;
}

function startWorker(port: number, environment: NodeJS.ProcessEnv): void {
  starts.push(Date.now());
  child = spawn(
    "bun",
    ["run", "vite", "dev", "--host", "127.0.0.1", "--port", String(port), "--strictPort"],
    { cwd: APP, env: environment, stdio: ["ignore", "pipe", "pipe"] },
  );
  child.stdout?.on("data", (chunk: Buffer) => logs.push(chunk.toString()));
  child.stderr?.on("data", (chunk: Buffer) => logs.push(chunk.toString()));
}

let passed = false;
try {
  persistDir = await mkdtemp(join(ROOT, ".volto-build/issue-9-workerd-state-"));
  // Wrangler and the Vite plugin use .wrangler/state by default. Point that path at this run's
  // isolated directory so the migration and both Worker processes use the same database.
  stateLink = join(APP, ".wrangler/state");
  try {
    await lstat(stateLink);
    throw new Error(`Refusing to replace existing local Wrangler state at ${stateLink}`);
  } catch (cause) {
    if (cause instanceof Error && !("code" in cause && cause.code === "ENOENT")) throw cause;
  }
  await mkdir(join(APP, ".wrangler"), { recursive: true });
  await symlink(persistDir, stateLink, "dir");
  // Build the local D1 schema before starting workerd. This uses Wrangler's local SQLite state only.
  // Wrangler 4.x has no `--yes` flag here: it skips the confirmation when the command runs without
  // a terminal, which the piped output of this script already is.
  const migrate = spawn("bun", ["run", "wrangler", "d1", "migrations", "apply", "DB", "--local", "--persist-to", persistDir], {
    cwd: APP,
    env: cleanEnvironment(),
    stdio: ["ignore", "pipe", "pipe"],
  });
  let migrationOutput = "";
  migrate.stdout.on("data", (chunk: Buffer) => (migrationOutput += chunk.toString()));
  migrate.stderr.on("data", (chunk: Buffer) => (migrationOutput += chunk.toString()));
  const migrationCode = await new Promise<number>((done, reject) => {
    migrate.once("error", reject);
    migrate.once("exit", (code) => done(code ?? 1));
  });
  assert.equal(migrationCode, 0, `Local D1 migrations failed: ${migrationOutput}`);

  const port = await freePort();
  const environment = cleanEnvironment();
  delete environment.AUTH_EXPOSE_DEVELOPMENT_CODE;
  startWorker(port, environment);

  const base = `http://127.0.0.1:${port}`;
  await waitForWorker(base);
  await assertLoopbackOnly(port);
  const health = await request(base, "/health/live");
  assert.equal(health.status, 200);
  const jwks = await request(base, "/.well-known/jwks.json");
  assert.equal(jwks.status, 200);

  // The local run leaves the mail settings empty and does not expose the development code. A Worker
  // that kept either one answers this request with a code or with a failed mail setup.
  const signIn = await request(base, "/v1/auth/email/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: `issue-9-signin-${crypto.randomUUID()}@example.test` }),
  });
  assert.equal(signIn.status, 503);
  assert.equal(errorCodeOf(await signIn.json()), "email_delivery_not_configured");

  const anonymous = await request(base, "/v2/admin/server-claims", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  });
  assert.equal(anonymous.status, 401);

  const signed = await request(base, "/v2/admin/server-claims", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  });
  assert.equal(signed.status, 200, "SERVER_CLAIMS_ADMIN_TOKEN must reach the local Worker runtime");
  const claim = stringField(await signed.json(), "claim");
  assert.ok(claim);

  const redeemed = await request(base, "/v2/server-claims/redeem", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ claim }),
  });
  assert.equal(redeemed.status, 200);
  const session = await redeemed.json();
  assert.ok(stringField(session, "sessionToken"));
  assert.equal(redeemedEmail(session), email);

  const sessionToken = stringField(session, "sessionToken");
  assert.ok(sessionToken);
  await stopWorker();
  if (stateLink) await unlink(stateLink).catch(() => undefined);
  startWorker(port, environment);
  await waitForWorker(base);

  const persistedSession = await request(base, "/api/browser/session", {
    headers: { Cookie: `__Host-openbot-web=${sessionToken}` },
  });
  assert.equal(persistedSession.status, 200, "the redeemed account session must survive a workerd restart");
  persistentSessionRead = true;
  const persistedAccount = await persistedSession.json();
  assert.equal(redeemedEmail(persistedAccount), email, "the claimed account must remain readable after a workerd restart");
  accountReadable = true;
  assert.equal(starts.length, 2, "workerd must start twice against the same persistent state directory");
  passed = true;
} catch (cause) {
  error = cause instanceof Error ? cause.message : String(cause);
} finally {
  await stopWorker();
  let cleanupFailed = false;
  if (stateLink) {
    try {
      await unlink(stateLink);
    } catch (cause) {
      if (!(cause instanceof Error && "code" in cause && cause.code === "ENOENT")) {
        const cleanupError = cause instanceof Error ? cause.message : String(cause);
        error = error ? `${error}; state link cleanup failed: ${cleanupError}` : `state link cleanup failed: ${cleanupError}`;
        passed = false;
        cleanupFailed = true;
      }
    }
  }
  if (persistDir) {
    try {
      await rm(persistDir, { recursive: true, force: true });
      cleaned = !cleanupFailed;
    } catch (cause) {
      const cleanupError = cause instanceof Error ? cause.message : String(cause);
      error = error ? `${error}; cleanup failed: ${cleanupError}` : `cleanup failed: ${cleanupError}`;
      passed = false;
      cleanupFailed = true;
    }
  }
  await mkdir(join(ROOT, ".volto-build/issue-9-local-workerd-e2e"), { recursive: true });
  await writeFile(
    REPORT,
    `${JSON.stringify({ passed, starts: starts.length, persistentSessionRead, accountReadable, cleaned, requests, error, logs: logs.join("").slice(-8_000) }, null, 2)}\n`,
  );
  process.stdout.write(`${passed ? "PASS" : "FAIL"}${error ? `: ${error}` : ""}: report in ${REPORT}\n`);
  if (!passed) process.exitCode = 1;
}
