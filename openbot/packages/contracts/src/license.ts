import {
  BILLING_CURRENCIES,
  BILLING_SUBSCRIPTION_STATUSES,
  type BillingCurrency,
  type BillingSubscriptionStatus,
  isStripeCheckoutUrl,
} from "./billing";
import { isBoolean, isDynamicRecord, isNumber, isOneOf, isString } from "./runtime-values";

/**
 * The commercial license: one monthly Stripe subscription per account. Its quantity is the number of
 * users. People count, not servers or computers: the owner and each account that is an active member
 * of a server that the owner owns. AI agents and chat-app participants do not count.
 */

/** The Stripe Price lookup key of the license. `stripe-bootstrap.ts` creates it. */
export const LICENSE_LOOKUP_KEY = "openbot_license_month";
/** The Stripe Product of the license. */
export const LICENSE_PRODUCT_ID = "openbot_license";
/**
 * The subscription metadata of a license. It names the account with `BILLING_METADATA.userId` and has
 * no server id, so a Worker that knows only server plans ignores it.
 */
export const LICENSE_METADATA = {
  kind: "openbot_kind",
  /**
   * The most users that Stripe invoiced in the period that ends at `paidPeriodEnd`. Fewer users apply
   * with no refund, so a later increase up to this count is already paid.
   */
  paidUsers: "openbot_paid_users",
  /** The end of the period that `paidUsers` is for, in milliseconds since the epoch. */
  paidPeriodEnd: "openbot_paid_period_end",
} as const;
export const LICENSE_METADATA_KIND = "license";

/** The base price includes this many users. */
export const LICENSE_INCLUDED_USERS = 5;
/** The largest license that the app sells. A larger team contacts sales. */
export const LICENSE_MAX_USERS = 25;

/** The monthly price in minor units: the base for the included users, then each extra user. */
export const LICENSE_PRICES: Readonly<Record<BillingCurrency, { base: number; extraUser: number }>> = {
  eur: { base: 10_000, extraUser: 1_500 },
  usd: { base: 12_000, extraUser: 1_800 },
  pln: { base: 44_000, extraUser: 6_600 },
};

/** The monthly amount of a license with this many users, in minor units. */
export function licenseAmount(users: number, currency: BillingCurrency): number {
  const price = LICENSE_PRICES[currency];
  return price.base + Math.max(0, users - LICENSE_INCLUDED_USERS) * price.extraUser;
}

/** True for a user count that the app can buy or set. */
export function isLicenseUserCount(value: unknown): value is number {
  return isNumber(value) && Number.isInteger(value) && value >= LICENSE_INCLUDED_USERS && value <= LICENSE_MAX_USERS;
}

export interface License {
  /** The Stripe subscription id. */
  subscriptionId: string;
  /** The number of users that the license pays for. */
  users: number;
  currency: BillingCurrency;
  /** The monthly list price in minor units, before discounts and tax. */
  amount: number;
  status: BillingSubscriptionStatus;
  /** Milliseconds since the epoch. */
  currentPeriodEnd: number | null;
  cancelAtPeriodEnd: boolean;
}

/** One person that the license of the account counts. */
export interface LicenseUser {
  name: string | null;
  email: string;
  /** The names of the owner's servers where this person is an active member. */
  servers: string[];
  /** True for the owner of the license. */
  owner: boolean;
}

/** The license of a server owner that covers this account. */
export interface LicenseCoverage {
  ownerName: string | null;
  ownerEmail: string;
}

export interface LicenseState {
  /** False when the account server has no billing. The app then shows no license. */
  available: boolean;
  /**
   * The currency of the account's open subscriptions. A Stripe customer pays in one currency, so a new
   * license must use it. Null when the account has no open subscription.
   */
  currency: BillingCurrency | null;
  /** The open license of the account, else its newest ended license, else null. */
  license: License | null;
  /** The number of people that the license counts now. */
  used: number;
  users: LicenseUser[];
  /** The open licenses of the owners of servers where this account is a member. */
  coveredBy: LicenseCoverage[];
}

export interface LicenseCheckoutRequest {
  users: number;
  currency: BillingCurrency;
}

export interface LicenseUsersRequest {
  users: number;
}

export interface LicenseRenewalRequest {
  /** True cancels the license at the end of its period. False renews it again. */
  cancel: boolean;
}

export const LICENSE_UNAVAILABLE_STATE: LicenseState = {
  available: false,
  currency: null,
  license: null,
  used: 0,
  users: [],
  coveredBy: [],
};

const SUBSCRIPTION_ID_PATTERN = /^sub_[A-Za-z0-9]{1,250}$/u;

function isCount(value: unknown): value is number {
  return isNumber(value) && Number.isSafeInteger(value) && value >= 0;
}

function parseLicense(value: unknown): License | null {
  if (
    !isDynamicRecord(value) ||
    !isString(value.subscriptionId) ||
    !SUBSCRIPTION_ID_PATTERN.test(value.subscriptionId) ||
    !isCount(value.users) ||
    !isOneOf(BILLING_CURRENCIES, value.currency) ||
    !isCount(value.amount) ||
    !isOneOf(BILLING_SUBSCRIPTION_STATUSES, value.status) ||
    !(value.currentPeriodEnd === null || isNumber(value.currentPeriodEnd)) ||
    !isBoolean(value.cancelAtPeriodEnd)
  ) {
    return null;
  }
  return {
    subscriptionId: value.subscriptionId,
    users: value.users,
    currency: value.currency,
    amount: value.amount,
    status: value.status,
    currentPeriodEnd: value.currentPeriodEnd,
    cancelAtPeriodEnd: value.cancelAtPeriodEnd,
  };
}

function parseLicenseUser(value: unknown): LicenseUser | null {
  if (
    !isDynamicRecord(value) ||
    !(value.name === null || isString(value.name)) ||
    !isString(value.email) ||
    !Array.isArray(value.servers) ||
    !value.servers.every(isString) ||
    !isBoolean(value.owner)
  ) {
    return null;
  }
  return { name: value.name, email: value.email, servers: [...value.servers], owner: value.owner };
}

function parseLicenseCoverage(value: unknown): LicenseCoverage | null {
  if (
    !isDynamicRecord(value) ||
    !(value.ownerName === null || isString(value.ownerName)) ||
    !isString(value.ownerEmail)
  )
    return null;
  return { ownerName: value.ownerName, ownerEmail: value.ownerEmail };
}

/** Returns null for a value that is not a license state in the account server shape. */
export function parseLicenseState(value: unknown): LicenseState | null {
  if (
    !isDynamicRecord(value) ||
    !isBoolean(value.available) ||
    !(value.currency === null || isOneOf(BILLING_CURRENCIES, value.currency)) ||
    !isCount(value.used) ||
    !Array.isArray(value.users) ||
    !Array.isArray(value.coveredBy)
  ) {
    return null;
  }
  const license = value.license === null ? null : parseLicense(value.license);
  if (value.license !== null && !license) return null;
  const users: LicenseUser[] = [];
  for (const item of value.users) {
    const user = parseLicenseUser(item);
    if (user) users.push(user);
  }
  const coveredBy: LicenseCoverage[] = [];
  for (const item of value.coveredBy) {
    const coverage = parseLicenseCoverage(item);
    if (coverage) coveredBy.push(coverage);
  }
  return { available: value.available, currency: value.currency, license, used: value.used, users, coveredBy };
}

export function parseLicenseCheckoutRequest(value: unknown): LicenseCheckoutRequest | null {
  if (!isDynamicRecord(value) || !isLicenseUserCount(value.users) || !isOneOf(BILLING_CURRENCIES, value.currency))
    return null;
  return { users: value.users, currency: value.currency };
}

export function parseLicenseUsersRequest(value: unknown): LicenseUsersRequest | null {
  if (!isDynamicRecord(value) || !isLicenseUserCount(value.users)) return null;
  return { users: value.users };
}

export function parseLicenseRenewalRequest(value: unknown): LicenseRenewalRequest | null {
  if (!isDynamicRecord(value) || !isBoolean(value.cancel)) return null;
  return { cancel: value.cancel };
}

/** Returns the Checkout page URL from a `{ url }` response, or null when it is not a Stripe Checkout page. */
export function parseLicenseCheckoutUrl(value: unknown): string | null {
  return isDynamicRecord(value) && isStripeCheckoutUrl(value.url) ? value.url : null;
}
