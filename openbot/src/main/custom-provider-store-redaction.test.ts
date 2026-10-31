// @vitest-environment node

import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { SaveCustomProviderInput } from "@openbot/contracts/ipc";
import { redactText } from "@openbot/logging";
import { Effect } from "effect";
import { afterEach, describe, expect, it } from "vitest";
import { CustomProviderStore } from "./custom-provider-store";

const directories: string[] = [];
const cipher = {
  canPersist: () => true,
  encrypt: (value: string) => Buffer.from(value),
  decrypt: (value: Buffer) => value.toString("utf8"),
};

afterEach(async () => {
  await Promise.all(directories.splice(0).map((directory) => rm(directory, { recursive: true, force: true })));
});

describe("custom provider secret redaction", () => {
  it("registers API keys and header values when saving a provider", async () => {
    const path = await providerFilePath();
    const store = new CustomProviderStore({ path, cipher });
    const input: SaveCustomProviderInput = {
      id: "local-provider",
      name: "Local provider",
      baseUrl: "https://provider.example/v1",
      apiKey: "custom-api-key-95d7e2b6",
      models: [],
      headers: [{ name: "X-Custom-Token", value: "custom-header-token-72a934fd" }],
    };

    await Effect.runPromise(store.save(input));

    const redacted = redactText(`${input.apiKey} ${input.headers[0]?.value}`);
    expect(redacted).toBe("[redacted] [redacted]");
    expect(redacted).not.toContain(input.apiKey);
    expect(redacted).not.toContain(input.headers[0]?.value);
  });

  it("registers replacement API keys and header values when updating a provider", async () => {
    const path = await providerFilePath();
    const store = new CustomProviderStore({ path, cipher });
    await Effect.runPromise(
      store.save({
        id: "local-provider",
        name: "Local provider",
        baseUrl: "https://provider.example/v1",
        apiKey: "initial-provider-api-key-0877b9b1",
        models: [],
        headers: [{ name: "X-Custom-Token", value: "initial-header-token-19b4f1d2" }],
      }),
    );
    const apiKey = "updated-provider-api-key-8f5ce422";
    const headerValue = "updated-header-token-9a6d1138";

    await Effect.runPromise(
      store.update({
        id: "local-provider",
        name: "Local provider",
        baseUrl: "https://provider.example/v1",
        models: [],
        apiKey,
        headers: [{ name: "X-Custom-Token", value: headerValue }],
      }),
    );

    const redacted = redactText(`${apiKey} ${headerValue}`);
    expect(redacted).toBe("[redacted] [redacted]");
    expect(redacted).not.toContain(apiKey);
    expect(redacted).not.toContain(headerValue);
  });

  it("registers stored API keys and header values when loading a provider", async () => {
    const path = await providerFilePath();
    const apiKey = "loaded-provider-api-key-3b9e42c1";
    const headerValue = "loaded-provider-header-token-9ca6f047";
    const secret = { apiKey, headers: [{ name: "X-Custom-Token", value: headerValue }] };
    await writeFile(
      path,
      JSON.stringify({
        version: 1,
        providers: [
          {
            id: "local-provider",
            name: "Local provider",
            baseUrl: "https://provider.example/v1",
            models: [],
            secret: Buffer.from(JSON.stringify(secret)).toString("base64"),
          },
        ],
      }),
    );
    const store = new CustomProviderStore({ path, cipher });

    await Effect.runPromise(store.load());

    const redacted = redactText(`${apiKey} ${headerValue}`);
    expect(redacted).toBe("[redacted] [redacted]");
    expect(redacted).not.toContain(apiKey);
    expect(redacted).not.toContain(headerValue);
  });
});

async function providerFilePath(): Promise<string> {
  const directory = await mkdtemp(join(tmpdir(), "openbot-custom-provider-redaction-"));
  directories.push(directory);
  return join(directory, "providers.json");
}
