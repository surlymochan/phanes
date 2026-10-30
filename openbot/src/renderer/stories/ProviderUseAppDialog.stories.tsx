/**
 * Connect in a browser tab, for a provider whose sign-in only the OpenBot app can finish.
 *
 * The provider's page returns to the computer that opened it, and a tab cannot receive it. Its row's
 * Connect opens this dialog in place of a sign-in that would end on a dead page.
 */

import { ProviderUseAppDialog } from "@openbot/ui/components/ProviderUseAppDialog";
import { fn } from "storybook/test";
import type { Meta, StoryObj } from "storybook-solidjs-vite";

const meta = {
  title: "Auth/ProviderUseAppDialog",
  component: ProviderUseAppDialog,
  args: {
    open: true,
    providerName: "Claude",
    onDownload: fn(),
    onClose: fn(),
  },
  parameters: { layout: "fullscreen" },
} satisfies Meta<typeof ProviderUseAppDialog>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Claude on a server with no pasted-code route. */
export const Claude: Story = {};

/** Antigravity on a server that has no page of its own to show. */
export const Antigravity: Story = { args: { providerName: "Antigravity" } };
