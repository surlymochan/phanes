// Reproduces issue #9 against the local Cloudflare Vite runtime (workerd/Miniflare).
// Run from `volto`: bun apps/auth-api/scripts/issue-9-local-workerd-e2e.ts
// The report is written to .volto-build/issue-9-local-workerd-e2e/report.json.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { join, resolve } from "node:path";
import { createDevelopmentTicketKeyPair } from "../../../scripts/development-secrets";

const ROOT = resolve(import.meta.dirname, "../../..");
const APP = join(ROOT, "apps/auth-api");
const REPORT = join(ROOT, ".volto-build/issue-9-local-workerd-e2e/report.json");
const STARTUP_TIMEOUT_MS = 45_000;
const token = "issue-9-local-server-claims-admin-token";
const email = `issue-9-${crypto.randomUUID()}@example.test`;
const requests: Array<{ path: string; status: number }> = [];
const logs: string[] = [];
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

let passed = false;
try {
  // Build the local D1 schema before starting workerd. This uses Wrangler's local SQLite state only.
  const migrate = spawn("bun", ["run", "wrangler", "d1", "migrations", "apply", "DB", "--local", "--yes"], {
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
  child = spawn("bun", ["run", "vite", "dev", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], {
    cwd: APP,
    env: environment,
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.on("data", (chunk: Buffer) => logs.push(chunk.toString()));
  child.stderr.on("data", (chunk: Buffer) => logs.push(chunk.toString()));

  const base = `http://127.0.0.1:${port}`;
  await waitForWorker(base);
  const health = await request(base, "/health/live");
  assert.equal(health.status, 200);
  const jwks = await request(base, "/.well-known/jwks.json");
  assert.equal(jwks.status, 200);

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
  const claim = (await signed.json()) as { claim?: unknown };
  assert.equal(typeof claim.claim, "string");

  const redeemed = await request(base, "/v2/server-claims/redeem", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ claim: claim.claim }),
  });
  assert.equal(redeemed.status, 200);
  const session = (await redeemed.json()) as { sessionToken?: unknown; user?: { email?: unknown } };
  assert.equal(typeof session.sessionToken, "string");
  assert.equal(session.user?.email, email);
  passed = true;
} catch (cause) {
  error = cause instanceof Error ? cause.message : String(cause);
} finally {
  if (child && child.exitCode === null) {
    child.kill("SIGTERM");
    await new Promise<void>((done) => {
      const timeout = setTimeout(() => {
        child?.kill("SIGKILL");
        done();
      }, 5_000);
      child?.once("exit", () => {
        clearTimeout(timeout);
        done();
      });
    });
  }
  await mkdir(join(ROOT, ".volto-build/issue-9-local-workerd-e2e"), { recursive: true });
  await writeFile(REPORT, `${JSON.stringify({ passed, requests, error, logs: logs.join("").slice(-8_000) }, null, 2)}\n`);
  process.stdout.write(`${passed ? "PASS" : "FAIL"}${error ? `: ${error}` : ""}: report in ${REPORT}\n`);
  if (!passed) process.exitCode = 1;
}
