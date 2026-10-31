import { type OpenWaitItem, OpenWaits } from "@openbot/ui/features/conversation/OpenWaits";
import { fn } from "storybook/test";
import type { Meta, StoryObj } from "storybook-solidjs-vite";

/*
 * The block above the composer while an agent waits for something outside its turn: a build, a
 * nightly run, or a script event. A routine run of the agent wakes the wait.
 */

/** A fixed moment, so the times read the same in every screenshot. */
const NOW = new Date(Date.UTC(2026, 9, 12, 14, 30));
const minutesAgo = (minutes: number) => new Date(NOW.getTime() - minutes * 60_000).toISOString();

const items: OpenWaitItem[] = [
  {
    id: "wait-release",
    note: "Release build of 2.14, then upload the DMG and send the link",
    request: "Ship 2.14 and tell me when the DMG is up.",
    openedAt: minutesAgo(14),
    state: "waiting",
  },
  {
    id: "wait-e2e",
    note: "Nightly end-to-end run on the macOS runner, with one retry for timing tests, then post the failures in the release channel",
    request: "Run the full end-to-end suite tonight and post what fails.",
    openedAt: minutesAgo(3),
    state: "waiting",
  },
  {
    id: "wait-pricing",
    note: "Pricing page deploy, then check the new links and finish the launch post",
    request: "Finish the launch post after the pricing page is live.",
    openedAt: minutesAgo(60 * 26),
    state: "woken",
  },
];

const meta = {
  title: "Conversation/Open waits",
  component: OpenWaits,
  render: (args) => (
    <main style={{ width: "min(640px, 100vw)", padding: "var(--openbot-space-4)" }}>
      <OpenWaits {...args} />
    </main>
  ),
  args: { items, now: NOW, onShowRequest: fn(), onCancel: fn() },
  parameters: { layout: "centered", a11y: { test: "error" } },
} satisfies Meta<typeof OpenWaits>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Waiting: Story = {};

/** The release build woke the agent, and its run continues the request. */
export const Resuming: Story = {
  args: { items: items.map((item) => (item.id === "wait-release" ? { ...item, state: "resuming" } : item)) },
};

/** The person closed the block. The header names the first wait. */
export const Collapsed: Story = { args: { defaultOpen: false } };

/** Without a way to show the request, the menu has only Cancel. */
export const CancelOnly: Story = { args: { onShowRequest: undefined } };
