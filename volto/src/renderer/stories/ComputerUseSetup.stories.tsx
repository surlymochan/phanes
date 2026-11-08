import type { ComputerUseState, MacPermissionId } from "@volto/contracts/ipc";
import { onCleanup } from "solid-js";
import type { Meta, StoryObj } from "storybook-solidjs-vite";
import { ComputerUseSetup } from "../src/features/computer-use/ComputerUseSetup";
import { createMockVolto } from "./mock-volto";

function permissions(granted: readonly MacPermissionId[]): ComputerUseState["permissions"] {
  return (["screen-recording", "accessibility"] as const).map((id) => ({ id, granted: granted.includes(id) }));
}

const permissionsRequired: ComputerUseState = {
  status: "permissions-required",
  permissions: permissions([]),
  message: null,
};

function MockedSetup(props: { state?: ComputerUseState; error?: Error; loading?: boolean }) {
  const previousApi = window.volto;
  const mock = createMockVolto();
  mock.api.computerUse.getState = props.loading
    ? () => new Promise(() => undefined)
    : props.error
      ? async () => {
          throw props.error;
        }
      : async () => props.state ?? permissionsRequired;
  window.volto = mock.api;
  onCleanup(() => {
    mock.dispose();
    window.volto = previousApi;
  });
  return (
    <main class="foundation-story foundation-interaction-stage">
      <ComputerUseSetup variant="compact" />
    </main>
  );
}

const meta = {
  title: "Settings/ComputerUseSetup",
  component: ComputerUseSetup,
  parameters: { layout: "fullscreen", a11y: { test: "error" } },
} satisfies Meta<typeof ComputerUseSetup>;

export default meta;
type Story = StoryObj<typeof meta>;

export const PermissionsRequired: Story = {
  render: () => <MockedSetup />,
};

export const PartlyGranted: Story = {
  render: () => <MockedSetup state={{ ...permissionsRequired, permissions: permissions(["screen-recording"]) }} />,
};

export const Ready: Story = {
  render: () => (
    <MockedSetup
      state={{ status: "ready", permissions: permissions(["screen-recording", "accessibility"]), message: null }}
    />
  ),
};

export const Loading: Story = {
  render: () => <MockedSetup loading />,
};

/** A build that carries no driver. A fault of that build, so the panel names no command to run. */
export const DriverMissing: Story = {
  render: () => (
    <MockedSetup
      state={{
        status: "driver-missing",
        permissions: permissions([]),
        message: "This build of Volto carries no Computer Use driver.",
      }}
    />
  ),
};

/** Windows and Linux grant no permission, so a driver that answers is all the panel has to report. */
export const ReadyWithoutPermissions: Story = {
  render: () => <MockedSetup state={{ status: "ready", permissions: [], message: null }} />,
};

export const Failure: Story = {
  render: () => <MockedSetup error={new Error("Volto could not check Computer Use.")} />,
};
