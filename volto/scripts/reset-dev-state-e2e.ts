import { afterAll, beforeAll, expect, test } from "bun:test";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join, parse, resolve } from "node:path";
import { developmentStatePaths, resetDevelopmentState } from "./reset-dev-state";

const originalDirectory = process.cwd();
let sandbox: string;

beforeAll(async () => {
  sandbox = await mkdtemp(join(originalDirectory, "reset-dev-state-e2e-"));
  process.chdir(sandbox);
});

afterAll(async () => {
  process.chdir(originalDirectory);
  if (sandbox) await rm(sandbox, { recursive: true, force: true });
});

for (const root of ["", " ", "\t\n", parse(resolve("/")).root]) {
  test(`refuses unsafe root ${JSON.stringify(root)} before deletion`, async () => {
    const deleted: string[] = [];
    expect(() => developmentStatePaths(root)).toThrow();
    await expect(resetDevelopmentState(root, sandbox, (path) => deleted.push(path))).rejects.toThrow();
    expect(deleted).toEqual([]);
  });
}

test("an empty root cannot delete profiles in the current directory", async () => {
  const profile = join(sandbox, "OpenBot Dev");
  await mkdir(profile, { recursive: true });
  await writeFile(join(profile, "marker"), "keep");
  const deleted: string[] = [];
  await expect(resetDevelopmentState("", sandbox, (path) => deleted.push(path))).rejects.toThrow();
  expect(deleted).toEqual([]);
  expect(await readFile(join(profile, "marker"), "utf8")).toBe("keep");
});

test("deletes only known development profiles from a valid root", async () => {
  const root = join(sandbox, "app-data");
  const names = ["OpenBot Dev", "OpenBot Dev Test Client", "Volto Dev Host"];
  for (const name of [...names, "Other Profile"]) {
    await mkdir(join(root, name), { recursive: true });
    await writeFile(join(root, name, "marker"), name);
  }
  const deleted: string[] = [];
  expect(await resetDevelopmentState(root, sandbox, (path) => deleted.push(path))).toEqual(
    names.map((name) => join(root, name)),
  );
  expect(deleted).toEqual(names.map((name) => join(root, name)));
  for (const name of names) expect(await Bun.file(join(root, name, "marker")).exists()).toBe(false);
  expect(await readFile(join(root, "Other Profile", "marker"), "utf8")).toBe("Other Profile");
});
