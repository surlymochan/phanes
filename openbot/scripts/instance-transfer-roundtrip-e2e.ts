// Run: bun scripts/instance-transfer-roundtrip-e2e.ts
// Report: .openbot-build/instance-transfer-roundtrip-e2e.json
//
// The export of an instance and the import of that file into a second computer. The check drives
// the real transfer service on both sides: a synthetic profile with an agent, its workspace files,
// a skill, the shared folder and the settings is exported, and the file is imported into a second
// synthetic profile that has an agent of its own. The second profile uses the real local skill
// library, so a skill is published there as a revision of its own.
//
// The check holds that the workspace files travel with their bytes and their mode, that the skill
// is published in the second profile with the same content, that the shared file is copied and a
// file that is already there is kept, that no secret travels, that an agent that cannot be created
// is reported and the other agent is imported, and that the imported agent has an id of its own.
//
// Every file of the check is synthetic and stays in the report directory. The check removes
// nothing. The report names the directory with the files of the check.

import assert from "node:assert/strict";
import { chmod, mkdir, mkdtemp, readFile, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type {
  AgentMemory,
  AgentSummary,
  CustomAgentSummary,
  CustomProviderSummary,
  McpServerConfig,
  Routine,
  SidebarLayoutSnapshot,
} from "@openbot/contracts/ipc";
import { sourceText } from "@openbot/i18n/source";
import { Effect } from "effect";
import { unzipSync } from "fflate";
import { executableEntries, sha256 } from "../src/main/agent-archive";
import {
  agentPrefix,
  decodeTransferManifest,
  INSTANCE_TRANSFER_MANIFEST,
} from "../src/main/instance-transfer-manifest";
import { type InstanceTransferOptions, InstanceTransferService } from "../src/main/instance-transfer-service";
import { LocalSkillLibrary } from "../src/main/local-skill-library";
import { SkillMarketplaceFailure } from "../src/main/skill-marketplace-service";
import { normalizedFiles } from "../src/main/skill-package";

const run = Effect.runPromise;

/** The value that must not travel in the file or in the second profile. */
const SECRET = "synthetic-secret-value";
/** The agent of the first profile that travels. */
const SOURCE_AGENT_ID = "agent-source-1";
/** The agent of the first profile that the second profile cannot create. */
const BROKEN_AGENT_ID = "agent-source-2";
/** The agent of the second profile that is there before the import. */
const TARGET_AGENT_ID = "agent-target-1";
/** The name of the travelling agent. The second profile has an agent of the same name. */
const AGENT_NAME = "Transfer Agent";
/** The name of the agent that the second profile cannot create. */
const BROKEN_AGENT_NAME = "Broken Agent";
/** The slug of the skill that travels. */
const SKILL_SLUG = "transfer-skill";

/** The text of the skill that travels. The name gives the slug of the skill. */
const SKILL_TEXT = [
  "---",
  "name: Transfer Skill",
  "description: A synthetic skill for the instance transfer check.",
  "---",
  "",
  "The body of the synthetic skill.",
  "",
].join("\n");

/** The workspace file that travels. */
const NOTES_TEXT = "# Notes\n\nA synthetic note for the instance transfer check.\n";

function summary(overrides: Partial<AgentSummary> & { id: string; name: string; workspacePath: string }): AgentSummary {
  return {
    title: "",
    description: "",
    notifications: false,
    provider: "codex",
    model: "gpt-5.5",
    reasoningEffort: "medium",
    threadId: null,
    preview: "",
    updatedAt: null,
    avatarSeed: overrides.id,
    avatarHue: null,
    avatarUrl: null,
    ...overrides,
  };
}

/** The routine of the first profile. The second profile cannot create it. */
function routine(agentId: string): Routine {
  return {
    id: "routine-1",
    agentId,
    name: "Morning report",
    instruction: "Report the synthetic day.",
    active: true,
    timezone: "UTC",
    trigger: {
      id: "trigger-1",
      routineId: "routine-1",
      schedule: { kind: "daily", time: "08:00" },
      nextRunAt: "2026-10-29T08:00:00.000Z",
      createdAt: "2026-10-28T00:00:00.000Z",
      updatedAt: "2026-10-28T00:00:00.000Z",
    },
    createdAt: "2026-10-28T00:00:00.000Z",
    updatedAt: "2026-10-28T00:00:00.000Z",
  };
}

/** The memory of the first profile. */
function memory(agentId: string): AgentMemory {
  return {
    id: "memory-1",
    agentId,
    text: "The synthetic memory of the transfer check.",
    origin: "manual",
    sourceTurnId: null,
    createdAt: "2026-10-28T00:00:00.000Z",
    updatedAt: "2026-10-28T00:00:00.000Z",
  };
}

/** The MCP server of the first profile. It holds a secret in its argument and in its value. */
function mcpServer(): McpServerConfig {
  return {
    id: "mcp-1",
    name: "Local MCP",
    transport: "stdio",
    enabled: true,
    command: "node",
    args: ["server.js", `--token=${SECRET}`],
    env: [{ key: "API_KEY", value: SECRET }],
    envPassthrough: [],
    workingDirectory: "",
    url: "",
    headers: [{ key: "X-Secret", value: SECRET }],
  };
}

/** The custom provider of the first profile. It holds a secret in its address. */
function customProvider(): CustomProviderSummary {
  return {
    id: "provider-1",
    name: "Local Provider",
    baseUrl: `https://provider.invalid/v1?key=${SECRET}`,
    hasApiKey: true,
    models: [{ id: "model-1", name: "Model One" }],
  };
}

/** The custom agent of the first profile. It holds a secret in its argument. */
function customAgent(): CustomAgentSummary {
  return {
    id: "custom-1",
    name: "Local Agent",
    command: "local-agent",
    args: ["--token", SECRET],
    envNames: ["API_KEY"],
    resolvedCommand: null,
  };
}

/** The sidebar of the first profile. */
function sourceSidebar(): SidebarLayoutSnapshot {
  return {
    revision: 1,
    sections: [{ id: "section-team", name: "Team" }],
    order: ["section-team"],
    agentAssignments: { [SOURCE_AGENT_ID]: "section-team" },
    agentOrder: [SOURCE_AGENT_ID],
  };
}

/**
 * The first profile. It exports one agent with its files, its skill, its memory and its routine,
 * and a second agent that the second profile cannot create.
 */
function sourceService(workspace: string, sharedRoot: string): InstanceTransferService {
  const agents: InstanceTransferOptions["agents"] = {
    listAgents: () => [
      summary({ id: SOURCE_AGENT_ID, name: AGENT_NAME, workspacePath: workspace }),
      summary({ id: BROKEN_AGENT_ID, name: BROKEN_AGENT_NAME, workspacePath: workspace }),
    ],
    listRoutines: (agentId) => (agentId === SOURCE_AGENT_ID ? [routine(agentId)] : []),
    listMemories: (agentId) => (agentId === SOURCE_AGENT_ID ? [memory(agentId)] : []),
    getMemorySelection: (agentId) => ({
      selections:
        agentId === SOURCE_AGENT_ID
          ? [{ memoryId: "memory-1", inclusion: "essential", userControlled: true, revision: 1 }]
          : [],
    }),
    setMemoryInclusion: () => {
      throw new Error("The export does not change a memory selection.");
    },
    resolveAvatar: () => null,
    listModels: () => [{ provider: "codex", id: "gpt-5.5" }],
    createAgentProfile: () => {
      throw new Error("The export does not create an agent.");
    },
    updateAgent: () => {
      throw new Error("The export does not change an agent.");
    },
    createRoutine: () => {
      throw new Error("The export does not create a routine.");
    },
    createMemory: () => {
      throw new Error("The export does not create a memory.");
    },
    memoryLimit: () => 32,
    initializeMemorySelection: () => {
      throw new Error("The export does not choose a memory.");
    },
    setAvatar: () => {
      throw new Error("The export does not set an avatar.");
    },
    deleteAgent: () => {
      throw new Error("The export does not delete an agent.");
    },
  };
  const settings: InstanceTransferOptions["settings"] = {
    mcpServers: () => [mcpServer()],
    saveMcpServer: () => {
      throw new Error("The export does not save an MCP server.");
    },
    customProviders: () => [customProvider()],
    saveCustomProvider: () => {
      throw new Error("The export does not save a custom provider.");
    },
    customAgents: () => Effect.succeed([customAgent()]),
    providersOff: () => [],
    sidebar: () => sourceSidebar(),
    mutateSidebar: () => {
      throw new Error("The export does not change the sidebar.");
    },
    routineFlowLinks: () => Effect.succeed([]),
    connectRoutineFlow: () => {
      throw new Error("The export does not connect a routine flow.");
    },
    signedInProviders: () => ["codex", "claude"],
  };
  const options: InstanceTransferOptions = {
    sharedRoot,
    appVersion: "e2e",
    agents,
    skills: {
      library: () => {
        throw new Error("The export does not publish a skill.");
      },
      installLocal: () => {
        throw new Error("The export does not install a skill.");
      },
    },
    settings,
    timezone: () => "UTC",
  };
  return new InstanceTransferService(options);
}

/** What the second profile recorded while the import ran. */
interface TargetRecord {
  agents: AgentSummary[];
  servers: McpServerConfig[];
  providers: Array<{ id: string; name: string; baseUrl: string; apiKey: string | null }>;
  installed: Array<{ agentId: string; skillId: string; revision: number }>;
  layout: SidebarLayoutSnapshot;
}

/**
 * The second profile. It has an agent of its own, and it uses the real local skill library, so a
 * skill that the import publishes is a revision of its own.
 */
function targetService(root: string, record: TargetRecord): InstanceTransferService {
  const workspaceRoot = join(root, "workspaces");
  const library = new LocalSkillLibrary(join(root, "skills"), () => record.agents);
  let created = 0;
  const agents: InstanceTransferOptions["agents"] = {
    listAgents: () => record.agents,
    listRoutines: () => [],
    listMemories: () => [],
    getMemorySelection: () => ({ selections: [] }),
    setMemoryInclusion: () => undefined,
    resolveAvatar: () => null,
    listModels: () => [{ provider: "codex", id: "gpt-5.5" }],
    createAgentProfile: (input) => {
      if (input.name === BROKEN_AGENT_NAME) return Effect.fail(new Error("The synthetic agent cannot be created."));
      created += 1;
      const id = `agent-imported-${created}`;
      const workspacePath = join(workspaceRoot, id);
      const agent = summary({
        id,
        name: input.name,
        workspacePath,
        title: input.title ?? "",
        description: input.description,
        avatarSeed: input.avatarSeed,
        avatarHue: input.avatarHue,
        provider: input.provider ?? "codex",
        model: input.model ?? "gpt-5.5",
        reasoningEffort: input.reasoningEffort ?? "medium",
      });
      record.agents.push(agent);
      return Effect.succeed(agent);
    },
    updateAgent: (input) => {
      const agent = record.agents.find((candidate) => candidate.id === input.agentId);
      if (!agent) return Effect.fail(new Error("The agent is not in this profile."));
      const updated = { ...agent, notifications: input.notifications ?? agent.notifications };
      record.agents = record.agents.map((candidate) => (candidate.id === updated.id ? updated : candidate));
      return Effect.succeed(updated);
    },
    createRoutine: (input) => {
      // The second profile refuses this routine, so the import reports it and keeps the agent.
      if (input.name === "Morning report") throw new Error("The synthetic routine cannot be created.");
      return { id: `routine-imported-${input.agentId}` };
    },
    createMemory: (input) => ({ id: `memory-imported-${input.agentId}` }),
    memoryLimit: () => 32,
    initializeMemorySelection: () => undefined,
    setAvatar: (agentId) => {
      const agent = record.agents.find((candidate) => candidate.id === agentId);
      if (!agent) return Effect.fail(new Error("The agent is not in this profile."));
      return Effect.succeed(agent);
    },
    deleteAgent: (agentId) => {
      record.agents = record.agents.filter((candidate) => candidate.id !== agentId);
      return Effect.void;
    },
  };
  const settings: InstanceTransferOptions["settings"] = {
    mcpServers: () => record.servers,
    saveMcpServer: (config) => {
      record.servers.push(config);
      return Effect.succeed(config);
    },
    customProviders: () => [],
    saveCustomProvider: (input) => {
      record.providers.push({
        id: input.id,
        name: input.name,
        baseUrl: input.baseUrl,
        apiKey: input.apiKey,
      });
      return Effect.succeed(input);
    },
    customAgents: () => Effect.succeed([]),
    providersOff: () => [],
    sidebar: () => record.layout,
    mutateSidebar: (action) => {
      if (action.type === "create") {
        const section = { id: `section-${record.layout.sections.length + 1}`, name: action.name };
        record.layout = {
          ...record.layout,
          revision: record.layout.revision + 1,
          sections: [...record.layout.sections, section],
          order: [...record.layout.order, section.id],
        };
        return Effect.succeed(record.layout);
      }
      if (action.type === "assign") {
        const assignments = { ...record.layout.agentAssignments };
        if (action.sectionId) assignments[action.agentId] = action.sectionId;
        else delete assignments[action.agentId];
        record.layout = {
          ...record.layout,
          revision: record.layout.revision + 1,
          agentAssignments: assignments,
        };
        return Effect.succeed(record.layout);
      }
      return Effect.succeed(record.layout);
    },
    routineFlowLinks: () => Effect.succeed([]),
    connectRoutineFlow: () => Effect.succeed(undefined),
    signedInProviders: () => ["codex"],
  };
  const skills: InstanceTransferOptions["skills"] = {
    library: () => library,
    installLocal: (input) =>
      Effect.gen(function* () {
        const agent = record.agents.find((candidate) => candidate.id === input.agentId);
        if (!agent)
          return yield* Effect.fail(
            new SkillMarketplaceFailure({ cause: new Error("The agent is not in this profile.") }),
          );
        const detail = yield* library
          .get(input.skillId, input.revision)
          .pipe(Effect.mapError((cause) => new SkillMarketplaceFailure({ cause })));
        const bundle = yield* library
          .bundle(input.skillId, input.revision)
          .pipe(Effect.mapError((cause) => new SkillMarketplaceFailure({ cause })));
        const files = normalizedFiles(bundle);
        // The app writes the skill into the workspace of the agent, so a provider finds it there.
        const folder = join(agent.workspacePath, ".agents", "skills", detail.slug);
        yield* Effect.promise(() => mkdir(folder, { recursive: true }));
        for (const [name, data] of Object.entries(files))
          yield* Effect.promise(() => writeFile(join(folder, name), data));
        record.installed.push({ agentId: input.agentId, skillId: input.skillId, revision: input.revision });
      }),
  };
  const options: InstanceTransferOptions = {
    sharedRoot: join(root, "shared"),
    appVersion: "e2e",
    agents,
    skills,
    settings,
    timezone: () => "UTC",
  };
  return new InstanceTransferService(options);
}

/** The files of the workspace of the first profile. */
async function seedWorkspace(workspace: string): Promise<void> {
  await mkdir(join(workspace, ".agents", "skills", SKILL_SLUG), { recursive: true });
  await writeFile(join(workspace, "notes.md"), NOTES_TEXT);
  const script = join(workspace, "run.sh");
  await writeFile(script, "#!/bin/sh\necho synthetic\n");
  await chmod(script, 0o755);
  await writeFile(join(workspace, ".agents", "skills", SKILL_SLUG, "SKILL.md"), SKILL_TEXT);
  await writeFile(join(workspace, ".agents", "skills", SKILL_SLUG, "helper.txt"), "helper\n");
}

/** The files of the shared folder of the first profile. */
async function seedShared(sharedRoot: string): Promise<void> {
  await mkdir(join(sharedRoot, "Data"), { recursive: true });
  await writeFile(join(sharedRoot, "new.txt"), "shared new\n");
  await writeFile(join(sharedRoot, "kept.txt"), "shared from the source\n");
  await writeFile(join(sharedRoot, "Data", "tables.db"), "synthetic tables\n");
}

/** The shared folder of the second profile. It has a file with the same name as one of the source. */
async function seedTargetShared(sharedRoot: string): Promise<void> {
  await mkdir(sharedRoot, { recursive: true });
  await writeFile(join(sharedRoot, "kept.txt"), "shared from this computer\n");
}

/** What the roundtrip holds, for the report. */
interface RoundtripResult {
  archive: string;
  importedAgentId: string;
  importedWorkspace: string;
  publishedSkill: string;
  warnings: string[];
  skipped: Array<{ key: string; name: string; reason: string }>;
}

async function checkRoundtrip(root: string): Promise<RoundtripResult> {
  const sourceWorkspace = join(root, "source", "workspace");
  const sourceShared = join(root, "source", "shared");
  const targetRoot = join(root, "target");
  const targetShared = join(targetRoot, "shared");
  await seedWorkspace(sourceWorkspace);
  const path = join(sourceWorkspace, "transfer.zip");
  await writeFile(`${path}.part`, "stale partial export\n");
  await seedShared(sourceShared);
  await seedTargetShared(targetShared);
  await mkdir(targetRoot, { recursive: true });

  const source = sourceService(sourceWorkspace, sourceShared);
  const exported = await run(
    source.exportTo(path, {
      agentIds: [SOURCE_AGENT_ID, BROKEN_AGENT_ID],
      includeFiles: true,
      includeSettings: true,
    }),
  );
  assert.equal(exported.saved, true, "The export did not save a file.");

  // The file holds the agents, their files and the settings, and no secret.
  const bytes = new Uint8Array(await readFile(path));
  assert.ok(bytes.byteLength < 1_000_000, "The export is not bounded in size.");
  assert.ok(
    !Buffer.from(bytes).includes(SECRET),
    "The file holds the secret of the MCP server, the provider or the custom agent.",
  );
  const entries = unzipSync(bytes);
  assert.ok(!entries[`${agentPrefix(SOURCE_AGENT_ID)}workspace/transfer.zip`], "The archive holds its output file.");
  assert.ok(
    !entries[`${agentPrefix(SOURCE_AGENT_ID)}workspace/transfer.zip.part`],
    "The archive holds its temporary output file.",
  );
  const manifestBytes = entries[INSTANCE_TRANSFER_MANIFEST];
  assert.ok(manifestBytes, "The file holds no manifest.");
  const decoded = decodeTransferManifest(manifestBytes);
  assert.equal(decoded.manifest.agents.length, 2, "The file holds another number of agents.");
  const [travelling] = decoded.manifest.agents;
  assert.ok(travelling, "The file holds no agent.");
  assert.equal(travelling.key, SOURCE_AGENT_ID, "The file holds another agent key.");
  const folder = travelling.files;
  assert.ok(folder, "The file holds no folder of files.");
  assert.equal(folder, `${agentPrefix(SOURCE_AGENT_ID)}workspace`, "The file holds another folder.");
  assert.ok(entries[`${folder}/notes.md`], "The file holds no note of the workspace.");
  assert.ok(entries[`${folder}/run.sh`], "The file holds no script of the workspace.");
  const notes = entries[`${folder}/notes.md`];
  assert.ok(notes, "The file holds no note of the workspace.");
  assert.equal(
    sha256(notes),
    sha256(new TextEncoder().encode(NOTES_TEXT)),
    "The file holds another content of the note.",
  );
  assert.ok(executableEntries(bytes).has(`${folder}/run.sh`), "The file lost the mode of the script.");
  assert.ok(entries[`shared/new.txt`], "The file holds no file of the shared folder.");
  assert.ok(entries[`shared/kept.txt`], "The file holds no file of the shared folder.");
  assert.ok(!entries["shared/Data/tables.db"], "The file holds the tables of the shared folder.");
  assert.ok(travelling.skills.length === 1, "The file holds another number of skills.");
  const [skillFolder] = travelling.skills;
  assert.ok(skillFolder, "The file holds no skill.");
  assert.ok(skillFolder.endsWith(SKILL_SLUG), "The file holds another skill.");

  // The settings of the file hold no secret, and the file names what it removed.
  const settings = decoded.manifest.settings;
  assert.ok(settings, "The file holds no settings.");
  const [server] = settings.mcpServers;
  assert.ok(server, "The file holds no MCP server.");
  assert.equal(server.env[0]?.value, "", "The file holds the value of an environment variable.");
  assert.equal(server.headers[0]?.value, "", "The file holds the value of a header.");
  assert.ok(
    !server.args.some((argument) => argument.includes(SECRET)),
    "The file holds the secret of the argument of the MCP server.",
  );
  assert.ok(
    !settings.customProviders[0]?.baseUrl.includes(SECRET),
    "The file holds the secret of the address of the custom provider.",
  );
  assert.ok(
    !settings.customAgents[0]?.args.some((argument) => argument.includes(SECRET)),
    "The file holds the secret of the argument of the custom agent.",
  );
  assert.ok(
    (settings.cleared ?? []).includes("Local MCP"),
    "The file does not name the MCP server whose values it removed.",
  );

  // The second profile imports the file.
  const record: TargetRecord = {
    agents: [summary({ id: TARGET_AGENT_ID, name: AGENT_NAME, workspacePath: join(targetRoot, "existing") })],
    servers: [],
    providers: [],
    installed: [],
    layout: { revision: 1, sections: [], order: [], agentAssignments: {}, agentOrder: [] },
  };
  const target = targetService(targetRoot, record);
  const staged = await run(target.stage(path));
  assert.equal(staged.agents.length, 2, "The preview offers another number of agents.");
  const preview = staged.agents.find((agent) => agent.key === SOURCE_AGENT_ID);
  assert.ok(preview, "The preview offers no agent.");
  assert.equal(preview.nameExists, true, "The preview does not report the agent of the same name.");
  assert.equal(preview.skillCount, 1, "The preview offers another number of skills.");
  assert.equal(preview.fileCount, 2, "The preview offers another number of files.");

  const applied = await run(
    target.apply({ token: staged.token, keys: [SOURCE_AGENT_ID, BROKEN_AGENT_ID], includeSettings: true }),
  );
  assert.equal(applied.agents.length, 1, "The import created another number of agents.");
  const [imported] = applied.agents;
  assert.ok(imported, "The import created no agent.");
  assert.notEqual(imported.id, SOURCE_AGENT_ID, "The imported agent kept the id of the first profile.");
  assert.notEqual(imported.id, TARGET_AGENT_ID, "The imported agent took the id of this profile.");
  assert.equal(imported.name, AGENT_NAME, "The imported agent has another name.");

  // The agent that could not be created is reported, and the other agent is imported.
  assert.equal(applied.skipped.length, 1, "The import reported another number of skipped agents.");
  assert.equal(applied.skipped[0]?.name, BROKEN_AGENT_NAME, "The import skipped another agent.");
  assert.ok(applied.skipped[0]?.reason, "The import reported no reason for the skipped agent.");

  // The agent of this computer is untouched.
  assert.ok(
    record.agents.some((agent) => agent.id === TARGET_AGENT_ID),
    "The import removed the agent of this computer.",
  );
  assert.equal(record.agents.length, 2, "The import holds another number of agents.");

  // The files of the workspace travel with their bytes and their mode.
  const workspace = imported.workspacePath;
  assert.equal(
    sha256(new Uint8Array(await readFile(join(workspace, "notes.md")))),
    sha256(new TextEncoder().encode(NOTES_TEXT)),
    "The imported note holds another content.",
  );
  const script = join(workspace, "run.sh");
  assert.equal(
    sha256(new Uint8Array(await readFile(script))),
    sha256(new TextEncoder().encode("#!/bin/sh\necho synthetic\n")),
    "The imported script holds another content.",
  );
  assert.ok((await stat(script)).mode & 0o111, "The imported script lost the mode of an executable.");

  // The skill is published in this profile and installed in the workspace of the agent.
  const library = new LocalSkillLibrary(join(targetRoot, "skills"), () => record.agents);
  const published = await run(library.list());
  assert.equal(published.length, 1, "The import published another number of skills.");
  const skill = published[0];
  assert.ok(skill, "The import published no skill.");
  assert.equal(skill.slug, SKILL_SLUG, "The import published another skill.");
  const bundle = await run(library.bundle(skill.id, skill.version));
  const files = normalizedFiles(bundle);
  assert.ok(files["SKILL.md"], "The published skill holds no SKILL.md.");
  assert.equal(
    sha256(files["SKILL.md"]),
    sha256(new TextEncoder().encode(SKILL_TEXT)),
    "The published skill holds another content.",
  );
  assert.equal(record.installed.length, 1, "The import installed another number of skills.");
  assert.equal(record.installed[0]?.agentId, imported.id, "The import installed the skill for another agent.");
  const installed = await readFile(join(workspace, ".agents", "skills", SKILL_SLUG, "SKILL.md"));
  assert.equal(
    sha256(new Uint8Array(installed)),
    sha256(new TextEncoder().encode(SKILL_TEXT)),
    "The installed skill holds another content.",
  );

  // The shared folder: a new file is copied, a file that is here is kept, and the tables stay here.
  assert.equal(
    sha256(new Uint8Array(await readFile(join(targetShared, "new.txt")))),
    sha256(new TextEncoder().encode("shared new\n")),
    "The imported shared file holds another content.",
  );
  assert.equal(
    await readFile(join(targetShared, "kept.txt"), "utf8"),
    "shared from this computer\n",
    "The import replaced the shared file that was here.",
  );
  await assert.rejects(readFile(join(targetShared, "Data", "tables.db")), "The import copied the tables.");

  // The settings of this computer: the MCP server is added turned off, the provider has no key and
  // the custom agent is not added at all.
  assert.equal(record.servers.length, 1, "The import saved another number of MCP servers.");
  const saved = record.servers[0];
  assert.ok(saved, "The import saved no MCP server.");
  assert.equal(saved.enabled, false, "The imported MCP server is turned on.");
  assert.equal(saved.env[0]?.value, "", "The imported MCP server holds the value of an environment variable.");
  assert.equal(saved.headers[0]?.value, "", "The imported MCP server holds the value of a header.");
  assert.ok(
    !saved.args.some((argument) => argument.includes(SECRET)),
    "The imported MCP server holds the secret of its argument.",
  );
  assert.equal(record.providers.length, 1, "The import saved another number of custom providers.");
  assert.equal(record.providers[0]?.apiKey, null, "The imported custom provider holds a key.");
  assert.ok(
    !record.providers[0]?.baseUrl.includes(SECRET),
    "The imported custom provider holds the secret of its address.",
  );

  // The import says what it left out.
  const warnings = applied.warnings;
  for (const warning of [
    sourceText("error.import.transferMcpValues", { name: "Local MCP" }),
    sourceText("error.import.transferProviderKey", { name: "Local Provider" }),
    sourceText("error.import.transferValuesCleared", { name: "Local Provider" }),
    sourceText("error.import.transferCustomAgentCommand", {
      name: "Local Agent",
      // The export removes the value of the argument, so the command holds the flag alone.
      command: ["local-agent", "--token", ""].join(" "),
    }),
    sourceText("error.import.transferAgentEnv", { name: "Local Agent", names: "API_KEY" }),
    sourceText("error.import.transferSharedKept", { number: 1 }),
    sourceText("error.import.routineSkipped", {
      name: AGENT_NAME,
      routine: "Morning report",
      reason: "The synthetic routine cannot be created.",
    }),
  ])
    assert.ok(warnings.includes(warning), `The import did not report: ${warning}`);

  // The provider that is signed in here is not asked for.
  assert.deepEqual(applied.signIn, ["claude"], "The import asks for another provider.");

  return {
    archive: path,
    importedAgentId: imported.id,
    importedWorkspace: workspace,
    publishedSkill: skill.id,
    warnings: applied.warnings,
    skipped: applied.skipped,
  };
}

interface Report {
  status: "running" | "passed" | "failed";
  checks: Array<{ name: string; status: "passed" | "failed" }>;
  artifacts?: string;
  result?: RoundtripResult;
  error?: string;
}

const reportPath = fileURLToPath(new URL("../.openbot-build/instance-transfer-roundtrip-e2e.json", import.meta.url));
const root = await mkdtemp(join(tmpdir(), "instance-transfer-roundtrip-"));
const report: Report = { status: "running", checks: [] };
try {
  const result = await checkRoundtrip(root);
  report.checks.push({ name: "export-and-import", status: "passed" });
  report.artifacts = root;
  report.result = result;
  report.status = "passed";
} catch (error) {
  report.status = "failed";
  report.checks.push({ name: "export-and-import", status: "failed" });
  report.artifacts = root;
  report.error = error instanceof Error ? error.message : String(error);
  process.exitCode = 1;
} finally {
  await mkdir(dirname(reportPath), { recursive: true });
  await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  process.stdout.write(`${JSON.stringify(report)}\n`);
}
