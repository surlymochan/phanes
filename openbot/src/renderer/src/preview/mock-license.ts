import type { LicenseDesktopApi } from "@openbot/contracts/ipc";
import { LICENSE_INCLUDED_USERS, type LicenseState, type LicenseUser, licenseAmount } from "@openbot/contracts/license";
import { sourceText } from "@openbot/i18n/source";

const DAY = 24 * 60 * 60_000;

/** The owner and the members of the preview servers. */
export function previewLicenseUsers(): LicenseUser[] {
  return [
    { name: "Ana Kowalska", email: "ana@example.com", servers: ["Office server", "Lab"], owner: true },
    { name: "Jan Nowak", email: "jan@example.com", servers: ["Office server"], owner: false },
    { name: null, email: "maria.silva@example.com", servers: ["Lab"], owner: false },
    { name: "Kenji Sato", email: "kenji@example.com", servers: ["Office server", "Lab"], owner: false },
  ];
}

/**
 * The preview account has no license unless `initial` gives one. Checkout acts as Stripe would after
 * payment: it makes an active license. Users and renewal change it as the account server does.
 */
export function createMockLicense(initial: Partial<LicenseState> = {}): LicenseDesktopApi {
  const users = previewLicenseUsers();
  const state: LicenseState = {
    available: true,
    currency: "eur",
    license: null,
    used: users.length,
    users,
    coveredBy: [],
    ...initial,
  };
  return {
    getState: async () => structuredClone(state),
    openCheckout: async (request) => {
      state.license = {
        subscriptionId: "sub_preview_license",
        users: request.users,
        currency: request.currency,
        amount: licenseAmount(request.users, request.currency),
        status: "active",
        currentPeriodEnd: Date.now() + 30 * DAY,
        cancelAtPeriodEnd: false,
      };
    },
    setUsers: async (request) => {
      const license = state.license;
      if (!license) throw new Error(sourceText("error.billing.noLicense"));
      if (request.users < Math.max(LICENSE_INCLUDED_USERS, state.used)) {
        throw new Error(sourceText("error.billing.licenseUsersInUse"));
      }
      license.users = request.users;
      license.amount = licenseAmount(request.users, license.currency);
      return structuredClone(state);
    },
    setRenewal: async (request) => {
      if (!state.license) throw new Error(sourceText("error.billing.noLicense"));
      state.license.cancelAtPeriodEnd = request.cancel;
      return structuredClone(state);
    },
  };
}
