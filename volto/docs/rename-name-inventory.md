# Volto remaining-name inventory

Baseline: `7869dad74`. Reproduce from the repository root:

```sh
bun volto/scripts/rename-name-audit.ts
```

The command audits `git grep -in openbot`: **5890 matching lines**, **0 lines requiring review**.

The scope is tracked working-tree content. Git excludes untracked reports, ignored build artifacts and the nested baseline worktree. After these reports are tracked, rerun the command to include their self-references. Counts refer to matching lines, not every occurrence; the table sums to the total.

| Category | Lines | Verified reason |
| --- | ---: | --- |
| audit-evidence-and-compatibility-checks | 1802 | Tracked review evidence, audit rules and rename compatibility checks; counted explicitly. |
| compatibility-explanation | 41 | Comments that explain why released values remain. |
| external-github-app-identity | 16 | The existing external GitHub App and bot identity. |
| external-github-release-environment | 1 | The existing GitHub OIDC release subject. |
| external-repository-and-domain | 777 | External repositories, URLs, deployed domains and endpoint fixtures. |
| external-social-account | 2 | The existing external social account handle; confirmed against the baseline. |
| external-url-query | 2 | Existing external feedback URL query values. |
| legacy-environment-interface | 113 | Old variable names retained at fallback reads, normalization boundaries and process-marker checks. |
| mixed-allowed-categories | 68 | A line has more than one allowed category. The ledger records each occurrence separately. |
| protected-baseline-documents | 6 | The two protected documents match the baseline bytes. |
| release-history | 607 | Historical release notes; both complete files match the baseline bytes. |
| released-app-name-or-cli-contract | 35 | Installed app identity, saved profile names and the parsed CLI status line. |
| released-public-content-slug | 6 | Existing public content URL slugs; confirmed against the same baseline file. |
| released-service-file-or-install-path | 1511 | Installed service names, executables, data directories, socket paths and retained asset paths. |
| released-wire-storage-or-platform-identifier | 794 | Saved keys and filenames, tool namespaces, headers, wire values, cookies and platform IDs. |
| renamed-native-class-in-released-package | 1 | The manifest names the renamed Kotlin implementation class; package and class declarations are checked. |
| unchanged-pinned-runtime-patch | 108 | Hash-pinned native runtime patches; both complete files match the baseline bytes. |

The detailed local ledger is `volto/.volto-build/rename-name-audit/report.json`; unresolved entries are in `needs-review.json` beside it. Each entry gives the repository path, line, occurrence column, category and baseline-token check. Source text and private values are not copied into the ledger.

The audit has no directory-wide runtime allowlist. A retained runtime token must exist verbatim in the corresponding original baseline (`7869dad74`) or merge base (`87c7380daa8904966e14bee592c334d7c600fefb`) file, including source filename aliases created by the rename. New legacy variable fallbacks, explicit compatibility checks and the native implementation class have separate rules. These lexical checks classify names; runtime behavior is covered by the separate compatibility and baseline test reports.
