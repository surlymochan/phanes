import { expect, test } from "bun:test";
import { chmod, mkdir, mkdtemp, stat, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { hostFileMode } from "./host-installation";

const cases = [
  ["host.plist", 0o644],
  ["host.PLIST", 0o644],
  ["host.PlIsT", 0o644],
  ["release.json", 0o644],
  ["release.JSON", 0o644],
  ["release.JsOn", 0o644],
  ["host-manager", 0o755],
  ["openbot-relaunch.sh", 0o755],
  ["host.plist.bak", 0o755],
  ["release.jsonl", 0o755],
  ["host-plist", 0o755],
] as const;

test.each(cases)("sets the installed mode for %s", async (name, expectedMode) => {
  const artifacts = join(import.meta.dir, "../.volto-build/host-installation-e2e");
  await mkdir(artifacts, { recursive: true });
  const directory = await mkdtemp(join(artifacts, "payload-"));
  const path = join(directory, name);
  await writeFile(path, "fixture");
  await chmod(path, hostFileMode(path));
  expect((await stat(path)).mode & 0o7777).toBe(expectedMode);
});
