// @vitest-environment node

import { mkdtemp, rm } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import type { ProviderRuntimeSnapshot } from "@openbot/contracts/ipc";
import { teeLogLines } from "@openbot/logging";
import { afterEach, describe, expect, it } from "vitest";
import { runCauseEffect } from "../backend/effect-boundary";
import { ProviderRuntimeManager } from "./provider-runtime-manager";

const cleanups: Array<() => Promise<void> | void> = [];

afterEach(async () => {
  for (const cleanup of cleanups.splice(0).reverse()) await cleanup();
});

describe("provider runtime failure log", () => {
  it("names the stage and version, and keeps secrets and the home directory out of the line", async () => {
    const root = await mkdtemp(join(tmpdir(), "openbot-runtime-failure-log-"));
    cleanups.push(() => rm(root, { recursive: true, force: true }));
    const lines: string[] = [];
    cleanups.push(teeLogLines(["provider-runtimes"], (line) => lines.push(line)));
    const secret = "sk-ant-0123456789abcdef";
    const manager = new ProviderRuntimeManager({
      root,
      platform: "linux",
      architecture: "x64",
      availableDiskBytes: async () => Number.MAX_SAFE_INTEGER,
      fetchImpl: async () => {
        throw new TypeError("fetch failed", {
          cause: new Error(`open ${join(homedir(), "proxy.conf")} failed with apiKey=${secret}`),
        });
      },
    });
    cleanups.push(() => runCauseEffect(manager.stop()));
    const failed = new Promise<ProviderRuntimeSnapshot>((resolve) => {
      manager.on("status", (snapshot) => {
        if (snapshot.providers.antigravity.phase === "download-error") resolve(snapshot);
      });
    });

    await runCauseEffect(manager.download("antigravity"));
    await failed;

    const line = lines.find((entry) => entry.includes("Provider runtime update failed."));
    expect(line).toBeDefined();
    expect(line).toContain('"runtime":"antigravity"');
    expect(line).toContain('"stage":"download"');
    expect(line).toMatch(/"version":"[^"]+"/u);
    expect(line).toContain("~/proxy.conf");
    expect(line).not.toContain(homedir());
    expect(line).not.toContain(secret);
    expect(line).not.toContain('"stack"');
  });
});
