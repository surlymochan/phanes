import type {
  InstalledSkill,
  MarketplaceAgentSummary,
  MarketplaceSkillQuery,
  MarketplaceSkillSummary,
  McpServerConfig,
} from "@openbot/contracts/ipc";
import type { McpConnectFlow } from "@openbot/contracts/mcp-connect-flows";
import { CONNECTOR_LISTINGS } from "@openbot/ui/features/connectors/connector-catalog";
import { Marketplace } from "@openbot/ui/features/marketplace/Marketplace";
import type { MarketplaceApp, MarketplaceModel } from "@openbot/ui/features/marketplace/marketplace-model";
import {
  createMarketplaceNavigation,
  type MarketplaceNavigation,
} from "@openbot/ui/features/marketplace/marketplace-view";
import { McpConnectDialog } from "@openbot/ui/features/settings/McpConnectDialog";
import type { McpConnectSubject } from "@openbot/ui/features/settings/McpConnectShell";
import { currentText } from "@openbot/ui/text";
import { createSignal, createStore, onSettled, Show, untrack } from "solid-js";
import { fn } from "storybook/test";
import type { Meta, StoryObj } from "storybook-solidjs-vite";
import {
  STORY_AGENT_SUMMARIES,
  STORY_MARKETPLACE_AGENT_DETAILS,
  STORY_MARKETPLACE_AGENTS,
  STORY_MARKETPLACE_PLUGINS,
  STORY_MARKETPLACE_SKILL_DETAILS,
  STORY_MARKETPLACE_SKILLS,
  STORY_MCP_SERVERS,
} from "../src/preview/fixtures";
import { CONNECTED_CONNECTOR_DETAILS, STORY_CONNECTOR_DETAILS, storyConnectorSet } from "./connector-fixtures";
import { CONNECT_OUTCOMES, type ConnectOutcome, connectAs, subjectFor } from "./mcp-connect-stage";

function page<T extends { name: string; description: string; category?: string | undefined }>(
  items: readonly T[],
  query: MarketplaceSkillQuery,
) {
  const text = query.query?.toLowerCase() ?? "";
  return {
    items: items.filter(
      (item) =>
        (!query.category || (item.category ?? "other") === query.category) &&
        (!text || item.name.toLowerCase().includes(text) || item.description.toLowerCase().includes(text)),
    ),
    nextCursor: null,
  };
}

function installed(skill: MarketplaceSkillSummary): InstalledSkill {
  return {
    skillId: skill.id,
    slug: skill.slug,
    name: skill.name,
    installedVersion: skill.version,
    availableVersion: skill.version,
    state: "installed",
    enabled: true,
  };
}

/** What the host holds for a plugin's apps: a working row, or a row whose sign-in the server refused. */
type HeldState = "connected" | "signInNeeded";

interface StoryProps {
  /** A member of a joined server browses and adds nothing. */
  member?: boolean;
  empty?: boolean;
  /** What the app's server answers when Connect opens its dialog. */
  outcome?: ConnectOutcome;
  /** Opens the window on this page, as a link or a click would. */
  start?: (nav: MarketplaceNavigation) => void;
  /** The plugins the host holds rows for, by slug. By default, only the first plugin is connected. */
  held?: Readonly<Record<string, HeldState>>;
  /** Every connector app is set up, so its page shows its rows and its Danger zone. */
  connected?: boolean;
}

const FIRST_HELD: Readonly<Record<string, HeldState>> = { [STORY_MARKETPLACE_PLUGINS[0]?.slug ?? ""]: "connected" };

function pluginStatus(held: HeldState | undefined): MarketplaceApp["status"] {
  if (held === "signInNeeded") return "attention";
  return held ? "connected" : "idle";
}

/** The real window on fixtures. Each action changes the fixture state, so the states can be tried. */
function MarketplaceStory(props: StoryProps) {
  const { member, empty, start, held = FIRST_HELD, connected } = untrack(() => ({ ...props }));
  const details = connected ? CONNECTED_CONNECTOR_DETAILS : STORY_CONNECTOR_DETAILS;
  const agents = empty ? [] : STORY_MARKETPLACE_AGENTS;
  const skills = empty ? [] : STORY_MARKETPLACE_SKILLS;
  const firstSkill = STORY_MARKETPLACE_SKILLS[0];
  const connectors = storyConnectorSet(details);
  // The story fixes the names once, in the language it opens with.
  const connectorApps = untrack(() =>
    details.map(({ id }): MarketplaceApp => {
      const listing = CONNECTOR_LISTINGS[id];
      return {
        kind: "connector",
        id,
        connector: id,
        name: currentText().t(listing.title),
        tagline: currentText().t(listing.tagline),
        status: connected || id === "github" ? "connected" : "idle",
        category: listing.category,
      };
    }),
  );
  const [state, setState] = createStore<{
    added: Record<string, true>;
    installed: Record<string, InstalledSkill[]>;
    apps: MarketplaceApp[];
    busy: Record<string, true>;
    notice: string;
  }>({
    added: { [STORY_MARKETPLACE_AGENTS[1]?.id ?? ""]: true },
    installed: { chief: firstSkill ? [installed(firstSkill)] : [] },
    apps: [
      ...connectorApps,
      ...STORY_MARKETPLACE_PLUGINS.map((plugin): MarketplaceApp => {
        const state = held[plugin.slug];
        return {
          kind: "plugin",
          id: plugin.slug,
          name: plugin.name,
          tagline: plugin.tagline,
          status: pluginStatus(state),
          category: plugin.category,
          plugin,
          connections: Object.fromEntries(
            plugin.apps.map((entry) => [entry.id, { held: Boolean(state), signInNeeded: state === "signInNeeded" }]),
          ),
        };
      }),
      ...STORY_MCP_SERVERS.slice(0, 1).map(
        (server): MarketplaceApp => ({
          kind: "custom",
          id: `custom:${server.id}`,
          name: server.name,
          tagline: server.command,
          status: "connected",
          server,
        }),
      ),
    ],
    busy: {},
    notice: "",
  });
  const [error, setError] = createSignal<string | null>(null);
  const [open, setOpen] = createSignal(true);
  /** The connect dialog on screen, as the Marketplace window opens it for an app's way in. */
  const [pending, setPending] = createSignal<{
    subject: McpConnectSubject;
    flow: McpConnectFlow;
    settle: (config: McpServerConfig | null) => void;
  } | null>(null);
  const connectStep = (subject: McpConnectSubject, flow: McpConnectFlow) =>
    new Promise<McpServerConfig | null>((resolve) =>
      setPending({
        subject,
        flow,
        settle: (config) => {
          setPending(null);
          resolve(config);
        },
      }),
    );
  /** The server the dialog talks to. A sign-in waits a moment, as a browser round trip does. */
  const server = (name: string) => connectAs(props.outcome ?? "connects", name);
  const signIn = (name: string) => async (config: McpServerConfig) => {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    return server(name)(config);
  };
  /** Marks the plugin's apps as held or not, and sets the status that follows from them. */
  const setHeld = (id: string, appIds: readonly string[], on: boolean) =>
    setState((draft) => {
      const app = draft.apps.find((entry) => entry.id === id);
      if (app?.kind !== "plugin") return;
      for (const appId of appIds) app.connections = { ...app.connections, [appId]: { held: on, signInNeeded: false } };
      const connections = Object.values(app.connections);
      if (connections.some((connection) => connection.signInNeeded)) app.status = "attention";
      else app.status = connections.some((connection) => connection.held) ? "connected" : "idle";
    });

  const setBusy = (id: string, on: boolean) => {
    setState((draft) => {
      if (on) draft.busy[id] = true;
      else delete draft.busy[id];
    });
  };

  const model: MarketplaceModel = {
    listAgents: async (query) => page<MarketplaceAgentSummary>(agents, query),
    loadAgent: async (id) => STORY_MARKETPLACE_AGENT_DETAILS[id],
    agentState: (listing) => (member ? null : state.added[listing.id] ? "added" : "add"),
    agentBusy: (id) => Boolean(state.busy[id]),
    addAgent: async (listing) => {
      setState((draft) => {
        draft.added[listing.id] = true;
        draft.notice = `${listing.name} added.`;
      });
      return true;
    },
    openChat: () => setOpen(false),

    listSkills: async (query) => page<MarketplaceSkillSummary>(skills, query),
    loadSkill: async (id) => STORY_MARKETPLACE_SKILL_DETAILS[id],
    agents: () => (member ? [] : STORY_AGENT_SUMMARIES),
    activeAgentId: () => STORY_AGENT_SUMMARIES[0]?.id ?? "",
    readSkills: () => undefined,
    skillRead: () => "loaded",
    installedSkill: (agentId, skillId) => state.installed[agentId]?.find((skill) => skill.skillId === skillId),
    skillBusy: () => false,
    setSkill: async (skill, agentIds, on) =>
      setState((draft) => {
        for (const agentId of agentIds) {
          const list = (draft.installed[agentId] ?? []).filter((entry) => entry.skillId !== skill.id);
          draft.installed[agentId] = on ? [...list, installed(skill)] : list;
        }
      }),
    trySkill: fn(),

    apps: () => state.apps,
    canConnectApps: () => !member,
    appBusy: (id) => Boolean(state.busy[id]),
    connectApp: async (app) => {
      setBusy(app.id, true);
      for (const entry of app.plugin.apps) {
        const flow = (entry.server.auth ?? [])[0];
        // Closing the dialog is a decision, not a failure: the app stays as it was.
        if (flow && !(await connectStep(subjectFor(entry), flow))) {
          setBusy(app.id, false);
          return false;
        }
      }
      setBusy(app.id, false);
      setHeld(
        app.id,
        app.plugin.apps.map((entry) => entry.id),
        true,
      );
      return true;
    },
    reconnectApp: async (app, appId) => {
      const entry = app.plugin.apps.find((one) => one.id === appId);
      const flow = entry?.server.auth?.[0];
      if (!entry || !flow) return false;
      setBusy(app.id, true);
      const config = await connectStep(subjectFor(entry), flow);
      setBusy(app.id, false);
      if (config) setHeld(app.id, [appId], true);
      return Boolean(config);
    },
    disconnectApp: (app) =>
      setHeld(
        app.id,
        app.plugin.apps.map((entry) => entry.id),
        false,
      ),
    removeServer: async (id) => {
      setState((draft) => {
        draft.apps = draft.apps.filter((app) => app.id !== id && !(app.kind === "custom" && app.server.id === id));
      });
      return true;
    },
    addServer: member ? undefined : fn(),
    runPrompt: fn(),
    copyLink: fn(),
    openUrl: fn(),
    connector: (id) => connectors[id],

    error,
    clearError: () => setError(null),
    notice: () => state.notice,
  };
  const nav = createMarketplaceNavigation();
  // A write in the component body is refused, so the start page opens once the window is up.
  onSettled(() => start?.(nav));
  return (
    <>
      <Marketplace model={model} nav={nav} open={open()} onOpenChange={setOpen} />
      <Show when={pending()} keyed>
        {(step) => (
          <McpConnectDialog
            subject={step.subject}
            flow={step.flow}
            onTest={server(step.subject.name)}
            onSignIn={signIn(step.subject.name)}
            onConnected={(config) => step.settle(config)}
            onCancel={() => step.settle(null)}
            onOpenUrl={fn()}
          />
        )}
      </Show>
    </>
  );
}

const meta = {
  title: "Settings/Marketplace",
  component: MarketplaceStory,
  args: { outcome: "connects" },
  argTypes: { outcome: { control: "select", options: CONNECT_OUTCOMES } },
  parameters: {
    layout: "fullscreen",
    a11y: { test: "error" },
    viewport: {
      options: {
        marketplaceDesktop: { name: "Marketplace — 1280 × 880", styles: { width: "1280px", height: "880px" } },
        marketplaceNarrow: { name: "Marketplace — 720 × 780", styles: { width: "720px", height: "780px" } },
      },
    },
  },
} satisfies Meta<typeof MarketplaceStory>;

export default meta;
type Story = StoryObj<typeof meta>;

const tab = (value: "agents" | "skills") => (nav: MarketplaceNavigation) =>
  nav.set((draft) => {
    draft.tab = value;
  });

/** A story that opens the window on the page `start` goes to. */
const at = (start: (nav: MarketplaceNavigation) => void, props: Omit<StoryProps, "start"> = {}): Story => ({
  render: (args) => <MarketplaceStory {...props} outcome={args.outcome} start={start} />,
});

export const Apps: Story = {};

export const Narrow: Story = { parameters: { viewport: { defaultViewport: "marketplaceNarrow" } } };

export const Agents = at(tab("agents"));

export const PluginApp = at((nav) => {
  nav.go({ kind: "app", id: STORY_MARKETPLACE_PLUGINS[0]?.slug ?? "" });
});

const plugin = (slug: string) => (nav: MarketplaceNavigation) => nav.go({ kind: "app", id: slug });

/** A key app: the page says which key the host saved, and never shows the key. */
export const PluginKey = at(plugin("resend"), { held: { resend: "connected" } });

/** The server refused the saved sign-in, so the page asks the user to sign in again. */
export const PluginSignInNeeded = at(plugin("notion"), { held: { notion: "signInNeeded" } });

/** A desktop app: the server runs in an app on this computer, at a local address. */
export const PluginDesktop = at(plugin("figma"), { held: { figma: "connected" } });

export const OnePassword = at((nav) => {
  nav.go({ kind: "app", id: "onepassword" });
});

export const GitHub = at((nav) => {
  nav.go({ kind: "app", id: "github" });
});

export const CustomServer = at((nav) => {
  nav.go({ kind: "app", id: `custom:${STORY_MCP_SERVERS[0]?.id ?? ""}` });
});

export const Skills = at(tab("skills"));

export const SkillPage = at((nav) => {
  const listing = STORY_MARKETPLACE_SKILLS[0];
  tab("skills")(nav);
  if (listing) nav.go({ kind: "skill", listing });
});

export const AgentPage = at((nav) => {
  const listing = STORY_MARKETPLACE_AGENTS[0];
  tab("agents")(nav);
  if (listing) nav.go({ kind: "agent", listing });
});

export const NoMatch = at((nav) =>
  nav.set((draft) => {
    draft.query = "no listing has this name";
  }),
);

export const EmptyCatalog = at(tab("agents"), { empty: true });

/** A member of a joined server: no Add, no Add custom, no install menu and no Connect. */
export const Member: Story = { args: { member: true } };

const connector = (id: string) => at((nav) => nav.go({ kind: "app", id }), { connected: true });

/** A chat channel: one connected Slack workspace. */
export const SlackConnected = connector("slack");

/** A chat channel: a group and a direct chat. */
export const TelegramConnected = connector("telegram");

/** A password vault, connected. */
export const OnePasswordConnected = connector("onepassword");

export const BitwardenConnected = connector("bitwarden");
