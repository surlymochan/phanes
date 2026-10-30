import type { ServerSummary } from "@openbot/contracts/ipc";
import { Button, Toaster } from "@openbot/ui";
import { RestartServerDialog } from "@openbot/ui/features/servers/RestartServerDialog";
import { createSignal, onCleanup } from "solid-js";
import type { Meta, StoryObj } from "storybook-solidjs-vite";

type StoryServer = Pick<ServerSummary, "id" | "name" | "state" | "hostedIssue">;

const SERVER: StoryServer = { id: "research-hosted", name: "Research server", state: "online", hostedIssue: null };

/**
 * The server goes offline about 1.5 seconds after the request, and comes back 4 seconds later. A failed
 * start reports `start_timeout`, as the desktop does after its start limit.
 */
function RestartStory(props: { outcome: "online" | "failed" | "request-failed" }) {
  const [server, setServer] = createSignal<StoryServer>(SERVER);
  const [open, setOpen] = createSignal(true);
  const timers = new Set<ReturnType<typeof setTimeout>>();
  onCleanup(() => {
    for (const timer of timers) clearTimeout(timer);
  });
  function later(ms: number, change: Partial<StoryServer>): void {
    const timer = setTimeout(() => setServer((current) => ({ ...current, ...change })), ms);
    timers.add(timer);
  }
  async function restart(): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, 600));
    if (props.outcome === "request-failed")
      throw new Error("The server cannot restart now. Wait a moment, then try again.");
    later(1500, { state: "connecting", hostedIssue: null });
    if (props.outcome === "online") later(5500, { state: "online" });
    else later(5500, { state: "offline", hostedIssue: "start_timeout" });
  }
  return (
    <>
      <Button type="button" variant="outline" onClick={() => setOpen(true)}>
        Restart server
      </Button>
      <RestartServerDialog
        server={open() ? server() : null}
        servers={[server()]}
        onClose={() => setOpen(false)}
        onRestart={restart}
      />
      <Toaster />
    </>
  );
}

const meta = {
  title: "Team/RestartServerDialog",
  parameters: { layout: "centered", a11y: { test: "error" } },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

/** Restart, then follow the server until it is online again. Hide keeps the restart in a toast. */
export const BackOnline: Story = { render: () => <RestartStory outcome="online" /> };

/** The server stops but does not start again. */
export const DidNotStart: Story = { render: () => <RestartStory outcome="failed" /> };

/** The account service refuses the restart, so the confirmation shows the error. */
export const RequestFailed: Story = { render: () => <RestartStory outcome="request-failed" /> };
