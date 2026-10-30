// Checks the OpenBot Contributor Assignment Agreement (CLA.md) on one pull request.
//
//   bun scripts/cla.ts pr <number>   checks one open pull request
//   --dry-run                        writes nothing to GitHub; the decisions go to REPORT_PATH
//
// The author of the pull request and each commit author must sign. Bots, organization members and
// users with write access skip the check, as in `contribution-policy.ts`. The author of the pull
// request signs with a ticked agreement box in the description. The pull request template has no
// box, so that people who do not sign are not asked to tick it. When an author who must sign has no
// box in the description, the script adds the unticked box to the end, so that the author signs with
// one click. Each author can also sign with a comment that contains the signing line of CLA.md. A
// box or a comment counts only when its last editor is the author: a user with write access can edit
// the text of other people.
//
// The script copies each new signature to SIGNATURES_FILE on the SIGNATURES_BRANCH branch, so the
// record stays when the author edits the description or deletes the comment. Each run reads all
// comments again, so a run that GitHub cancels loses no signature: the next run records it. A
// closed pull request is skipped.
//
// No package imports: `cla.yml` runs this before `bun install`. Every GitHub read uses `gh api --jq`.

import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

// Not COLLABORATOR: that includes collaborators with read or triage access, who must sign. The
// permission lookup in `isTrusted` passes collaborators with write access.
const TRUSTED_ASSOCIATIONS = new Set(["OWNER", "MEMBER"]);
const TRUSTED_PERMISSIONS = new Set(["admin", "maintain", "write"]);
const AGREEMENT_PATH = "CLA.md";
const AGREEMENT_NAME = "OpenBot Contributor Assignment Agreement";
const SIGNATURES_BRANCH = "cla-signatures";
const SIGNATURES_FILE = "signatures.json";
const STATUS_CONTEXT = "CLA";
const COMMENT_MARKER = "<!-- cla -->";
const COMMENT_AUTHOR = "github-actions[bot]";
/** The pull request commits endpoint returns at most this number of commits. */
const MAX_COMMITS = 250;
const SAVE_ATTEMPTS = 3;
const REPORT_PATH = ".openbot-build/cla/report.json";

const args = process.argv.slice(2);
const dryRun = args.includes("--dry-run");
const [command, numberArgument] = args.filter((arg) => !arg.startsWith("--"));

const repository =
  process.env.GITHUB_REPOSITORY || gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]);
const agreementUrl = `https://github.com/${repository}/blob/main/${AGREEMENT_PATH}`;
const writes: string[] = [];

function fail(message: string): never {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

function gh(ghArgs: readonly string[], input?: string): string {
  return execFileSync("gh", ghArgs, {
    encoding: "utf8",
    input,
    stdio: ["pipe", "pipe", "pipe"],
    maxBuffer: 64 * 1024 * 1024,
  }).replace(/\n+$/, "");
}

function read(path: string, jq: string): string {
  return gh(["api", path, "--jq", jq]);
}

function readLines(path: string, jq: string): string[] {
  return gh(["api", "--paginate", path, "--jq", jq])
    .split("\n")
    .filter((line) => line !== "");
}

/** `gh` prints the HTTP status on standard error when a request fails. */
function failedWith(error: unknown, statuses: readonly number[]): boolean {
  const stderr = error instanceof Error && "stderr" in error ? String(error.stderr) : "";
  return statuses.some((status) => stderr.includes(`HTTP ${status}`));
}

/** One column of a `@tsv` row. A missing column is a script error. */
function column(values: readonly string[], index: number): string {
  const value = values[index];
  if (value === undefined) fail(`Unexpected value from GitHub: ${values.join("\t")}`);
  return value;
}

function isTrusted(login: string, association: string): boolean {
  if (TRUSTED_ASSOCIATIONS.has(association)) return true;
  // `author_association` can miss a private organization member. The permission lookup does not,
  // and it fails closed: an error treats the author as external.
  try {
    return TRUSTED_PERMISSIONS.has(read(`repos/${repository}/collaborators/${login}/permission`, ".permission"));
  } catch {
    return false;
  }
}

/** Every change to GitHub goes through here, so `--dry-run` only records it. */
function write(description: string, ghArgs: readonly string[], input?: string): void {
  writes.push(description);
  if (!dryRun) gh(ghArgs, input);
}

function parseNumber(value: string | undefined): number {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number <= 0) fail(`Expected a pull request number, got "${value}".`);
  return number;
}

/** The version of CLA.md and the line that signs it. The script fails when CLA.md does not contain the line. */
function loadAgreement(): { readonly version: string; readonly signingLine: string } {
  const text = readFileSync(AGREEMENT_PATH, "utf8");
  const version = /^Version: (\S+)$/m.exec(text)?.[1];
  if (version === undefined) fail(`${AGREEMENT_PATH} has no "Version:" line.`);
  const signingLine = `I have read the ${AGREEMENT_NAME} ${version} and I agree to it.`;
  if (!text.includes(signingLine)) fail(`${AGREEMENT_PATH} does not contain the signing line "${signingLine}".`);
  return { version, signingLine };
}

/** Compares lines without links, case, extra spaces or the final full stop. */
function normalize(line: string): string {
  return line
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .trim()
    .replace(/\s+/g, " ")
    .replace(/\.$/, "")
    .toLowerCase();
}

/** The boxes (`- [ ] label`) with the signing line in a pull request description, ticked or not. */
function agreementBoxes(body: string, signingLine: string): { readonly line: string; readonly ticked: boolean }[] {
  const expected = normalize(signingLine);
  return body.split(/\r?\n/).flatMap((line) => {
    const [, mark, label] = /^\s*[-*] \[([ xX])\] (.+)$/.exec(line) ?? [];
    return mark !== undefined && label !== undefined && normalize(label) === expected
      ? [{ line: line.trim(), ticked: mark !== " " }]
      : [];
  });
}

/** The line of a comment that signs, if there is one. */
function signingLineIn(body: string, signingLine: string): string | undefined {
  const expected = normalize(signingLine);
  // A quoted line (`> ...`) does not sign: an author can quote the request to ask about it.
  return body
    .split(/\r?\n/)
    .map((line) => line.trim().replace(/^`+|`+$/g, ""))
    .find((line) => normalize(line) === expected);
}

interface Person {
  readonly login: string;
  readonly id: number;
}

interface Signature {
  readonly login: string;
  readonly userId: number;
  readonly version: string;
  readonly signedAt: string;
  readonly pullRequest: number;
  readonly method: "checkbox" | "comment";
  /** The pull request for a checkbox, or the comment. */
  readonly url: string;
  readonly text: string;
}

interface SignatureStore {
  readonly signatures: readonly Signature[];
  /** The blob SHA of SIGNATURES_FILE, or undefined when the file does not exist. */
  readonly sha: string | undefined;
  readonly branchExists: boolean;
}

function loadStore(): SignatureStore {
  try {
    const row = read(
      `repos/${repository}/contents/${SIGNATURES_FILE}?ref=${SIGNATURES_BRANCH}`,
      '[.sha, (.content | gsub("\\n"; ""))] | @tsv',
    ).split("\t");
    const parsed: { signatures: Signature[] } = JSON.parse(Buffer.from(column(row, 1), "base64").toString("utf8"));
    return { signatures: parsed.signatures, sha: column(row, 0), branchExists: true };
  } catch (error) {
    if (!failedWith(error, [404])) throw error;
  }
  try {
    read(`repos/${repository}/git/ref/heads/${SIGNATURES_BRANCH}`, ".object.sha");
    return { signatures: [], sha: undefined, branchExists: true };
  } catch (error) {
    if (!failedWith(error, [404])) throw error;
    return { signatures: [], sha: undefined, branchExists: false };
  }
}

function hasSigned(signatures: readonly Signature[], id: number, version: string): boolean {
  return signatures.some((signature) => signature.userId === id && signature.version === version);
}

/** Writes the file with the content API. A new branch gets one commit with no parent. */
function saveStore(store: SignatureStore, signatures: readonly Signature[], message: string): void {
  const content = `${JSON.stringify({ signatures }, null, 2)}\n`;
  if (!store.branchExists) {
    const tree = gh(
      ["api", "-X", "POST", `repos/${repository}/git/trees`, "--input", "-", "--jq", ".sha"],
      JSON.stringify({ tree: [{ path: SIGNATURES_FILE, mode: "100644", type: "blob", content }] }),
    );
    const commit = gh(
      ["api", "-X", "POST", `repos/${repository}/git/commits`, "--input", "-", "--jq", ".sha"],
      JSON.stringify({ message, tree, parents: [] }),
    );
    gh(
      ["api", "-X", "POST", `repos/${repository}/git/refs`, "--input", "-"],
      JSON.stringify({ ref: `refs/heads/${SIGNATURES_BRANCH}`, sha: commit }),
    );
    return;
  }
  gh(
    ["api", "-X", "PUT", `repos/${repository}/contents/${SIGNATURES_FILE}`, "--input", "-"],
    JSON.stringify({
      message,
      branch: SIGNATURES_BRANCH,
      content: Buffer.from(content).toString("base64"),
      ...(store.sha === undefined ? {} : { sha: store.sha }),
    }),
  );
}

/**
 * Adds the new signatures to the record. A different run that writes at the same time makes the
 * write fail with 409 or 422; then the record is read again and merged.
 */
function recordSignatures(added: readonly Signature[], version: string): void {
  const message = `Record CLA ${version} signatures: ${added.map((signature) => signature.login).join(", ")}`;
  writes.push(`${message}\n${added.map((signature) => signature.url).join("\n")}`);
  if (dryRun) return;
  for (let attempt = 1; ; attempt += 1) {
    const store = loadStore();
    const missing = added.filter((signature) => !hasSigned(store.signatures, signature.userId, version));
    if (missing.length === 0) return;
    try {
      saveStore(store, [...store.signatures, ...missing], message);
      return;
    } catch (error) {
      if (attempt === SAVE_ATTEMPTS || !failedWith(error, [409, 422])) throw error;
    }
  }
}

interface PullRequest {
  readonly number: number;
  readonly author: Person;
  readonly authorIsBot: boolean;
  readonly association: string;
  readonly sha: string;
  readonly state: string;
  readonly commits: number;
}

function loadPullRequest(number: number): PullRequest {
  const row = read(
    `repos/${repository}/pulls/${number}`,
    "[.user.login, .user.id, .user.type, .author_association, .head.sha, .state, .commits] | @tsv",
  ).split("\t");
  return {
    number,
    author: { login: column(row, 0), id: Number(column(row, 1)) },
    authorIsBot: column(row, 2) === "Bot",
    association: column(row, 3),
    sha: column(row, 4),
    state: column(row, 5),
    commits: Number(column(row, 6)),
  };
}

/** The people who must sign, and the commits whose author email has no GitHub account. */
function requiredSigners(pr: PullRequest): { readonly signers: Person[]; readonly unlinked: string[] } {
  const people = new Map<number, Person>();
  const unlinked: string[] = [];
  if (!pr.authorIsBot) people.set(pr.author.id, pr.author);
  const rows = readLines(
    `repos/${repository}/pulls/${pr.number}/commits?per_page=100`,
    '.[] | [(.author.login // ""), (.author.id // 0 | tostring), (.author.type // ""), .sha] | @tsv',
  ).map((line) => line.split("\t"));
  for (const row of rows) {
    const login = column(row, 0);
    if (login === "") unlinked.push(column(row, 3).slice(0, 12));
    else if (column(row, 2) !== "Bot") people.set(Number(column(row, 1)), { login, id: Number(column(row, 1)) });
  }
  const signers = [...people.values()].filter(
    (person) => !isTrusted(person.login, person.id === pr.author.id ? pr.association : ""),
  );
  return { signers, unlinked };
}

function signingComments(
  number: number,
  signers: readonly Person[],
  version: string,
  signingLine: string,
): Signature[] {
  const ids = new Set(signers.map((person) => person.id));
  const found = new Map<number, Signature>();
  const rows = readLines(
    `repos/${repository}/issues/${number}/comments?per_page=100`,
    '.[] | [.user.id, .user.login, .created_at, .html_url, (.body // "" | @base64), .node_id] | @tsv',
  ).map((line) => line.split("\t"));
  for (const row of rows) {
    const userId = Number(column(row, 0));
    const login = column(row, 1);
    if (!ids.has(userId) || found.has(userId)) continue;
    const text = signingLineIn(Buffer.from(column(row, 4), "base64").toString("utf8"), signingLine);
    if (text === undefined) continue;
    // A user with write access can edit the comment of a different person. As for the box, only an
    // edit by the comment author counts.
    const edit = gh([
      "api",
      "graphql",
      "-f",
      "query=query($id:ID!){node(id:$id){... on IssueComment{lastEditedAt editor{login}}}}",
      "-F",
      `id=${column(row, 5)}`,
      "--jq",
      '.data.node | [(.lastEditedAt // ""), (.editor.login // "")] | @tsv',
    ]).split("\t");
    const editor = column(edit, 1);
    if (editor !== "" && editor !== login) continue;
    found.set(userId, {
      login,
      userId,
      version,
      signedAt: column(edit, 0) || column(row, 2),
      pullRequest: number,
      method: "comment",
      url: column(row, 3),
      text,
    });
  }
  return [...found.values()];
}

interface Description {
  readonly body: string;
  readonly createdAt: string;
  /** Empty when nobody edited the description. */
  readonly lastEditedAt: string;
  readonly url: string;
  /** The login of the last editor, or empty when nobody edited the description. */
  readonly editor: string;
}

function loadDescription(number: number): Description {
  const [owner, name] = repository.split("/");
  const query =
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){body createdAt lastEditedAt url editor{login}}}}";
  const row = gh([
    "api",
    "graphql",
    "-f",
    `query=${query}`,
    "-F",
    `owner=${owner}`,
    "-F",
    `name=${name}`,
    "-F",
    `number=${number}`,
    "--jq",
    '.data.repository.pullRequest | [(.body // "" | @base64), .createdAt, (.lastEditedAt // ""), .url, (.editor.login // "")] | @tsv',
  ]).split("\t");
  return {
    body: Buffer.from(column(row, 0), "base64").toString("utf8"),
    createdAt: column(row, 1),
    lastEditedAt: column(row, 2),
    url: column(row, 3),
    editor: column(row, 4),
  };
}

/** The signature of the pull request author from the box in the description, if the author ticked it. */
function checkboxSignature(
  pr: PullRequest,
  description: Description,
  version: string,
  signingLine: string,
): Signature | undefined {
  if (description.editor !== "" && description.editor !== pr.author.login) return undefined;
  const text = agreementBoxes(description.body, signingLine).find((box) => box.ticked)?.line;
  if (text === undefined) return undefined;
  return {
    login: pr.author.login,
    userId: pr.author.id,
    version,
    signedAt: description.lastEditedAt || description.createdAt,
    pullRequest: pr.number,
    method: "checkbox",
    url: description.url,
    text,
  };
}

/**
 * Adds an agreement section with an unticked box to the end of the description, so that the author
 * signs with one click on the box. GitHub starts no workflow for this edit, because the job token
 * makes it. A failure only goes to the log: the status and the comment must still be written.
 */
function addBox(number: number, body: string, signingLine: string): void {
  // The edit replaces the whole description. When the author changed it after `body` was read, do
  // not overwrite the change: the run for that `edited` event adds the box.
  if (loadDescription(number).body !== body) return;
  const before = body.trimEnd() === "" ? "" : `${body.trimEnd()}\n\n`;
  const box = `- [ ] ${signingLine.replace(AGREEMENT_NAME, `[${AGREEMENT_NAME}](${agreementUrl})`)}`;
  try {
    write(
      `add the agreement box to the description of #${number}`,
      ["api", "-X", "PATCH", `repos/${repository}/pulls/${number}`, "-F", "body=@-"],
      `${before}## Contributor Assignment Agreement\n\n${box}\n`,
    );
  } catch (error) {
    process.stderr.write(`Could not add the agreement box to #${number}: ${String(error)}\n`);
  }
}

/** Keeps one CLA comment per pull request and edits it in place. */
function upsertStickyComment(number: number, body: string, createIfMissing: boolean): void {
  const text = `${COMMENT_MARKER}\n${body}`;
  const [existing] = readLines(
    `repos/${repository}/issues/${number}/comments?per_page=100`,
    `.[] | select(.user.login == "${COMMENT_AUTHOR}" and (.body | contains("${COMMENT_MARKER}"))) | .id`,
  );
  if (existing !== undefined) {
    write(
      `edit CLA comment on #${number}:\n${body}`,
      ["api", "-X", "PATCH", `repos/${repository}/issues/comments/${existing}`, "-F", "body=@-"],
      text,
    );
  } else if (createIfMissing) {
    write(
      `comment on #${number}:\n${body}`,
      ["api", "-X", "POST", `repos/${repository}/issues/${number}/comments`, "-F", "body=@-"],
      text,
    );
  }
}

function setStatus(sha: string, state: "success" | "failure", description: string): void {
  write(`status ${state} on ${sha.slice(0, 12)}: ${description}`, [
    "api",
    "-X",
    "POST",
    `repos/${repository}/statuses/${sha}`,
    "-f",
    `state=${state}`,
    "-f",
    `context=${STATUS_CONTEXT}`,
    "-f",
    `description=${description}`,
    "-f",
    `target_url=${agreementUrl}`,
  ]);
}

function requestComment(
  pr: PullRequest,
  unsigned: readonly Person[],
  unlinked: readonly string[],
  version: string,
  signingLine: string,
): string {
  const lines: string[] = [];
  if (unsigned.length > 0) {
    const authorUnsigned = unsigned.some((person) => person.id === pr.author.id);
    const others = unsigned.filter((person) => person.id !== pr.author.id);
    lines.push(
      `${unsigned.map((person) => `@${person.login}`).join(", ")}, thank you for the contribution. Before we can merge it, each author from outside the organization must sign the [OpenBot Contributor Assignment Agreement](${agreementUrl}) ${version}. The agreement assigns the copyright in your contributions to Synthetify Labs.`,
      "",
    );
    if (authorUnsigned) {
      lines.push(
        `@${pr.author.login}, to sign, read the agreement. Then tick the agreement box in the description of this pull request. If the description has no box, add this line to it:`,
        "",
        "```markdown",
        `- [x] ${signingLine}`,
        "```",
        "",
      );
    }
    if (others.length > 0) {
      lines.push(
        `${others.map((person) => `@${person.login}`).join(", ")}, to sign, read the agreement. Then add a comment to this pull request with this line:`,
        "",
        "```text",
        signingLine,
        "```",
        "",
      );
    }
    lines.push(
      "You sign one time for each version of the agreement. The signature applies to all your contributions to OpenBot.",
    );
  }
  if (unlinked.length > 0) {
    if (lines.length > 0) lines.push("");
    lines.push(
      `The author email of these commits is not linked to a GitHub account, so we cannot find who must sign: ${unlinked.map((sha) => `\`${sha}\``).join(", ")}. Add the email to your GitHub account, or change the commit author and push again.`,
    );
  }
  return lines.join("\n");
}

interface Decision {
  readonly number: number;
  readonly state: string;
  readonly signers: readonly string[];
  readonly recorded: readonly string[];
  readonly unsigned: readonly string[];
  readonly unlinked: readonly string[];
  readonly result: "skipped" | "pass" | "fail";
  readonly reason: string;
}

function checkPullRequest(number: number): Decision {
  const { version, signingLine } = loadAgreement();
  const pr = loadPullRequest(number);
  if (pr.state !== "open") {
    return {
      number,
      state: pr.state,
      signers: [],
      recorded: [],
      unsigned: [],
      unlinked: [],
      result: "skipped",
      reason: `pull request is ${pr.state}`,
    };
  }
  const { signers, unlinked } = requiredSigners(pr);
  const store = loadStore();
  const missing = signers.filter((person) => !hasSigned(store.signatures, person.id, version));
  const description = missing.some((person) => person.id === pr.author.id) ? loadDescription(number) : undefined;
  const box = description === undefined ? undefined : checkboxSignature(pr, description, version, signingLine);
  const rest = missing.filter((person) => person.id !== box?.userId);
  const added = [
    ...(box === undefined ? [] : [box]),
    ...(rest.length === 0 ? [] : signingComments(number, rest, version, signingLine)),
  ];
  if (added.length > 0) recordSignatures(added, version);
  const addedIds = new Set(added.map((signature) => signature.userId));
  const unsigned = missing.filter((person) => !addedIds.has(person.id));
  if (
    description !== undefined &&
    unsigned.some((person) => person.id === pr.author.id) &&
    agreementBoxes(description.body, signingLine).length === 0
  ) {
    addBox(number, description.body, signingLine);
  }

  const tooManyCommits = pr.commits > MAX_COMMITS;
  const passed = unsigned.length === 0 && unlinked.length === 0 && !tooManyCommits;
  const reason = tooManyCommits
    ? `More than ${MAX_COMMITS} commits. Squash the commits so that the authors can be checked.`
    : unsigned.length > 0
      ? `Not signed by ${unsigned.map((person) => person.login).join(", ")}.`
      : unlinked.length > 0
        ? "A commit author has no GitHub account."
        : signers.length === 0
          ? "No author must sign."
          : "All authors signed.";

  setStatus(pr.sha, passed ? "success" : "failure", reason.slice(0, 140));
  if (passed) {
    upsertStickyComment(number, "All authors signed the agreement. The CLA check passes.", false);
  } else if (tooManyCommits) {
    upsertStickyComment(number, reason, true);
  } else {
    upsertStickyComment(number, requestComment(pr, unsigned, unlinked, version, signingLine), true);
  }

  return {
    number,
    state: pr.state,
    signers: signers.map((person) => person.login),
    recorded: added.map((signature) => signature.login),
    unsigned: unsigned.map((person) => person.login),
    unlinked,
    result: passed ? "pass" : "fail",
    reason,
  };
}

if (command !== "pr") fail("Usage: bun scripts/cla.ts pr <number> [--dry-run]");
const decision = checkPullRequest(parseNumber(numberArgument));

const report = { repository, command, dryRun, decision, writes };
if (dryRun) {
  mkdirSync(dirname(REPORT_PATH), { recursive: true });
  writeFileSync(REPORT_PATH, `${JSON.stringify(report, null, 2)}\n`);
}
process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
