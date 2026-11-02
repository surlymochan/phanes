// Runs the real catalog checker against external example sources.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { createOpenBotLogger } from "@openbot/logging";

const logger = createOpenBotLogger("i18n-check-e2e");
const root = resolve(import.meta.dirname, "..");
const checker = join(root, "scripts", "i18n-check.ts");
const reportPath = join(root, ".openbot-build", "i18n-check-e2e", "report.json");
const temporary = mkdtempSync(join(tmpdir(), "openbot-i18n-check-"));
const checks: string[] = [];
const runs: { name: string; arguments: string[]; status: number | null; stdout: string; stderr: string }[] = [];
let error: string | null = null;

function check(name: string, roots: string[]) {
  const args = roots.flatMap((path) => ["--example-root", path]);
  const result = spawnSync(process.execPath, [checker, ...args], {
    cwd: root,
    encoding: "utf8",
    timeout: 120_000,
    maxBuffer: 8 * 1024 * 1024,
  });
  runs.push({ name, arguments: args, status: result.status, stdout: result.stdout ?? "", stderr: result.stderr ?? "" });
  assert.equal(result.error, undefined, `${name}: ${result.error?.message}`);
  assert.equal(result.signal, null, `${name}: checker terminated with ${result.signal}`);
  return { status: result.status, output: `${result.stdout}\n${result.stderr}` };
}

try {
  const args = process.argv.slice(2);
  assert.equal(args.length, 2, "Use --example-root PATH to name the moved example.");
  assert.equal(args[0], "--example-root");
  assert.ok(args[1]);
  const source = resolve(root, args[1]);
  const fixture = join(temporary, "external-example");
  mkdirSync(fixture);
  for (const file of ["app.js", "index.html"]) {
    writeFileSync(join(fixture, file), readFileSync(join(source, file)));
  }
  assert.equal(existsSync(join(root, "examples")), false, "The migrated legacy example root must be absent.");
  const valid = check("external-example-without-legacy-root", [fixture]);
  assert.equal(valid.status, 0, valid.output);
  checks.push("External JavaScript and HTML are scanned while the legacy example root is absent.");

  const scriptRoot = join(temporary, "script-sources");
  const markupRoot = join(temporary, "markup-sources");
  mkdirSync(scriptRoot);
  mkdirSync(markupRoot);
  writeFileSync(join(scriptRoot, "app.js"), readFileSync(join(fixture, "app.js")));
  writeFileSync(join(markupRoot, "index.html"), readFileSync(join(fixture, "index.html")));
  const repeated = check("repeatable-example-roots", [scriptRoot, markupRoot]);
  assert.equal(repeated.status, 0, repeated.output);
  checks.push("Repeated example roots include both JavaScript and HTML source trees.");

  const missingRoot = join(temporary, "missing-example");
  const missing = check("missing-explicit-root", [fixture, missingRoot]);
  assert.notEqual(missing.status, 0, missing.output);
  assert.ok(missing.output.includes(missingRoot), missing.output);
  checks.push("An explicit missing example root fails and identifies its path.");

  // Build the key at runtime so this test does not count as a literal use of it.
  const removedKey = ["orgHealthDemo", "notProvided"].join(".");
  let replacements = 0;
  for (const file of ["app.js", "index.html"]) {
    const path = join(fixture, file);
    const text = readFileSync(path, "utf8");
    replacements += text.split(removedKey).length - 1;
    writeFileSync(path, text.replaceAll(removedKey, "fixture.removedTranslation"));
  }
  assert.ok(replacements > 0, "The unused-key fixture must remove a real source reference.");
  const unused = check("unused-key-from-external-example", [fixture]);
  assert.notEqual(unused.status, 0, unused.output);
  assert.ok(unused.output.includes(`${removedKey} is not used`), unused.output);
  checks.push("Removing an external source reference still fails the unused catalog key check.");
} catch (cause) {
  error = cause instanceof Error ? (cause.stack ?? cause.message) : String(cause);
  process.exitCode = 1;
} finally {
  mkdirSync(dirname(reportPath), { recursive: true });
  writeFileSync(reportPath, `${JSON.stringify({ ok: error === null, checks, runs, error }, null, 2)}\n`);
  rmSync(temporary, { recursive: true, force: true });
}

if (error !== null) logger.error(error);
else logger.info(`Passed ${checks.length} catalog checker E2E checks. Report: ${reportPath}`);
