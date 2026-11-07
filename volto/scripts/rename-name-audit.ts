// Audit retained names without a service connection. Run from volto: bun scripts/rename-name-audit.ts
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "../..");
const baseline = "7869dad74";
const mergeBase = "87c7380daa8904966e14bee592c334d7c600fefb";
const pattern = /[A-Za-z0-9_./:@+%-]*openbot[A-Za-z0-9_./:@+%-]*/giu;
function git(args: string[]) {
  const result = spawnSync("git", args, { cwd: root, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
  if (result.status !== 0 && result.status !== 1) throw new Error(result.stderr);
  return result.stdout;
}
function entries(output: string) {
  return output
    .split("\n")
    .filter(Boolean)
    .map((entry) => {
      const match = /^([^:]+):(\d+):(.*)$/u.exec(entry);
      if (!match) throw new Error("Unexpected grep output");
      return { path: match[1] ?? "", line: Number(match[2]), text: match[3] ?? "" };
    });
}
const original = new Map<string, { tokens: Set<string>; lines: Set<string> }>();
for (const reference of [baseline, mergeBase]) {
  for (const entry of entries(git(["grep", "-in", "openbot", reference, "--"]).replaceAll(`${reference}:`, ""))) {
    const path = entry.path.replace(/^openbot\//u, "volto/");
    const record = original.get(path) ?? { tokens: new Set<string>(), lines: new Set<string>() };
    for (const match of entry.text.matchAll(pattern)) record.tokens.add(match[0]);
    record.lines.add(entry.text);
    original.set(path, record);
    // Source files changed their product name as well as their workspace directory.
    original.set(path.replaceAll("openbot", "volto"), record);
  }
}
const evidencePaths = new Set([
  "TASKS.md",
  "REPORT.md",
  "volto/docs/rename-review.md",
  "volto/docs/rename-name-inventory.md",
  "volto/scripts/rename-name-audit.ts",
  "volto/scripts/check-rename-external-compat.py",
  "volto/scripts/environment-compatibility-e2e.ts",
  "volto/scripts/rename-contracts-e2e.ts",
  "volto/scripts/rename-storage-e2e.ts",
  "volto/scripts/check-rename-runtime-compat.py",
  "volto/scripts/check-rename-asset-paths.py",
  "volto/scripts/check-rename-release-artifacts.cjs",
  "volto/docs/rename-validation.json",
  "volto/docs/rename-changed-files.json",
]);
const exactFiles = new Map(
  [
    "HANDOFF.md",
    "volto/docs/ARCHITECTURE.md",
    "volto/CHANGELOG.md",
    "volto/apps/mobile/CHANGELOG.md",
    "volto/vendor/remote-desktop/patches/moonlight-web-stream-v2.10.0-openbot.patch",
    "volto/vendor/remote-desktop/patches/sunshine-v2026.516.143833-openbot.patch",
  ].map((path) => [
    path,
    readFileSync(resolve(root, path), "utf8") ===
      git([
        "show",
        `${path === "HANDOFF.md" || path === "volto/docs/ARCHITECTURE.md" ? mergeBase : baseline}:${path.replace(/^volto\//u, "openbot/")}`,
      ]),
  ]),
);
const nativeClass = readFileSync(
  resolve(
    root,
    "volto/apps/mobile/modules/android-notifications/android/src/main/java/run/openbot/mobile/notifications/OpenBotMessagingService.kt",
  ),
  "utf8",
);
function classify(path: string, text: string, token: string, retained: boolean) {
  if (evidencePaths.has(path)) return "audit-evidence-and-compatibility-checks";
  if (path.endsWith("CHANGELOG.md")) return exactFiles.get(path) ? "release-history" : "needs-review";
  if (path === "HANDOFF.md" || path === "volto/docs/ARCHITECTURE.md")
    return exactFiles.get(path) ? "protected-baseline-documents" : "needs-review";
  if (path.startsWith("volto/vendor/remote-desktop/patches/") && exactFiles.get(path))
    return "unchanged-pinned-runtime-patch";
  if (path === "upstream-sync.json" && text.includes('"upstream_repo"') && retained)
    return "external-repository-and-domain";
  if (
    /Keep released OpenBot|released OpenBot page API|released.*(?:file name|identif)|legacy.*(?:fallback|compatib)/iu.test(
      text,
    )
  )
    return "compatibility-explanation";
  if (/OPENBOT_[A-Z_0-9]*|EXPO_PUBLIC_OPENBOT/u.test(token) && /(?:VOLTO_|fallback|remains supported)/u.test(text))
    return "legacy-environment-interface";
  const variable = /OPENBOT_[A-Z_0-9]+/u.exec(token)?.[0];
  if (variable && [...(original.get(path)?.tokens ?? [])].some((value) => value.includes(variable)))
    return "legacy-environment-interface";
  if (token === "OPENBOT" && text.includes("(VOLTO|OPENBOT)")) return "legacy-environment-interface";
  if (
    token === "run.openbot.mobile.notifications.VoltoMessagingService" &&
    nativeClass.includes("package run.openbot.mobile.notifications") &&
    nativeClass.includes("class VoltoMessagingService :")
  )
    return "renamed-native-class-in-released-package";
  if (
    path === "volto/README.md" &&
    token === "app.openbot.desktop" &&
    original.get("volto/electron-builder.yml")?.tokens.has(token)
  )
    return "compatibility-explanation";
  if (
    path === "volto/changelog.d/rename-compatibility.md" &&
    token === "OpenBot" &&
    text.includes("Keep the installed `OpenBot` application identity") &&
    original.get("volto/electron-builder.yml")?.tokens.has(token)
  )
    return "compatibility-explanation";
  if (
    path === "volto/scripts/host-manager-macos.ts" &&
    token === "OpenBot-" &&
    /`OpenBot-\$\{version\}-arm64\.dmg`/u.test(text) &&
    [...(original.get(path)?.lines ?? [])].some((line) => /\/OpenBot-\$\{version\}-arm64\.dmg/u.test(line))
  )
    return "released-service-file-or-install-path";
  if (!retained) return "needs-review";
  if (path === "volto/README.md" && /^(?:OpenBot|OpenBot\.)$/u.test(token) && original.get(path)?.lines.has(text))
    return "released-app-name-or-cli-contract";
  if (
    path === "volto/build/linux/openbot.apparmor" &&
    token === "OpenBot" &&
    text.includes("/{OpenBot,Volto}*.AppImage")
  )
    return "released-service-file-or-install-path";
  if (token === "@OpenBot_") return "external-social-account";
  if (/^(?:introducing-openbot|wtf-is-openbot|write-a-guide-for-openbot)$/u.test(token))
    return "released-public-content-slug";
  if (/openbot-(?:github|computer-use)$|openbot_(?:plan|interval|license_prices)/u.test(token))
    return "released-wire-storage-or-platform-identifier";
  if (/openbotgit/iu.test(token)) return "external-github-app-identity";
  if (
    /(?:https?:\/\/|wss:\/\/|git\+https:|mailto:|github\.com\/|githubusercontent\.com\/|ghcr\.io\/|nightly-labs\/|nibiru-labs\/)|openbot\.(?:run|site|app|test|invalid|example|localhost|local)\b|openbot-.*\.(?:workers\.dev|firebasestorage\.app)/iu.test(
      token,
    )
  )
    return "external-repository-and-domain";
  if (/OPENBOT_[A-Z_0-9]*|EXPO_PUBLIC_OPENBOT/u.test(token)) return "legacy-environment-interface";
  if (
    /(?:openbot(?:_browser|[.:/])|mcp__openbot|x-openbot-|OpenBot-(?:Host|Hosting)|application\/x-openbot|__openbot_|openbot_(?:remote|metadata|kind|license|paid|user|server|starter)|run\.openbot|app\.openbot|group\.run\.openbot|OpenBotAppGroup|window\.openbotPage|x-scheme-handler\/openbot|openbotVersion|openbotRemoteViewer)/u.test(
      token,
    )
  )
    return "released-wire-storage-or-platform-identifier";
  if (/(?:^|\/)\.?openbot\.?$|^br-openbot$|^installed_user:-openbot$|^URL:OpenBot$/u.test(token))
    return "released-service-file-or-install-path";
  if (/openbot[-_]|OpenBot(?:\.app|\.exe|\.AppImage|-)|[/.]OpenBot|OpenBot\//u.test(token))
    return "released-service-file-or-install-path";
  if (
    token === "OpenBot" &&
    /(?:["'`]OpenBot["'`]|\\OpenBot|OpenBot Dev|productName: OpenBot|OpenBot subfolder|add an OpenBot|name is "OpenBot"|grep -Fxq "OpenBot|echo "OpenBot \$\(value version\))/u.test(
      text,
    )
  )
    return "released-app-name-or-cli-contract";
  if (token.startsWith("Feedback%20for%20OpenBot%20")) return "external-url-query";
  if (token.startsWith("repo:nightly-labs@") && token.includes("/openbot@"))
    return "external-github-release-environment";
  if (token === ".openbot" || token === "u0000openbot-avatar-seed-")
    return "released-wire-storage-or-platform-identifier";
  if (
    /^(?:openbot|openbot:|openbot\.|:openbot|\/OpenBot|OpenBot\.|openbot\/|\/openbot|1\/openbot|local\/openbot|[A-Z_]+\/openbot)$/u.test(
      token,
    ) &&
    /(?:["'`:/= ]openbot["'` :\n]|service_user|user:|name:|sqlite|dirname|SOURCE|binary|executable|process|mcp|tool|chmod|chown|ln -s|install |ENTRYPOINT|entrypoint|COPY |ARG |ENV |USER |label|sudo|local\/bin)/iu.test(
      text,
    )
  )
    return "released-service-file-or-install-path";
  if (/openbot(?:Docker|Restrict|Safe|Version)/u.test(token)) return "needs-review";
  return "needs-review";
}
const counts: Record<string, number> = {};
const occurrenceCounts: Record<string, number> = {};
const lines = entries(git(["grep", "-in", "openbot"])).map(({ path, line, text }) => {
  const tokens = [...text.matchAll(pattern)].map((match) => {
    const retained = original.get(path)?.tokens.has(match[0]) ?? false;
    const category = classify(path, text, match[0], retained);
    occurrenceCounts[category] = (occurrenceCounts[category] ?? 0) + 1;
    // Do not copy source text, URLs, account labels, or other private values into evidence.
    return { column: (match.index ?? 0) + 1, category, presentInBaseline: retained };
  });
  const categories = [...new Set(tokens.map((token) => token.category))];
  const category = categories.includes("needs-review")
    ? "needs-review"
    : categories.length === 1
      ? (categories[0] ?? "needs-review")
      : "mixed-allowed-categories";
  counts[category] = (counts[category] ?? 0) + 1;
  return { path, line, category, categories, occurrences: tokens };
});
const output = resolve(root, "volto/.volto-build/rename-name-audit");
await mkdir(output, { recursive: true });
const report = {
  command: "git grep -in openbot",
  baseline,
  scope:
    "Tracked working-tree content, including tracked audit evidence. Untracked files and ignored baseline worktrees are excluded by git grep.",
  method:
    "Classify each occurrence by syntax and purpose. Runtime identifiers must exist verbatim in the same baseline file. No directory-wide runtime allowlist. Every line and occurrence has an inventory entry; unknown values fail the check.",
  total: lines.length,
  counts,
  occurrenceCounts,
  exactBaselineFiles: Object.fromEntries(exactFiles),
  lines,
};
await writeFile(resolve(output, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
const unknown = lines.filter((line) => line.category === "needs-review");
await writeFile(resolve(output, "needs-review.json"), `${JSON.stringify(unknown, null, 2)}\n`);
const descriptions: Record<string, string> = {
  "release-history": "Historical release notes; both complete files match the baseline bytes.",
  "protected-baseline-documents": "The two protected documents match the baseline bytes.",
  "unchanged-pinned-runtime-patch": "Hash-pinned native runtime patches; both complete files match the baseline bytes.",
  "external-repository-and-domain": "External repositories, URLs, deployed domains and endpoint fixtures.",
  "released-wire-storage-or-platform-identifier":
    "Saved keys and filenames, tool namespaces, headers, wire values, cookies and platform IDs.",
  "released-service-file-or-install-path":
    "Installed service names, executables, data directories, socket paths and retained asset paths.",
  "legacy-environment-interface":
    "Old variable names retained at fallback reads, normalization boundaries and process-marker checks.",
  "released-app-name-or-cli-contract": "Installed app identity, saved profile names and the parsed CLI status line.",
  "external-github-app-identity": "The existing external GitHub App and bot identity.",
  "external-github-release-environment": "The existing GitHub OIDC release subject.",
  "external-url-query": "Existing external feedback URL query values.",
  "renamed-native-class-in-released-package":
    "The manifest names the renamed Kotlin implementation class; package and class declarations are checked.",
  "compatibility-explanation": "Comments that explain why released values remain.",
  "audit-evidence-and-compatibility-checks":
    "Tracked review evidence, audit rules and rename compatibility checks; counted explicitly.",
  "external-social-account": "The existing external social account handle; confirmed against the baseline.",
  "released-public-content-slug": "Existing public content URL slugs; confirmed against the same baseline file.",
  "mixed-allowed-categories":
    "A line has more than one allowed category. The ledger records each occurrence separately.",
  "needs-review": "An occurrence has no verified classification; this makes the audit fail.",
};
const inventory = [
  "# Volto remaining-name inventory",
  "",
  `Baseline: \`${baseline}\`. Reproduce from the repository root:`,
  "",
  "```sh",
  "bun volto/scripts/rename-name-audit.ts",
  "```",
  "",
  `The command audits \`git grep -in openbot\`: **${lines.length} matching lines**, **${unknown.length} lines requiring review**.`,
  "",
  "The scope is tracked working-tree content. Git excludes untracked reports, ignored build artifacts and the nested baseline worktree. After these reports are tracked, rerun the command to include their self-references. Counts refer to matching lines, not every occurrence; the table sums to the total.",
  "",
  "| Category | Lines | Verified reason |",
  "| --- | ---: | --- |",
  ...Object.entries(counts)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(
      ([category, count]) => `| ${category} | ${count} | ${descriptions[category] ?? "Review the detailed ledger."} |`,
    ),
  "",
  "The detailed local ledger is `volto/.volto-build/rename-name-audit/report.json`; unresolved entries are in `needs-review.json` beside it. Each entry gives the repository path, line, occurrence column, category and baseline-token check. Source text and private values are not copied into the ledger.",
  "",
  `The audit has no directory-wide runtime allowlist. A retained runtime token must exist verbatim in the corresponding original baseline (\`${baseline}\`) or merge base (\`${mergeBase}\`) file, including source filename aliases created by the rename. New legacy variable fallbacks, explicit compatibility checks and the native implementation class have separate rules. These lexical checks classify names; runtime behavior is covered by the separate compatibility and baseline test reports.`,
  "",
].join("\n");
await writeFile(resolve(root, "volto/docs/rename-name-inventory.md"), inventory);
const changedProtectedFiles = [...exactFiles].filter(([, matches]) => !matches).map(([path]) => path);
process.stdout.write(
  `${JSON.stringify({ total: report.total, counts, occurrenceCounts, changedProtectedFiles, needsReview: unknown.map(({ path, line }) => ({ path, line })) })}\n`,
);
if (unknown.length || changedProtectedFiles.length) process.exitCode = 1;
