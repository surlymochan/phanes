import { isDynamicRecord, isOneOf, isString } from "./runtime-values";

/**
 * The first visit of this browser to openbot.run, as the website saves it. The web client and the
 * checkout return page send it to the account service, which keeps the first one it gets for an
 * account. ANALYTICS.md describes the attribution; every field is a closed set or a bounded label.
 */
export interface FirstTouch {
  acquisitionSource: FirstTouchAcquisitionSource;
  sourcePlatform: FirstTouchSourcePlatform;
  /** The website screen of the first visit: a fixed path or a published article. */
  landingPage: string;
  /** The UTC day of the first visit, `YYYY-MM-DD`. */
  firstVisitOn: string;
  utmSource?: string;
  utmMedium?: string;
  utmCampaign?: string;
  utmContent?: string;
  utmTerm?: string;
}

export const FIRST_TOUCH_ACQUISITION_SOURCES = ["direct", "search", "social", "github", "other"] as const;
export type FirstTouchAcquisitionSource = (typeof FIRST_TOUCH_ACQUISITION_SOURCES)[number];

export const FIRST_TOUCH_SOURCE_PLATFORMS = [
  "instagram",
  "twitter",
  "reddit",
  "facebook",
  "linkedin",
  "discord",
  "tiktok",
  "youtube",
  "github",
  "google",
  "bing",
  "duckduckgo",
  "brave",
  "yahoo",
  "unknown",
] as const;
export type FirstTouchSourcePlatform = (typeof FIRST_TOUCH_SOURCE_PLATFORMS)[number];

export const FIRST_TOUCH_CAMPAIGN_FIELDS = ["utmSource", "utmMedium", "utmCampaign", "utmContent", "utmTerm"] as const;

export const FIRST_TOUCH_STORAGE_KEY = "openbot.first-touch.v1";
/** A visit older than this is not a source of a later account: the next visit replaces it. */
export const FIRST_TOUCH_MAX_AGE_DAYS = 90;

/** The same shape as the website's campaign tags: a value outside it is dropped, not shortened. */
export const FIRST_TOUCH_CAMPAIGN_VALUE = /^[a-z0-9][a-z0-9._-]{0,63}$/u;
const LANDING_PAGE = /^\/[a-z0-9/_-]{0,127}$/u;
const DAY = /^\d{4}-\d{2}-\d{2}$/u;
const DAY_MS = 86_400_000;

/** The first touch in an untrusted value, or null. Unknown keys are dropped. */
export function parseFirstTouch(value: unknown): FirstTouch | null {
  if (!isDynamicRecord(value)) return null;
  const { acquisitionSource, sourcePlatform, landingPage, firstVisitOn } = value;
  if (
    !isOneOf(FIRST_TOUCH_ACQUISITION_SOURCES, acquisitionSource) ||
    !isOneOf(FIRST_TOUCH_SOURCE_PLATFORMS, sourcePlatform) ||
    !isString(landingPage) ||
    !LANDING_PAGE.test(landingPage) ||
    !isString(firstVisitOn) ||
    !DAY.test(firstVisitOn) ||
    Number.isNaN(Date.parse(`${firstVisitOn}T00:00:00Z`))
  ) {
    return null;
  }
  const touch: FirstTouch = { acquisitionSource, sourcePlatform, landingPage, firstVisitOn };
  for (const field of FIRST_TOUCH_CAMPAIGN_FIELDS) {
    const campaign = value[field];
    if (isString(campaign) && FIRST_TOUCH_CAMPAIGN_VALUE.test(campaign)) touch[field] = campaign;
  }
  return touch;
}

/** The UTC day of a time, as `firstVisitOn` stores it. */
export function firstTouchDay(now: number): string {
  return new Date(now).toISOString().slice(0, 10);
}

/**
 * The saved first touch of this browser, or null when there is none, it is damaged or too old.
 * `storage` is a getter because reading `window.localStorage` throws when the browser blocks storage.
 */
export function readFirstTouch(storage: () => Pick<Storage, "getItem">, now: number): FirstTouch | null {
  try {
    const stored = storage().getItem(FIRST_TOUCH_STORAGE_KEY);
    const touch = stored ? parseFirstTouch(JSON.parse(stored)) : null;
    if (!touch) return null;
    const age = now - Date.parse(`${touch.firstVisitOn}T00:00:00Z`);
    return age <= FIRST_TOUCH_MAX_AGE_DAYS * DAY_MS ? touch : null;
  } catch {
    return null;
  }
}

/** Saves the visit unless this browser already has a valid first touch. Storage failures are silent. */
export function rememberFirstTouch(
  storage: () => Pick<Storage, "getItem" | "setItem">,
  touch: FirstTouch,
  now: number,
): void {
  try {
    if (readFirstTouch(storage, now)) return;
    storage().setItem(FIRST_TOUCH_STORAGE_KEY, JSON.stringify(touch));
  } catch {
    // Attribution must never change what the website does.
  }
}
