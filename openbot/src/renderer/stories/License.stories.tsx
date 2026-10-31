import type { LicenseDesktopApi } from "@openbot/contracts/ipc";
import { type License, type LicenseState, licenseAmount } from "@openbot/contracts/license";
import { BillingPanel } from "@openbot/ui/features/billing/BillingPanel";
import { createBillingStore } from "@openbot/ui/features/billing/billing-store";
import { LicenseSection } from "@openbot/ui/features/billing/LicenseSection";
import { createLicenseStore } from "@openbot/ui/features/billing/license-store";
import type { Meta, StoryObj } from "storybook-solidjs-vite";
import { createMockBilling, previewBillingServers } from "../src/preview/mock-billing";
import { createMockLicense, previewLicenseUsers } from "../src/preview/mock-license";

const DAY = 24 * 60 * 60_000;

/** Active server plans only, so the failed-payment alert of a server does not hide the license. */
const SERVERS = previewBillingServers().filter((server) => server.status === "active");

function license(users: number, change: Partial<License> = {}): License {
  return {
    subscriptionId: "sub_story_license",
    users,
    currency: "eur",
    amount: licenseAmount(users, "eur"),
    status: "active",
    // One month after the story opens, so the renewal date is never in the past.
    currentPeriodEnd: Date.now() + 30 * DAY,
    cancelAtPeriodEnd: false,
    ...change,
  };
}

/** The Billing panel with the license section above the server plans. */
function LicenseStory(props: { calls: LicenseDesktopApi }) {
  const billing = createBillingStore(
    () => createMockBilling(SERVERS),
    () => true,
  );
  const store = createLicenseStore(
    () => props.calls,
    () => true,
  );
  return (
    <main class="foundation-story" style={{ "max-width": "720px" }}>
      <BillingPanel
        store={billing}
        available
        license={<LicenseSection store={store} onManagePayment={() => undefined} onContactUs={() => undefined} />}
      />
    </main>
  );
}

function story(initial: Partial<LicenseState>): Story {
  return { render: () => <LicenseStory calls={createMockLicense(initial)} /> };
}

const meta = {
  title: "Account/Billing/License",
  component: LicenseStory,
  args: { calls: createMockLicense() },
  parameters: { layout: "fullscreen", a11y: { test: "error" } },
} satisfies Meta<typeof LicenseStory>;

export default meta;
type Story = StoryObj<typeof meta>;

/** No license. "Buy a license" opens the users, the price and "Continue to checkout". */
export const Buy: Story = story({});

/** 4 of 10 users. The row menu has Users, Change users, Update payment method and Cancel. */
export const Active: Story = story({ license: license(10) });

/** More people use the license than it pays for: a warning with "Change users". */
export const OverLimit: Story = story({ license: license(5), used: 7 });

/** The renewal payment failed. */
export const PaymentFailed: Story = story({ license: license(10, { status: "past_due" }) });

/** The license ends at the end of its period. The menu has Renew. */
export const Cancelling: Story = story({ license: license(10, { cancelAtPeriodEnd: true }) });

/** A member of a server whose owner has a license. */
export const CoveredBy: Story = story({
  used: 1,
  users: previewLicenseUsers().slice(0, 1),
  coveredBy: [{ ownerName: "Ana Kowalska", ownerEmail: "ana@example.com" }],
});

/** The license ended. The account can buy a new one. */
export const Ended: Story = story({
  license: license(10, { status: "canceled", currentPeriodEnd: Date.now() - 3 * DAY }),
});

/** The account server has no license price. Only the server plans show. */
export const Unavailable: Story = story({ available: false });
