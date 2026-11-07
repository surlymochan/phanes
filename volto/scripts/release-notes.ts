// The release gate for CHANGELOG.md. The public /changelog page reads the file as it is, so a
// release that ships with an empty or malformed section ships with an empty or broken page entry.
// No package imports: `release.yml` runs this before `bun install`.

const RELEASE_GROUPS = ["Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"];
const PLACEHOLDER = /\b(TODO|TBD|FIXME|XXX)\b/;

export const UNRELEASED = "Unreleased";

/**
 * The date of a mobile release that no store has approved yet: `## [1.2.0] - In review`.
 * `/changelog` does not show it. `bun run mobile:release:published` writes the real date.
 */
export const IN_REVIEW = "In review";

/** The stores of the mobile app. Each one reviews a build on its own, so each has its own date. */
export const MOBILE_STORES = ["ios", "android"] as const;
export type MobileStore = (typeof MOBILE_STORES)[number];
export const MOBILE_STORE_NAMES: Record<MobileStore, string> = { ios: "iOS", android: "Android" };

/** The day each store made the version available. A store that still reviews it has no entry. */
export type StoreDates = Partial<Record<MobileStore, string>>;

const DAY = /^\d{4}-\d{2}-\d{2}$/;
const STORE_DATE = /^(\d{4}-\d{2}-\d{2}) \(([^)]+)\)$/;
const isDay = (text: string) => DAY.test(text) && !Number.isNaN(Date.parse(`${text}T00:00:00Z`));
const storeNamed = (name: string) => MOBILE_STORES.find((store) => MOBILE_STORE_NAMES[store] === name);

/**
 * Reads what follows `## [x.y.z] - ` in a mobile heading. Undefined when it is not one of:
 *
 *   In review                                no store has the version
 *   2026-10-14                               both stores since that day
 *   2026-10-14 (Android)                     only that store; the other one still reviews it
 *   2026-10-14 (Android), 2026-10-16 (iOS)   both stores, earlier day first
 */
export function parseStoreDates(text: string): StoreDates | undefined {
  if (text === IN_REVIEW) return {};
  if (isDay(text)) return { ios: text, android: text };
  const dates: StoreDates = {};
  const parts = text.split(", ");
  for (const part of parts) {
    const [, day = "", name = ""] = STORE_DATE.exec(part) ?? [];
    const store = storeNamed(name);
    if (!isDay(day) || store === undefined || dates[store] !== undefined) return undefined;
    dates[store] = day;
  }
  // One form for each state, so a heading that `formatStoreDates` would write differently is wrong.
  return parts.length <= 2 && formatStoreDates(dates) === text ? dates : undefined;
}

/** The heading text of `dates`. The inverse of `parseStoreDates`. */
export function formatStoreDates(dates: StoreDates): string {
  const entries = MOBILE_STORES.flatMap((store) => {
    const day = dates[store];
    return day === undefined ? [] : [{ store, day }];
  }).sort((a, b) => a.day.localeCompare(b.day));
  const [first, second] = entries;
  if (first === undefined) return IN_REVIEW;
  if (second !== undefined && second.day === first.day) return first.day;
  return entries.map(({ store, day }) => `${day} (${MOBILE_STORE_NAMES[store]})`).join(", ");
}

interface ReleaseNotesOptions {
  /** Accept the mobile heading forms of `parseStoreDates`. Only the mobile app waits for store review. */
  storeReview?: boolean;
}

/** What stops `version` from being released. Empty when its section is ready. */
export function releaseNotesProblems(
  changelog: string,
  version: string,
  { storeReview = false }: ReleaseNotesOptions = {},
): string[] {
  const lines = changelog.split(/\r?\n/);
  const start = lines.findIndex((line) => line.startsWith(`## [${version}]`));
  if (start === -1) return [`CHANGELOG.md has no "## [${version}]" section.`];

  const problems: string[] = [];
  if (lines.filter((line) => line.startsWith(`## [${version}]`)).length > 1) {
    problems.push(`CHANGELOG.md has more than one "## [${version}]" section.`);
  }
  const heading = lines[start] ?? "";
  const date = /^## \[[^\]]+\] - (\d{4}-\d{2}-\d{2})$/.exec(heading)?.[1];
  const prefix = `## [${version}] - `;
  // The page formats the date, and an impossible one such as 2026-13-45 stops it from rendering.
  if (storeReview) {
    if (
      version !== UNRELEASED &&
      (!heading.startsWith(prefix) || parseStoreDates(heading.slice(prefix.length)) === undefined)
    ) {
      problems.push(
        `"${heading}" must be "${prefix}" and then "${IN_REVIEW}", a real YYYY-MM-DD date for both stores, ` +
          `"YYYY-MM-DD (Android)" or "YYYY-MM-DD (iOS)" for one store, or "YYYY-MM-DD (Android), YYYY-MM-DD (iOS)" ` +
          "with the earlier date first.",
      );
    }
  } else if (version !== UNRELEASED && (date === undefined || !isDay(date))) {
    problems.push(`"${heading}" must be "## [${version}] - YYYY-MM-DD" with a real date.`);
  }

  const end = lines.findIndex((line, index) => index > start && line.startsWith("## "));
  const section = lines.slice(start + 1, end === -1 ? undefined : end);
  let group: string | undefined;
  let groupEntries = 0;
  let entries = 0;
  const closeGroup = () => {
    if (group !== undefined && groupEntries === 0) problems.push(`"### ${group}" in ${version} has no entries.`);
  };

  for (const line of section) {
    const groupHeading = /^### (.+)$/.exec(line);
    if (groupHeading) {
      closeGroup();
      group = (groupHeading[1] ?? "").trim();
      groupEntries = 0;
      if (!RELEASE_GROUPS.includes(group)) {
        problems.push(`"### ${group}" in ${version} is not one of ${RELEASE_GROUPS.join(", ")}.`);
      }
      continue;
    }
    if (line.startsWith("- ")) {
      if (group === undefined) problems.push(`"${line}" in ${version} is not under a "### " group.`);
      if (line.slice(2).trim() === "") problems.push(`An item in ${version} has no text.`);
      groupEntries += 1;
      entries += 1;
    }
    if (PLACEHOLDER.test(line)) problems.push(`"${line.trim()}" in ${version} is a placeholder.`);
  }
  closeGroup();

  if (entries === 0) {
    problems.push(`${version} has no entries. Add "- " items under "### Added", "### Changed" or "### Fixed".`);
  }
  return problems;
}

/** Where each pull request writes its own notes. The release moves them into CHANGELOG.md. */
export const FRAGMENT_DIR = "changelog.d";

/** The iPhone app has its own notes and versions. `/changelog` shows them in its Mobile tab. */
export const MOBILE_CHANGELOG = "apps/mobile/CHANGELOG.md";
export const MOBILE_FRAGMENT_DIR = "apps/mobile/changelog.d";
/** `app.json` is the version that the App Store shows. The first file is where it is read. */
export const MOBILE_VERSION_FILES = ["apps/mobile/app.json", "apps/mobile/package.json"] as const;

/** The items under `## [Unreleased]`. A release moves them into its section. */
export function unreleasedItems(changelog: string): string[] {
  const lines = changelog.split(/\r?\n/);
  const start = lines.findIndex((line) => line.startsWith(`## [${UNRELEASED}]`));
  if (start === -1) return [];
  const end = lines.findIndex((line, index) => index > start && line.startsWith("## "));
  return lines.slice(start + 1, end === -1 ? undefined : end).filter((line) => line.startsWith("- "));
}

/** What stops one fragment in `changelog.d` from being released. Empty when it is ready. */
export function fragmentProblems(fragment: string): string[] {
  const problems = releaseNotesProblems(`## [${UNRELEASED}]\n${fragment}`, UNRELEASED);
  if (/^##? /m.test(fragment)) problems.push(`Use only "### " group headings in a fragment.`);
  for (const line of collectGroups(fragment, new Map())) problems.push(`"${line.trim()}" is not in a "- " item.`);
  return problems;
}

/**
 * The section body that holds every item of `bodies`. The groups follow the order of
 * `RELEASE_GROUPS`, and the items in a group keep the order of `bodies`.
 */
export function assembleSection(bodies: readonly string[]): string {
  const groups = new Map<string, string[]>();
  // The release deletes the fragments after it writes this section, so a line that would be left
  // out stops the release instead.
  const stray = bodies.flatMap((body) => collectGroups(body, groups));
  if (stray.length > 0) {
    throw new Error(`These release notes lines are not in a "- " item:\n${stray.join("\n")}`);
  }
  const rank = (group: string) => {
    const index = RELEASE_GROUPS.indexOf(group);
    return index === -1 ? RELEASE_GROUPS.length : index;
  };
  return [...groups]
    .filter(([, items]) => items.length > 0)
    .sort(([a], [b]) => rank(a) - rank(b))
    .map(([group, items]) => `### ${group}\n\n${items.join("\n")}`)
    .join("\n\n");
}

/** Adds the items of `body` to `groups`, with their continuation lines. Returns the other lines. */
function collectGroups(body: string, groups: Map<string, string[]>): string[] {
  const stray: string[] = [];
  let items: string[] | undefined;
  let open = false;
  for (const line of body.split(/\r?\n/)) {
    const groupHeading = /^### (.+)$/.exec(line);
    if (groupHeading) {
      const group = (groupHeading[1] ?? "").trim();
      items = groups.get(group) ?? [];
      groups.set(group, items);
      open = false;
    } else if (line.trim() === "") {
      open = false;
    } else if (items && line.startsWith("- ")) {
      items.push(line);
      open = true;
    } else if (items && open) {
      items[items.length - 1] += `\n${line}`;
    } else {
      stray.push(line);
    }
  }
  return stray;
}
