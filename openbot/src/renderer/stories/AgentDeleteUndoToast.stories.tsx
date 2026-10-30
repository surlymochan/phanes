import { Button, Toaster, toast } from "@openbot/ui";
import { AgentDeleteUndoToast, startUndoCountdown } from "@openbot/ui/features/agents/AgentDeleteUndoToast";
import { onCleanup } from "solid-js";
import type { Meta, StoryObj } from "storybook-solidjs-vite";
import { requireFixture, STORY_AGENTS } from "./fixtures";

const meta = {
  title: "Agents/Agent delete undo toast",
  parameters: { layout: "fullscreen", a11y: { test: "error" } },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

const agent = {
  ...requireFixture(
    STORY_AGENTS.find((candidate) => candidate.id === "research"),
    "research agent",
  ),
  title: "Analyst",
};

function raise(): void {
  const id = `story-agent-delete-${agent.id}`;
  const close = () => {
    countdown.stop();
    queueMicrotask(() => toast.dismiss(id));
  };
  const countdown = startUndoCountdown(close);
  toast(() => <AgentDeleteUndoToast agent={agent} countdown={countdown} onUndo={close} />, {
    id,
    duration: Number.POSITIVE_INFINITY,
    unstyled: true,
    closeButton: false,
    class: "agent-delete-undo-toast-host",
  });
}

/** The toast after a confirmed agent delete. The bar empties in 8 s, and stops while the pointer is on the toast. */
export const AfterDelete: Story = {
  render: () => {
    onCleanup(() => toast.dismiss());
    return (
      <main class="foundation-story">
        <Button type="button" onClick={raise}>
          Delete {agent.name}
        </Button>
        <Toaster />
      </main>
    );
  },
};
