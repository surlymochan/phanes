REPORT: ready — Release filenames and installation instructions agree. Baseline and current checks were reproduced with no new failures.

| Final review finding | Fix | Evidence from this worktree |
| --- | --- | --- |
| Download names differ from the README | Set the release artifact prefix to `Volto`. Update checksum and release consumers. Keep old and new manifest names valid for server installation and updates. | 44 offline checks use the real electron-builder filename expansion and Linux executable resolution, exercise both shell guards, and check macOS asset selection. All seven documented desktop artifact names match. |
| Installed identity and deep links | Keep the installed app name, bundle ID, keychain services, data paths and released link scheme. Set the Linux executable name explicitly so package-name changes do not break installed helpers. Explain these retained identities in the README. | The same artifact check verifies the installed identity and protocol. The runtime audit passes 91 checks; contract and storage E2E scripts pass five and four behavior groups. No real keychain is accessed. |
| Remaining-name count was stale | Regenerate the inventory and synchronize this report and the JSON evidence after all files are included. New compatibility rules check their exact file and baseline value. | The name audit reports no unclassified lines and no changes to protected baseline files. See the count and categories below. |
| Baseline comparison was not reproducible | Create a detached baseline worktree at `7869dad74`, install both roots, and run the same checks sequentially. | Both trees pass all 15 typechecks, 1,146 desktop/mobile tests and 23 API tests. Both failure lists are empty. |

## Baseline comparison

The baseline is `.tmp/main-baseline`, created with `git worktree add --detach .tmp/main-baseline 7869dad74`. The package roots are `.tmp/main-baseline/openbot` and `volto`, based on `main-latest` (`87c7380daa`). Bun is 1.4.2 and Node is 24.14.1.

Both roots pass `bun install --frozen-lockfile --ignore-scripts`; the baseline install completed on retry after its first attempt stalled on an Expo artifact. Install hooks are disabled; both lockfiles remain unchanged. All checks run one at a time. Unit suites use one worker. Every check receives the same clean environment without provider or control-plane credentials, an isolated test home and local caches. `TMPDIR` points to `.tmp/test-tmp` inside this worktree. Desktop tests also apply the existing hermetic profile setup. The baseline working tree is clean after the checks.

| Check | Baseline | Current | New failures |
| --- | ---: | ---: | ---: |
| All 15 `typecheck:*` projects, sequentially | 15 passed | 15 passed | 0 |
| Desktop/mobile unit tests | 1,146 passed | 1,146 passed | 0 |
| Account API unit tests | 23 passed | 23 passed | 0 |

**Baseline failures:** `[]`. **Current failures:** `[]`. The [validation JSON](rename-validation.json) records actual commands, exit codes, counts and local evidence paths. Detailed command logs and Vitest JSON reports remain under ignored `.tmp/verify/`.

To repeat the comparison in a fresh worktree, create the baseline above and run these commands in **each** package root with the same clean test environment and an absolute worktree-local `TMPDIR`:

```sh
bun install --frozen-lockfile --ignore-scripts
for project in node renderer storybook api brand contracts logging user-errors i18n sites team-client mobile remote status telemetry; do
  bun run "typecheck:$project" || exit 1
done
bun run test:desktop --maxWorkers=1 --reporter=json --outputFile=<absolute-evidence-file>
bun run test:api --maxWorkers=1 --reporter=json --outputFile=<absolute-evidence-file>
```

The explicit baseline comparison is required by this task. It replaces the usual project default of focused local checks. No aggregate parallel check was run.

## Remaining names

<!-- name-counts:start -->
Command: `git grep -in openbot`. **5,890 matching tracked lines; 0 unclassified lines.**

| Retained category | Matching lines |
| --- | ---: |
| audit-evidence-and-compatibility-checks | 1802 |
| compatibility-explanation | 41 |
| external-github-app-identity | 16 |
| external-github-release-environment | 1 |
| external-repository-and-domain | 777 |
| external-social-account | 2 |
| external-url-query | 2 |
| legacy-environment-interface | 113 |
| mixed-allowed-categories | 68 |
| protected-baseline-documents | 6 |
| release-history | 607 |
| released-app-name-or-cli-contract | 35 |
| released-public-content-slug | 6 |
| released-service-file-or-install-path | 1511 |
| released-wire-storage-or-platform-identifier | 794 |
| renamed-native-class-in-released-package | 1 |
| unchanged-pinned-runtime-patch | 108 |
<!-- name-counts:end -->

The generated [inventory](rename-name-inventory.md) and local per-occurrence ledger classify the exact output of `git grep -in openbot`. This count includes the Volto rename, main-latest additions and tracked audit reports. Ignored dependencies, temporary files and the nested baseline worktree are excluded by Git.

## Focused verification

Run from the repository root, with `TMPDIR` set to the absolute `.tmp/test-tmp` directory:

```sh
node volto/scripts/check-rename-release-artifacts.cjs
python3 volto/scripts/check-rename-external-compat.py
python3 volto/scripts/check-rename-runtime-compat.py
python3 volto/scripts/check-rename-asset-paths.py
bun volto/scripts/rename-name-audit.ts
```

The external/mobile, runtime and asset/source checks pass 94, 91 and 392 checks respectively. From `volto/`, these also pass:

```sh
bun scripts/environment-compatibility-e2e.ts
bun scripts/rename-contracts-e2e.ts
bun scripts/rename-storage-e2e.ts
```

The environment check covers three isolated process cases and 69 production reads. The storage check opens existing database data, writes, reopens it and checks the real isolated database-host boundary. Protected handoff, architecture, changelog and native-runtime-lock files match baseline bytes. See the validation JSON for their hashes.

The rename-name audit reports zero unclassified lines. Focused Swrm workspace, hub bridge, CI gate and upstream drift tests pass. No push was performed. No real control plane, deployment, account, device or installed app was used. Signed native packages, OS protocol registration, actual keychain access and live deployed-service behavior remain untested.
