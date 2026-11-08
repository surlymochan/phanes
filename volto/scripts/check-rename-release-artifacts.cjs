// Check release artifacts and retained install identities without a build or service connection.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");
const { spawnSync } = require("node:child_process");
const vm = require("node:vm");
const root = path.resolve(__dirname, "../..");
const req = createRequire(path.join(root, "volto/package.json"));
const { parse } = req("yaml");
req("app-builder-lib");
const { PlatformPackager } = req("app-builder-lib/out/platformPackager");
const { LinuxPackager } = req("app-builder-lib/out/linuxPackager");
const { Platform } = req("app-builder-lib/out/core");
const { Arch } = req("builder-util");
const metadata = JSON.parse(fs.readFileSync(path.join(root, "volto/package.json"), "utf8"));
const config = parse(fs.readFileSync(path.join(root, "volto/electron-builder.yml"), "utf8"));
const checks = [];
function check(name, actual, expected) {
  assert.deepEqual(actual, expected, name);
  checks.push({ name, actual, passed: true });
}
const info = { metadata, config };
for (const [platform, extensions, archs] of [
  [Platform.MAC, ["dmg", "zip"], ["arm64", "x64"]],
  [Platform.WINDOWS, ["exe"], ["x64"]],
  [Platform.LINUX, ["AppImage"], ["x64", "arm64"]],
]) {
  const packager = platform === Platform.LINUX ? new LinuxPackager(info) : new PlatformPackager(info, platform);
  for (const ext of extensions)
    for (const arch of archs) {
      const filenameArch = platform === Platform.LINUX && arch === "x64" ? "x86_64" : arch;
      check(
        `${platform.name} ${ext} ${arch} artifact`,
        packager.expandArtifactNamePattern(null, ext, Arch[arch]),
        `Volto-${metadata.version}-${filenameArch}.${ext}`,
      );
    }
  check(`${platform.name} product name`, packager.appInfo.productName, "OpenBot");
  check(`${platform.name} app ID`, packager.appInfo.id, "app.openbot.desktop");
  if (platform === Platform.LINUX) check("Linux installed executable", packager.executableName, "openbot");
}
check("Packaged manifest product name", metadata.productName, "OpenBot");
check("Packaged manifest name", metadata.name, "volto");
check("Registered protocol", config.protocols[0].schemes, ["openbot"]);
for (const file of ["volto/scripts/install-server.sh", "volto/scripts/hosting/openbot-hosted-update"]) {
  const text = fs.readFileSync(path.join(root, file), "utf8");
  const guard = text.match(/case "\$path" in\n[\s\S]*?\nesac|case "\$path" in\n[\s\S]*?\n {2}esac/)[0];
  for (const arch of ["x86_64", "arm64"])
    for (const [name, valid] of [
      [`Volto-0.52.0-${arch}.AppImage`, true],
      [`OpenBot-0.52.0-${arch}.AppImage`, true],
      [`Volto-0.51.0-${arch}.AppImage`, false],
      [`Other-0.52.0-${arch}.AppImage`, false],
      [`../Volto-0.52.0-${arch}.AppImage`, false],
    ]) {
      const result = spawnSync(
        "/bin/bash",
        [
          "-c",
          `fail() { exit 1; }\nversion=0.52.0\narchitecture="$1"\npath="$2"\n${guard}`,
          "manifest-guard",
          arch,
          name,
        ],
        {
          env: {
            PATH: "/usr/bin:/bin",
            HOME: path.join(root, ".tmp/test-home"),
            TMPDIR: path.join(root, ".tmp/test-tmp"),
          },
          encoding: "utf8",
        },
      );
      check(`${file}: ${name}`, result.status === 0, valid);
    }
}
const mac = fs.readFileSync(path.join(root, "volto/scripts/host-manager-macos.ts"), "utf8");
const selection = mac.match(/const assetName = \[.*?\n {6}\);\n {6}if \(!assetName\) throw new Error\([^\n]+\);/s)[0];
for (const [names, expected] of [
  [["Volto-0.52.0-arm64.dmg"], "Volto-0.52.0-arm64.dmg"],
  [["OpenBot-0.52.0-arm64.dmg"], "OpenBot-0.52.0-arm64.dmg"],
  [["OpenBot-0.52.0-arm64.dmg", "Volto-0.52.0-arm64.dmg"], "Volto-0.52.0-arm64.dmg"],
  [["Volto-0.51.0-arm64.dmg"], null],
  [["https://example.invalid/image.dmg"], null],
]) {
  let selected = null;
  try {
    selected = vm.runInNewContext(`${selection}\nassetName;`, {
      version: "0.52.0",
      release: { assets: names.map((name) => ({ name })) },
    });
  } catch (error) {
    if (expected !== null) throw error;
    assert.equal(error.message, "The release has no macOS ARM64 disk image.");
  }
  check(`macOS asset selection ${names.join(", ")}`, selected, expected);
}
const workflow = fs.readFileSync(path.join(root, "volto/.github/workflows/release.yml"), "utf8");
check(
  "macOS release checksum consumes Volto artifacts",
  workflow.includes(
    `artifacts=("Volto-$RELEASE_VERSION-\${{ matrix.arch }}.dmg" "Volto-$RELEASE_VERSION-\${{ matrix.arch }}.zip")`,
  ),
  true,
);
check(
  "AppArmor supports both filenames",
  fs.readFileSync(path.join(root, "volto/build/linux/openbot.apparmor"), "utf8").includes("/{OpenBot,Volto}*.AppImage"),
  true,
);
const report = {
  scope:
    "Offline electron-builder artifact expansion, installed identity and extracted manifest guards. No package build, OS registration, keychain access or service request.",
  checks,
  passed: true,
};
const output = path.join(root, "volto/.volto-build/rename-release-artifacts/report.json");
fs.mkdirSync(path.dirname(output), { recursive: true });
fs.writeFileSync(output, `${JSON.stringify(report, null, 2)}\n`);
process.stdout.write(
  `${JSON.stringify({
    passed: true,
    checks: checks.length,
    report: "volto/.volto-build/rename-release-artifacts/report.json",
  })}\n`,
);
