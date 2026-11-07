// Probe only safeStorage. Do not load application services or the control plane.
import assert from "node:assert/strict";
import { createCipheriv, pbkdf2Sync } from "node:crypto";
import { writeFileSync } from "node:fs";
import { app, safeStorage } from "electron";
import { configureElectronSafeStorage } from "../src/main/electron-safe-storage";

function argument(name: string): string {
  const value = process.argv.find((item) => item.startsWith(`--${name}=`));
  if (!value) throw new Error(`Missing --${name}`);
  return value.slice(name.length + 3);
}

app.setPath("userData", argument("profile"));
const readyBeforeConfiguration = app.isReady();
assert.equal(readyBeforeConfiguration, false);
// The packaged policy is injected; the probe itself is an unpackaged Electron entry.
if (argument("mode") !== "launch") {
  configureElectronSafeStorage(app.commandLine, process.platform, argument("packaged") === "true", process.env);
}
const isolated = argument("isolated") === "true";
const selected = process.platform === "darwin" ? "use-mock-keychain" : "password-store";
assert.equal(app.commandLine.hasSwitch(selected), isolated);
if (isolated && process.platform === "linux") assert.equal(app.commandLine.getSwitchValue(selected), "basic");

void app.whenReady().then(() => {
  // Never query encryption for a production-policy case: that could open the real keychain.
  let roundTrip: boolean | null = null;
  let backend: string | null = null;
  if (isolated) {
    if (process.platform === "linux") {
      backend = safeStorage.getSelectedStorageBackend();
      assert.equal(backend, "basic_text");
    }
    assert.equal(safeStorage.isEncryptionAvailable(), true);
    const encrypted = safeStorage.encryptString("isolated-probe");
    if (process.platform === "darwin") {
      // macOS has no backend query. Match Chromium's mock-key ciphertext independently:
      // https://github.com/chromium/chromium/blob/130.0.6723.0/crypto/mock_apple_keychain.cc
      // https://github.com/chromium/chromium/blob/130.0.6723.0/components/os_crypt/sync/os_crypt_mac.mm
      const key = pbkdf2Sync("mock_password", "saltysalt", 1003, 16, "sha1");
      const cipher = createCipheriv("aes-128-cbc", key, Buffer.alloc(16, " "));
      const expected = Buffer.concat([Buffer.from("v10"), cipher.update("isolated-probe"), cipher.final()]);
      assert.deepEqual(encrypted, expected, "safeStorage must use Chromium's mock key");
      backend = "mock_keychain";
    }
    roundTrip = safeStorage.decryptString(encrypted) === "isolated-probe";
    assert.equal(roundTrip, true);
  }
  writeFileSync(argument("report"), JSON.stringify({ readyBeforeConfiguration, isolated, backend, roundTrip }));
  app.quit();
});
