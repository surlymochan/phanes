import {
  BILLING_SUBSCRIPTION_STATUSES,
  type BillingCurrency,
  type BillingSubscriptionStatus,
  isOpenBillingStatus,
} from "@openbot/contracts/billing";
import {
  LICENSE_INCLUDED_USERS,
  LICENSE_LOOKUP_KEY,
  LICENSE_MAX_USERS,
  LICENSE_METADATA,
  LICENSE_METADATA_KIND,
  type License,
  type LicenseCheckoutRequest,
  type LicenseCoverage,
  type LicenseState,
  type LicenseUser,
  licenseAmount,
} from "@openbot/contracts/license";
import { isOneOf } from "@openbot/contracts/runtime-values";
import { sourceText } from "@openbot/i18n/source";
import { Context, Effect, Layer } from "effect";
import {
  type AccountAnalytics,
  type BillingAction,
  NO_ACCOUNT_ANALYTICS,
  type PlanChangeDirection,
  type PlanEndReason,
} from "./account-analytics";
import {
  BillingError,
  type BillingOperationError,
  billingCall,
  OPEN_STATUSES_SQL,
  stripeFailure,
  stripeRequest,
} from "./billing-errors";
import type { BillingSales } from "./billing-sales";
import {
  isBillingCurrency,
  type StripeClient,
  type StripePrice,
  StripeRequestError,
  type StripeSubscription,
  type StripeTransportError,
  subscriptionCustomerId,
  subscriptionLatestInvoiceId,
  subscriptionPeriodEnd,
} from "./stripe-client";

/** Prices change only when `stripe-bootstrap.ts` runs, so each isolate reads the price once in this time. */
const PRICE_TTL_MS = 5 * 60_000;
const priceCache = new Map<string, { expiresAt: number; price: StripePrice | null }>();

/** Where the Stripe page sends the user back: the desktop return page or the web client. */
export type LicenseReturnTarget = "desktop" | "web";

/** A license subscription that a sync stored. */
export interface LicenseSync {
  kind: "license";
  userId: string;
  latestInvoiceId: string | null;
  users: number;
  currency: BillingCurrency;
}

/** The stored state of a license before a sync writes it. */
interface StoredLicense {
  users: number;
  status: string;
  cancel_at_period_end: number;
}

interface LicenseRow {
  stripe_subscription_id: string;
  users: number;
  currency: string;
  status: string;
  current_period_end: number | null;
  cancel_at_period_end: number;
}

interface LicenseUserRow {
  id: string;
  name: string | null;
  email: string;
  server_name: string | null;
}

export interface LicenseServiceOptions {
  database: D1Database;
  stripe: StripeClient;
  secretKey: string;
  now: () => number;
  /** The Stripe customer of the account. Server plans and the license use the same customer. */
  ensureCustomer: (user: { id: string; email: string }) => Effect.Effect<string, BillingError | BillingOperationError>;
  /** Reports the lifecycle of a license. The sync reports each stored change once. */
  analytics?: AccountAnalytics;
  /** The sales ledger. Without it, a started Checkout goes to `analytics`. */
  sales?: BillingSales | null;
}

class LicenseDependencies extends Context.Service<
  LicenseDependencies,
  { database: D1Database; stripe: StripeClient; now: () => number }
>()("auth-api/LicenseService/Dependencies") {}

/** True for a Stripe Price of the license. A changed amount moves the lookup key, so the metadata also counts. */
export function isLicensePrice(price: { lookup_key: string | null; metadata: Readonly<Record<string, string>> }) {
  return price.lookup_key === LICENSE_LOOKUP_KEY || price.metadata[LICENSE_METADATA.kind] === LICENSE_METADATA_KIND;
}

/**
 * The commercial license of an account: one monthly subscription whose quantity is the number of users.
 * It uses its own table, so the server plans, their member limits and the released routes do not change.
 */
export class LicenseService {
  readonly #layer: Layer.Layer<LicenseDependencies>;
  readonly #secretKey: string;
  readonly #ensureCustomer: LicenseServiceOptions["ensureCustomer"];
  readonly #analytics: AccountAnalytics;
  readonly #sales: BillingSales | null;

  constructor(options: LicenseServiceOptions) {
    this.#secretKey = options.secretKey;
    this.#ensureCustomer = options.ensureCustomer;
    this.#analytics = options.analytics ?? NO_ACCOUNT_ANALYTICS;
    this.#sales = options.sales ?? null;
    this.#layer = Layer.succeed(LicenseDependencies, {
      database: options.database,
      stripe: options.stripe,
      now: options.now,
    });
  }

  /**
   * The license of the account, the people that it counts, and the licenses of other owners that cover
   * this account. People count: the owner and each account that is an active member of a server that the
   * owner owns. AI agents and chat-app participants have no membership, so they do not count.
   */
  readonly getState = Effect.fn("LicenseService.getState")(
    function* (
      this: LicenseService,
      userId: string,
    ): Effect.fn.Return<LicenseState, BillingError | BillingOperationError, LicenseDependencies> {
      const dependencies = yield* LicenseDependencies;
      const [license, currency, userRows, coveredBy] = yield* Effect.all(
        [
          this.#latestLicense(userId),
          this.#lockedCurrency(userId),
          billingCall(() =>
            dependencies.database
              .prepare(
                `SELECT u.id, u.name, u.email, NULL AS server_name FROM users u WHERE u.id = ?1
                 UNION ALL
                 SELECT u.id, u.name, u.email, h.name AS server_name
                   FROM remote_hosts h
                   JOIN remote_memberships m ON m.host_id = h.host_id AND m.status = 'active'
                   JOIN users u ON u.id = m.user_id
                  WHERE h.owner_user_id = ?1 AND m.user_id <> ?1
                  ORDER BY server_name COLLATE NOCASE`,
              )
              .bind(userId)
              .all<LicenseUserRow>(),
          ),
          billingCall(() =>
            dependencies.database
              .prepare(
                `SELECT DISTINCT o.name AS owner_name, o.email AS owner_email
                   FROM remote_memberships m
                   JOIN remote_hosts h ON h.host_id = m.host_id AND h.owner_user_id <> ?1
                   JOIN licenses l ON l.user_id = h.owner_user_id AND l.status IN ${OPEN_STATUSES_SQL}
                   JOIN users o ON o.id = h.owner_user_id
                  WHERE m.user_id = ?1 AND m.status = 'active'
                  ORDER BY o.name COLLATE NOCASE, o.email`,
              )
              .bind(userId)
              .all<{ owner_name: string | null; owner_email: string }>(),
          ),
        ],
        { concurrency: "unbounded" },
      );
      const users = licenseUsers(userId, userRows.results);
      return {
        // Sales pause when the Stripe price is archived. The section still shows, and checkout then
        // tells the user whom to contact.
        available: true,
        currency,
        license: license ? licenseFromRow(license) : null,
        used: users.length,
        users,
        coveredBy: coveredBy.results.map(
          (row): LicenseCoverage => ({ ownerName: row.owner_name, ownerEmail: row.owner_email }),
        ),
      };
    },
    (operation) => operation.pipe(Effect.provide(this.#layer)),
  ).bind(this);

  /**
   * A Stripe Checkout page for a new license. An account has one open license and one open license page.
   * Two requests at the same time keep the newer page and both return it. Stripe is the source of truth:
   * a page paid before its webhook, or a page paid while this request runs, stops the new page.
   */
  readonly createCheckout = Effect.fn("LicenseService.createCheckout")(
    function* (
      this: LicenseService,
      user: { id: string; email: string },
      request: LicenseCheckoutRequest,
      target: LicenseReturnTarget,
      origin: string,
    ): Effect.fn.Return<string, BillingError | BillingOperationError, LicenseDependencies> {
      const dependencies = yield* LicenseDependencies;
      const price = yield* this.#price();
      if (!price) return yield* licenseUnavailable();
      if (yield* this.#openLicense(user.id)) return yield* licenseExists();
      // The account service refuses a join above the license, so a license cannot start below the
      // people that it counts now.
      const { used } = yield* this.getState(user.id);
      if (request.users < used)
        return yield* new BillingError(409, "license_users_in_use", sourceText("error.billing.licenseUsersInUse"));
      // A Stripe customer pays all its subscriptions in one currency.
      const locked = yield* this.#lockedCurrency(user.id);
      if (locked && locked !== request.currency)
        return yield* new BillingError(409, "license_currency", sourceText("error.billing.licenseCurrency"));
      const customerId = yield* this.#ensureCustomer(user);
      const returnUrl = licenseReturnUrl(origin, target);
      const session = yield* this.#stripeCall(() =>
        dependencies.stripe.createCheckoutSession({
          customerId,
          priceId: price.id,
          currency: request.currency,
          userId: user.id,
          quantity: request.users,
          metadata: { [LICENSE_METADATA.kind]: LICENSE_METADATA_KIND },
          successUrl: returnUrl,
          cancelUrl: `${returnUrl}${returnUrl.includes("?") ? "&" : "?"}cancelled=1`,
          nowSeconds: Math.floor(dependencies.now() / 1_000),
        }),
      );
      const listed = yield* this.#stripeCall(() => dependencies.stripe.listOpenCheckoutSessions(customerId));
      const pages = listed.filter(
        (page) => page.metadata[LICENSE_METADATA.kind] === LICENSE_METADATA_KIND && page.url !== null,
      );
      // The list can be read before Stripe shows the new page in it.
      if (!pages.some((page) => page.id === session.id))
        pages.push({ ...session, status: "open", created: Math.floor(dependencies.now() / 1_000), metadata: {} });
      pages.sort((left, right) => right.created - left.created || right.id.localeCompare(left.id));
      const [kept = session, ...older] = pages;
      let paid = false;
      for (const page of older) {
        const closed = yield* this.#stripeCall(() => dependencies.stripe.expireCheckoutSession(page.id));
        if (closed.status === "complete") paid = true;
      }
      // Read after the older pages closed, so that a page paid before they closed shows here.
      const readAt = dependencies.now();
      const subscriptions = yield* this.#stripeCall(() => dependencies.stripe.listCustomerSubscriptions(customerId));
      const open = subscriptions.find(
        (subscription) =>
          isOneOf(BILLING_SUBSCRIPTION_STATUSES, subscription.status) &&
          isOpenBillingStatus(subscription.status) &&
          subscription.items.data.some((item) => isLicensePrice(item.price)),
      );
      if (open) yield* this.sync(open, user.id, readAt);
      if (paid || open) {
        yield* this.#stripeCall(() => dependencies.stripe.expireCheckoutSession(kept.id));
        return yield* licenseExists();
      }
      // Each page starts once: a request that returns a newer page of another request reports nothing.
      if (kept.id === session.id) yield* this.#checkoutStarted(user.id, session.id, request);
      return kept.url ?? session.url;
    },
    (operation) => operation.pipe(Effect.provide(this.#layer)),
  ).bind(this);

  /**
   * Changes the number of users. More users are invoiced now for the rest of the period, and the change
   * fails when that payment fails. Fewer users apply from the next invoice, with no refund. The license
   * cannot go below the people that it counts now.
   */
  readonly setUsers = Effect.fn("LicenseService.setUsers")(
    function* (
      this: LicenseService,
      userId: string,
      users: number,
    ): Effect.fn.Return<LicenseState, BillingError | BillingOperationError, LicenseDependencies> {
      const dependencies = yield* LicenseDependencies;
      const license = yield* this.#openLicense(userId);
      if (!license) return yield* noLicense();
      const state = yield* this.getState(userId);
      if (users < Math.max(LICENSE_INCLUDED_USERS, state.used) || users > LICENSE_MAX_USERS)
        return yield* new BillingError(409, "license_users_in_use", sourceText("error.billing.licenseUsersInUse"));
      if (users === license.users) return state;
      const current = yield* this.#stripeCall(() =>
        dependencies.stripe.getSubscription(license.stripe_subscription_id),
      );
      const item = current.items.data[0];
      if (!item || !isLicensePrice(item.price)) return yield* noLicense();
      const quantity = item.quantity ?? 1;
      // Fewer users apply with no refund, so the users that this period already paid for stay paid. Only
      // users above that count are invoiced now.
      const periodEnd = String(subscriptionPeriodEnd(current) ?? "");
      const paidUsers = Number(current.metadata[LICENSE_METADATA.paidUsers]);
      const paid =
        current.metadata[LICENSE_METADATA.paidPeriodEnd] === periodEnd && Number.isSafeInteger(paidUsers)
          ? Math.max(quantity, paidUsers)
          : quantity;
      // Each write gets the time after Stripe answers: the webhook of an earlier write in this change can
      // read Stripe after this change started, and its older state must not replace this one.
      const setQuantity = (next: number, proration: "always_invoice" | "none") =>
        stripeRequest(() =>
          dependencies.stripe.setSubscriptionQuantity({
            subscriptionId: current.id,
            itemId: item.id,
            quantity: next,
            proration,
            metadata: {
              [LICENSE_METADATA.paidUsers]: String(Math.max(paid, proration === "always_invoice" ? next : 0)),
              [LICENSE_METADATA.paidPeriodEnd]: periodEnd,
            },
          }),
        ).pipe(Effect.map((updated) => ({ updated, readAt: dependencies.now() })));
      if (users <= paid) {
        const { updated, readAt } = yield* setQuantity(users, "none").pipe(Effect.mapError(stripeFailure));
        yield* this.sync(updated, userId, readAt);
        return yield* this.getState(userId);
      }
      // Stripe prorates from the current quantity, so the license first goes back to the paid users at no
      // charge. When the payment fails, Stripe keeps the paid users and the license goes back to as it was.
      if (quantity !== paid) yield* setQuantity(paid, "none").pipe(Effect.mapError(stripeFailure));
      const sync = (written: { updated: StripeSubscription; readAt: number }) =>
        this.sync(written.updated, userId, written.readAt);
      const { updated, readAt } = yield* setQuantity(users, "always_invoice").pipe(
        Effect.catch((error) =>
          Effect.gen(function* () {
            // With no answer from Stripe, the increase can be applied and paid. Read it again, so that a paid
            // increase is kept and not charged a second time.
            if (!(error instanceof StripeRequestError)) {
              const readAt = dependencies.now();
              const after = yield* stripeRequest(() => dependencies.stripe.getSubscription(current.id)).pipe(
                Effect.mapError(stripeFailure),
              );
              if (after.items.data[0]?.quantity === users) return { updated: after, readAt };
            }
            if (quantity !== paid)
              yield* setQuantity(quantity, "none").pipe(
                Effect.flatMap(sync),
                Effect.catch(() =>
                  Effect.sync(() =>
                    console.warn("billing: license users were not restored", { subscriptionId: current.id }),
                  ),
                ),
              );
            return yield* Effect.fail(
              error instanceof StripeRequestError && error.status === 402
                ? new BillingError(402, "license_payment_failed", sourceText("error.billing.licensePaymentFailed"))
                : stripeFailure(error),
            );
          }),
        ),
      );
      yield* this.sync(updated, userId, readAt);
      return yield* this.getState(userId);
    },
    (operation) => operation.pipe(Effect.provide(this.#layer)),
  ).bind(this);

  /** Cancels the license at the end of its period, or renews it again. */
  readonly setRenewal = Effect.fn("LicenseService.setRenewal")(
    function* (
      this: LicenseService,
      userId: string,
      cancel: boolean,
    ): Effect.fn.Return<LicenseState, BillingError | BillingOperationError, LicenseDependencies> {
      const dependencies = yield* LicenseDependencies;
      const license = yield* this.#openLicense(userId);
      if (!license) return yield* noLicense();
      const readAt = dependencies.now();
      const updated = yield* this.#stripeCall(() =>
        dependencies.stripe.setRenewal(license.stripe_subscription_id, cancel),
      );
      yield* this.sync(updated, userId, readAt);
      return yield* this.getState(userId);
    },
    (operation) => operation.pipe(Effect.provide(this.#layer)),
  ).bind(this);

  /**
   * Stores a license subscription that the webhook or a change read from Stripe. Two reads of one
   * subscription can run at the same time, so the write of the older read is skipped.
   */
  readonly sync = Effect.fn("LicenseService.sync")(
    function* (
      this: LicenseService,
      subscription: StripeSubscription,
      ownerId: string,
      readAt: number,
    ): Effect.fn.Return<LicenseSync | null, BillingError | BillingOperationError, LicenseDependencies> {
      const dependencies = yield* LicenseDependencies;
      const currency = subscription.currency.toLowerCase();
      const users = subscription.items.data[0]?.quantity ?? 1;
      if (
        !isBillingCurrency(currency) ||
        !isOneOf(BILLING_SUBSCRIPTION_STATUSES, subscription.status) ||
        !Number.isSafeInteger(users) ||
        users < 1
      ) {
        console.warn("billing: subscription is not an OpenBot license", { subscriptionId: subscription.id });
        return null;
      }
      const cancelAtPeriodEnd = subscription.cancel_at_period_end || typeof subscription.cancel_at === "number";
      // One transaction reads the state before the write, so each stored change is reported once.
      const [before, written] = yield* billingCall(() =>
        dependencies.database.batch<StoredLicense>([
          dependencies.database
            .prepare("SELECT users, status, cancel_at_period_end FROM licenses WHERE stripe_subscription_id = ?")
            .bind(subscription.id),
          dependencies.database
            .prepare(
              `INSERT INTO licenses(
               stripe_subscription_id, user_id, stripe_customer_id, users, currency, status, current_period_end,
               cancel_at_period_end, updated_at
             ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
             ON CONFLICT(stripe_subscription_id) DO UPDATE SET
               users = excluded.users,
               currency = excluded.currency,
               status = excluded.status,
               current_period_end = excluded.current_period_end,
               cancel_at_period_end = excluded.cancel_at_period_end,
               updated_at = excluded.updated_at
             WHERE excluded.updated_at >= licenses.updated_at`,
            )
            .bind(
              subscription.id,
              ownerId,
              subscriptionCustomerId(subscription),
              users,
              currency,
              subscription.status,
              subscriptionPeriodEnd(subscription),
              cancelAtPeriodEnd ? 1 : 0,
              readAt,
            ),
        ]),
      );
      const previous = before?.results[0] ?? null;
      const change =
        written?.meta.changes === 1
          ? licenseChange(previous, { users, status: subscription.status, cancelAtPeriodEnd })
          : null;
      // Checkout lets one account open one license. A second one comes only from the Dashboard or a payment
      // that no check could stop, and a person must refund it.
      const others = yield* billingCall(() =>
        dependencies.database
          .prepare(
            `SELECT stripe_subscription_id FROM licenses
             WHERE user_id = ? AND stripe_subscription_id <> ? AND status IN ${OPEN_STATUSES_SQL}`,
          )
          .bind(ownerId, subscription.id)
          .all<{ stripe_subscription_id: string }>(),
      );
      if (isOpenBillingStatus(subscription.status) && others.results.length > 0)
        console.warn("billing: an account has more than one open license", {
          subscriptionIds: [subscription.id, ...others.results.map((row) => row.stripe_subscription_id)],
        });
      if (change) {
        // A license that starts after every earlier license of the account ended. A failed read leaves
        // the property out: the license is already saved, so the event must not fail the sync.
        const reactivated =
          change.action !== "plan_started"
            ? undefined
            : others.results.length > 0
              ? false
              : yield* billingCall(() =>
                  dependencies.database
                    .prepare(
                      "SELECT 1 AS ended FROM licenses WHERE user_id = ? AND stripe_subscription_id <> ? AND status = 'canceled' LIMIT 1",
                    )
                    .bind(ownerId, subscription.id)
                    .first<{ ended: number }>(),
                ).pipe(
                  Effect.map(Boolean),
                  Effect.catch(() => Effect.succeed(undefined)),
                );
        this.#analytics.track(ownerId, {
          name: "billing_action",
          action: change.action,
          product: "license",
          users,
          interval: "month",
          currency,
          amount: licenseAmount(users, currency),
          ...(change.direction ? { direction: change.direction } : {}),
          ...(reactivated === undefined ? {} : { reactivated }),
          ...(change.endReason ? { endReason: change.endReason } : {}),
        });
      }
      return {
        kind: "license",
        userId: ownerId,
        latestInvoiceId: subscriptionLatestInvoiceId(subscription),
        users,
        currency,
      };
    },
    (operation) => operation.pipe(Effect.provide(this.#layer)),
  ).bind(this);

  /** The account of a stored license, or null for a subscription that is not a stored license. */
  readonly storedOwner = Effect.fn("LicenseService.storedOwner")(
    function* (
      this: LicenseService,
      subscriptionId: string,
    ): Effect.fn.Return<string | null, BillingError | BillingOperationError, LicenseDependencies> {
      const dependencies = yield* LicenseDependencies;
      const row = yield* billingCall(() =>
        dependencies.database
          .prepare("SELECT user_id FROM licenses WHERE stripe_subscription_id = ?")
          .bind(subscriptionId)
          .first<{ user_id: string }>(),
      );
      return row?.user_id ?? null;
    },
    (operation) => operation.pipe(Effect.provide(this.#layer)),
  ).bind(this);

  /** The open licenses whose period ended before `before` and that no sync read since `readBefore`. */
  readonly lapsed = Effect.fn("LicenseService.lapsed")(
    function* (
      this: LicenseService,
      before: number,
      readBefore: number,
      limit: number,
    ): Effect.fn.Return<string[], BillingError | BillingOperationError, LicenseDependencies> {
      const dependencies = yield* LicenseDependencies;
      const rows = yield* billingCall(() =>
        dependencies.database
          .prepare(
            `SELECT stripe_subscription_id FROM licenses
             WHERE status IN ${OPEN_STATUSES_SQL} AND current_period_end < ? AND updated_at < ?
             ORDER BY updated_at LIMIT ?`,
          )
          .bind(before, readBefore, limit)
          .all<{ stripe_subscription_id: string }>(),
      );
      return rows.results.map((row) => row.stripe_subscription_id);
    },
    (operation) => operation.pipe(Effect.provide(this.#layer)),
  ).bind(this);

  /**
   * Records a started license Checkout. The page is already open, so a failed write is logged and does
   * not fail the request.
   */
  readonly #checkoutStarted = Effect.fn("LicenseService.checkoutStarted")(function* (
    this: LicenseService,
    userId: string,
    sessionId: string,
    request: LicenseCheckoutRequest,
  ): Effect.fn.Return<void, never, LicenseDependencies> {
    const dependencies = yield* LicenseDependencies;
    const amount = licenseAmount(request.users, request.currency);
    if (!this.#sales) {
      this.#analytics.track(userId, {
        name: "billing_action",
        action: "checkout_started",
        product: "license",
        users: request.users,
        interval: "month",
        currency: request.currency,
        amount,
      });
      return;
    }
    yield* this.#sales
      .licenseCheckout({
        sessionId,
        userId,
        users: request.users,
        currency: request.currency,
        amount,
        createdAt: dependencies.now(),
      })
      .pipe(Effect.catch(() => Effect.sync(() => console.warn("billing: license checkout fact was not saved"))));
  });

  readonly #openLicense = Effect.fn("LicenseService.openLicense")(function* (
    this: LicenseService,
    userId: string,
  ): Effect.fn.Return<LicenseRow | null, BillingError | BillingOperationError, LicenseDependencies> {
    const dependencies = yield* LicenseDependencies;
    return yield* billingCall(() =>
      dependencies.database
        .prepare(
          `SELECT stripe_subscription_id, users, currency, status, current_period_end, cancel_at_period_end
           FROM licenses WHERE user_id = ? AND status IN ${OPEN_STATUSES_SQL} ORDER BY updated_at DESC LIMIT 1`,
        )
        .bind(userId)
        .first<LicenseRow>(),
    );
  });

  /** The open license, else the newest ended license. */
  readonly #latestLicense = Effect.fn("LicenseService.latestLicense")(function* (
    this: LicenseService,
    userId: string,
  ): Effect.fn.Return<LicenseRow | null, BillingError | BillingOperationError, LicenseDependencies> {
    const dependencies = yield* LicenseDependencies;
    return yield* billingCall(() =>
      dependencies.database
        .prepare(
          `SELECT stripe_subscription_id, users, currency, status, current_period_end, cancel_at_period_end
           FROM licenses WHERE user_id = ?
           ORDER BY status IN ${OPEN_STATUSES_SQL} DESC, updated_at DESC LIMIT 1`,
        )
        .bind(userId)
        .first<LicenseRow>(),
    );
  });

  /** The currency of the account's open server plans and license, or null when it has none. */
  readonly #lockedCurrency = Effect.fn("LicenseService.lockedCurrency")(function* (
    this: LicenseService,
    userId: string,
  ): Effect.fn.Return<BillingCurrency | null, BillingError | BillingOperationError, LicenseDependencies> {
    const dependencies = yield* LicenseDependencies;
    const row = yield* billingCall(() =>
      dependencies.database
        .prepare(
          `SELECT currency FROM billing_subscriptions WHERE user_id = ?1 AND status IN ${OPEN_STATUSES_SQL}
           UNION ALL
           SELECT currency FROM licenses WHERE user_id = ?1 AND status IN ${OPEN_STATUSES_SQL}
           LIMIT 1`,
        )
        .bind(userId)
        .first<{ currency: string }>(),
    );
    return row && isBillingCurrency(row.currency) ? row.currency : null;
  });

  /** The license Price, or null when the Stripe account has none. */
  readonly #price = Effect.fn("LicenseService.price")(function* (
    this: LicenseService,
  ): Effect.fn.Return<StripePrice | null, BillingError | BillingOperationError, LicenseDependencies> {
    const dependencies = yield* LicenseDependencies;
    const now = dependencies.now();
    const cached = priceCache.get(this.#secretKey);
    if (cached && cached.expiresAt > now) return cached.price;
    const list = yield* this.#stripeCall(() => dependencies.stripe.listPricesByLookupKeys([LICENSE_LOOKUP_KEY]));
    const price = list.find((item) => item.lookup_key === LICENSE_LOOKUP_KEY) ?? null;
    priceCache.set(this.#secretKey, { expiresAt: now + PRICE_TTL_MS, price });
    return price;
  });

  readonly #stripeCall = Effect.fn("LicenseService.stripeCall")(
    <T>(call: () => Effect.Effect<T, StripeRequestError | StripeTransportError>) =>
      stripeRequest(call).pipe(Effect.mapError(stripeFailure)),
  );
}

/** The owner first, then each other person once, with the names of the owner's servers where they are a member. */
function licenseUsers(ownerId: string, rows: readonly LicenseUserRow[]): LicenseUser[] {
  const users = new Map<string, LicenseUser>();
  for (const row of rows) {
    const user = users.get(row.id) ?? { name: row.name, email: row.email, servers: [], owner: row.id === ownerId };
    if (row.server_name !== null && !user.servers.includes(row.server_name)) user.servers.push(row.server_name);
    users.set(row.id, user);
  }
  return [...users.values()].sort((left, right) => Number(right.owner) - Number(left.owner));
}

function licenseFromRow(row: LicenseRow): License | null {
  if (!isBillingCurrency(row.currency) || !isOneOf(BILLING_SUBSCRIPTION_STATUSES, row.status)) return null;
  const status: BillingSubscriptionStatus = row.status;
  return {
    subscriptionId: row.stripe_subscription_id,
    users: row.users,
    currency: row.currency,
    amount: licenseAmount(row.users, row.currency),
    status,
    currentPeriodEnd: row.current_period_end,
    cancelAtPeriodEnd: row.cancel_at_period_end === 1 && isOpenBillingStatus(status),
  };
}

/**
 * The change of a license that one sync stored, with the same actions as a server plan. One sync reports
 * one action: a start or an end first, then a change of users, then a change of renewal.
 */
function licenseChange(
  previous: StoredLicense | null,
  current: { users: number; status: BillingSubscriptionStatus; cancelAtPeriodEnd: boolean },
): { action: BillingAction; direction?: PlanChangeDirection; endReason?: PlanEndReason } | null {
  const wasOpen =
    previous !== null &&
    isOneOf(BILLING_SUBSCRIPTION_STATUSES, previous.status) &&
    isOpenBillingStatus(previous.status);
  const isOpen = isOpenBillingStatus(current.status);
  if (!previous || !wasOpen) return isOpen ? { action: "plan_started" } : null;
  if (!isOpen)
    return {
      action: "plan_ended",
      endReason: previous.status === "past_due" || previous.status === "unpaid" ? "unpaid" : "canceled",
    };
  if (previous.users !== current.users)
    return { action: "plan_changed", direction: current.users > previous.users ? "up" : "down" };
  const wasCancelling = previous.cancel_at_period_end === 1;
  if (wasCancelling === current.cancelAtPeriodEnd) return null;
  return { action: current.cancelAtPeriodEnd ? "cancel_scheduled" : "cancel_withdrawn" };
}

/**
 * The web client opens Billing again. The desktop app reads the license again, so its page only says to go back.
 * `kind` tells the return page which Stripe page closed.
 */
function licenseReturnUrl(origin: string, target: LicenseReturnTarget): string {
  if (target === "web") return `${origin}/app?billing=license`;
  return `${origin}/billing/return?kind=license`;
}

function licenseExists(): BillingError {
  return new BillingError(409, "license_exists", sourceText("error.billing.licenseExists"));
}

function licenseUnavailable(): BillingError {
  return new BillingError(503, "license_unavailable", sourceText("error.billing.licenseUnavailable"));
}

function noLicense(): BillingError {
  return new BillingError(404, "no_license", sourceText("error.billing.noLicense"));
}
