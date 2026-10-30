// The composer drafts of the iPhone app. The account, the server and the chat keep their own
// text. The composer text and the outbox keep their own files. A message that the host did not
// take stays saved. A sent message clears the draft. The text comes back when the chat opens
// again, and after a restart. The text of a thread stays saved. A text that was cleared while the
// file was read does not come back. A write that failed is written again.
//
// The script drives the real mobile store and the real mobile files. `expo-file-system` needs a
// device, so the plugin below gives the two adapters a filesystem of this worktree with the same
// calls. The adapter files are real: the drafts of one account live in their own folder, and a
// write goes to the next file first. The composer screen itself needs a device.
//
// The gate of the composer is the app logic between the screen and the store. The script drives it
// as the screen does: the person types, clears or sends, the file of the account arrives, and the
// app goes to the background.
//
// Nothing is removed: the stub keeps a file under another name where the native call removes it.
//
// Run: bun run test:mobile-composer-draft
// It writes .openbot-build/mobile-composer-draft-e2e/report.json and exits non-zero on a failure.

import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { copyFile, mkdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { isDynamicRecord, isString } from "@openbot/contracts/runtime-values";
import { createOpenBotLogger } from "@openbot/logging";
import { plugin } from "bun";
import { ComposerDraftGate } from "../apps/mobile/src/features/chat/model/composer-draft-gate";
import {
  type ComposerDraftStorage,
  ComposerDraftStore,
  type ComposerDraftTarget,
  composerDraftChatKey,
  composerDraftFile,
  type PersistedComposerDrafts,
} from "../apps/mobile/src/features/chat/model/composer-drafts";

const logger = createOpenBotLogger("mobile-composer-draft-e2e");
const OUT = resolve(import.meta.dirname, "../.openbot-build/mobile-composer-draft-e2e");
// The documents of the phone. A new folder per run keeps the files of an earlier run.
const DOCUMENTS = join(OUT, `documents-${Date.now()}`);
const DOCUMENT_ROOT = `${DOCUMENTS}/`;

plugin({
  name: "mobile-composer-draft-files",
  setup(build) {
    // The native filesystem of the phone, with the same calls, on a folder of this worktree.
    build.module("expo-file-system/legacy", () => ({
      loader: "object",
      exports: {
        documentDirectory: DOCUMENT_ROOT,
        EncodingType: { Base64: "base64", UTF8: "utf8" },
        getInfoAsync: async (path: string) => {
          const info = await stat(path).catch(() => null);
          return info
            ? { exists: true, isDirectory: info.isDirectory(), size: info.size }
            : { exists: false, isDirectory: false, size: 0 };
        },
        readAsStringAsync: async (path: string, options?: { encoding?: string }) => {
          const content = await readFile(path, "utf8");
          return options?.encoding === "base64" ? Buffer.from(content, "utf8").toString("base64") : content;
        },
        writeAsStringAsync: async (path: string, contents: string, options?: { encoding?: string }) => {
          await mkdir(dirname(path), { recursive: true });
          const text = options?.encoding === "base64" ? Buffer.from(contents, "base64").toString("utf8") : contents;
          await writeFile(path, text, "utf8");
        },
        makeDirectoryAsync: async (path: string) => {
          await mkdir(path, { recursive: true });
        },
        deleteAsync: async (path: string) => {
          // The native call removes the file. This run keeps it under another name, so the path is
          // free for the next write and nothing is removed.
          if (!path.startsWith(DOCUMENT_ROOT)) throw new Error(`The stub only writes in ${DOCUMENT_ROOT}.`);
          const info = await stat(path).catch(() => null);
          if (!info) return;
          await rename(path, `${path}.kept-${Date.now()}`);
        },
        moveAsync: async (options: { from: string; to: string }) => {
          await mkdir(dirname(options.to), { recursive: true });
          await rename(options.from, options.to);
        },
        copyAsync: async (options: { from: string; to: string }) => {
          await mkdir(dirname(options.to), { recursive: true });
          await copyFile(options.from, options.to);
        },
      },
    }));
    build.module("expo-crypto", () => ({
      loader: "object",
      exports: { randomUUID: () => randomUUID() },
    }));
  },
});

// The files of the drafts. The plugin above is ready now.
const { createComposerDraftFiles } = await import("../apps/mobile/src/features/chat/model/composer-draft-files");

// The outbox of the phone. Its types come from the mobile app, which the node project cannot
// read, so the script names the small part of the store that it uses.
const outboxUrl = new URL("../apps/mobile/src/features/chat/model/outbox-store.ts", import.meta.url).href;
const outboxFilesUrl = new URL("../apps/mobile/src/features/chat/model/outbox-files.ts", import.meta.url).href;
const outboxModule: { OutboxStore: OutboxStoreConstructor } = await import(outboxUrl);
const outboxFilesModule: { createOutboxFiles: (scope: string) => OutboxStorageView | null } = await import(
  outboxFilesUrl
);

interface OutboxFileView {
  id: string;
  name: string;
  fileName: string;
}

interface OutboxMessageView {
  clientMessageId: string;
  state: string;
  text: string;
  files: OutboxFileView[];
}

interface OutboxAttachmentView {
  id: string;
  name: string;
  mimeType: string;
  size: number;
  base64: string;
}

interface OutboxDeliveryView {
  files: OutboxAttachmentView[];
}

interface OutboxStorageView {
  load(): Promise<{ messages: OutboxMessageView[] } | null>;
  save(outbox: { version: number; messages: OutboxMessageView[] }): Promise<void>;
}

interface OutboxStoreView {
  add(
    target: { kind: "agent"; serverId: string; agentId: string },
    input: { text: string; replyToMessageId: string | null; files: readonly OutboxAttachmentView[] },
  ): { clientMessageId: string };
  load(): Promise<void>;
  retry(clientMessageId: string): void;
  setOnline(serverIds: ReadonlySet<string>): void;
  snapshot(): { messages: OutboxMessageView[] };
}

interface OutboxStoreConstructor {
  new (options: {
    storage: OutboxStorageView | null;
    deliver: (message: OutboxMessageView, delivery: OutboxDeliveryView) => Promise<{ messageId: string | null }>;
    describeError: (error: unknown) => string;
    randomId: () => string;
  }): OutboxStoreView;
}

interface Step {
  name: string;
  passed: boolean;
  detail: string;
}

interface PersistedDraftView {
  text: string;
  threadId: string | null;
  updatedAt: string;
}

interface PersistedDraftsView {
  version: 1;
  drafts: Record<string, PersistedDraftView>;
}

const steps: Step[] = [];

function check(name: string, passed: boolean, detail: string): void {
  steps.push({ name, passed, detail });
  if (passed) logger.info(`${name}: ${detail}`);
  else logger.error(`${name}: ${detail}`);
}

/** Waits for a state of the store, not for the clock. */
async function waitFor(label: string, ready: () => boolean): Promise<void> {
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    if (ready()) return;
    await new Promise((done) => setTimeout(done, 10));
  }
  throw new Error(`Timed out waiting for ${label}.`);
}

/** The list of the drafts of one account, as it is on the phone. */
function readPersistedDrafts(file: string): PersistedDraftsView {
  return decodePersistedDrafts(JSON.parse(readFileSync(file, "utf8")));
}

function decodePersistedDrafts(value: unknown): PersistedDraftsView {
  if (!isDynamicRecord(value) || value.version !== 1 || !isDynamicRecord(value.drafts))
    return { version: 1, drafts: {} };
  const drafts: Record<string, PersistedDraftView> = {};
  for (const [key, draft] of Object.entries(value.drafts)) {
    if (!isDynamicRecord(draft) || !isString(draft.text)) continue;
    drafts[key] = {
      text: draft.text,
      threadId: isString(draft.threadId) ? draft.threadId : null,
      updatedAt: isString(draft.updatedAt) ? draft.updatedAt : "",
    };
  }
  return { version: 1, drafts };
}

const ACCOUNT_ONE = "https://openbot.example:443:user-one";
const ACCOUNT_TWO = "https://openbot.example:443:user-two";
const ACCOUNT_THREE = "https://openbot.example:443:user-three";
const ACCOUNT_FOUR = "https://openbot.example:443:user-four";

function agentChat(serverId: string, id: string, threadId: string | null = null): ComposerDraftTarget {
  return { kind: "agent", serverId, id, threadId };
}

/** The store of one account, with the file of the account read. */
async function openDrafts(scope: string): Promise<ComposerDraftStore> {
  const store = new ComposerDraftStore({ storage: createComposerDraftFiles(scope) });
  await store.load();
  return store;
}

/** A gate of a chat whose file of the account is read later. */
function openGate(): {
  gate: ComposerDraftGate;
  store: ComposerDraftStore;
  release: (saved: PersistedDraftsView | null) => void;
  loaded: Promise<void>;
} {
  let releaseFile: (saved: PersistedDraftsView | null) => void = () => undefined;
  const file = new Promise<PersistedDraftsView | null>((resolveFile) => {
    releaseFile = resolveFile;
  });
  const store = new ComposerDraftStore({ storage: { load: () => file, save: async () => undefined } });
  return { gate: new ComposerDraftGate(store), store, release: releaseFile, loaded: store.load() };
}

/** The file of an account that holds a text for a chat. */
function savedDrafts(target: ComposerDraftTarget, text: string): PersistedDraftsView {
  return {
    version: 1,
    drafts: {
      [composerDraftChatKey(target)]: { text, threadId: null, updatedAt: new Date(0).toISOString() },
    },
  };
}

async function main(): Promise<void> {
  mkdirSync(OUT, { recursive: true });

  // The accounts keep their own text and their own files.
  const accountOne = await openDrafts(ACCOUNT_ONE);
  const accountTwo = await openDrafts(ACCOUNT_TWO);
  accountOne.setDraft(agentChat("server-1", "agent-1"), "the text of the first account");
  await accountOne.flush();
  check(
    "The accounts keep their own text",
    accountOne.draftFor(agentChat("server-1", "agent-1")) === "the text of the first account" &&
      accountTwo.draftFor(agentChat("server-1", "agent-1")) === "",
    `the second account shows ${JSON.stringify(accountTwo.draftFor(agentChat("server-1", "agent-1")))}`,
  );
  const fileOne = composerDraftFile(DOCUMENT_ROOT, ACCOUNT_ONE);
  const fileTwo = composerDraftFile(DOCUMENT_ROOT, ACCOUNT_TWO);
  check(
    "The accounts keep their own files",
    fileOne !== fileTwo && existsSync(fileOne) && !existsSync(fileTwo),
    `${fileOne} and ${fileTwo}`,
  );

  // The server, the chat and the kind keep their own text.
  const chats: { target: ComposerDraftTarget; text: string }[] = [
    { target: agentChat("server-1", "agent-1"), text: "agent one of server one" },
    { target: agentChat("server-1", "agent-2"), text: "agent two of server one" },
    { target: agentChat("server-2", "agent-1"), text: "agent one of server two" },
    { target: { kind: "channel", serverId: "server-1", id: "agent-1", threadId: null }, text: "channel of server one" },
  ];
  for (const chat of chats) accountOne.setDraft(chat.target, chat.text);
  await accountOne.flush();
  check(
    "The server and the chat keep their own text",
    chats.every((chat) => accountOne.draftFor(chat.target) === chat.text),
    chats.map((chat) => `${composerDraftChatKey(chat.target)}: ${accountOne.draftFor(chat.target)}`).join("; "),
  );

  // The composer text and the outbox keep their own files.
  let messageCount = 0;
  const nextMessageId = (): string => {
    messageCount += 1;
    return `message-${messageCount}`;
  };
  const failingOutbox = new outboxModule.OutboxStore({
    storage: outboxFilesModule.createOutboxFiles(ACCOUNT_ONE),
    deliver: async () => {
      throw new Error("The host did not take the message.");
    },
    describeError: () => "The host did not take the message.",
    randomId: nextMessageId,
  });
  await failingOutbox.load();
  failingOutbox.setOnline(new Set(["server-1"]));
  failingOutbox.add(
    { kind: "agent", serverId: "server-1", agentId: "agent-1" },
    { text: "the text of a message the host did not take", replyToMessageId: null, files: [] },
  );
  await waitFor("the failed message", () => failingOutbox.snapshot().messages[0]?.state === "failed");
  const outboxFile = join(DOCUMENTS, "outbox", encodeURIComponent(ACCOUNT_ONE), "messages.json");
  await waitFor(
    "the file of the outbox",
    () =>
      existsSync(outboxFile) &&
      readFileSync(outboxFile, "utf8").includes("the text of a message the host did not take"),
  );
  const draftsText = readFileSync(fileOne, "utf8");
  const outboxText = readFileSync(outboxFile, "utf8");
  check(
    "The composer text and the outbox keep their own files",
    fileOne !== outboxFile &&
      !draftsText.includes('"messages"') &&
      !outboxText.includes('"drafts"') &&
      !draftsText.includes("the text of a message the host did not take"),
    `${fileOne} and ${outboxFile}`,
  );

  // A message that the host did not take stays saved over a restart.
  const restartedOutbox = new outboxModule.OutboxStore({
    storage: outboxFilesModule.createOutboxFiles(ACCOUNT_ONE),
    deliver: async () => {
      throw new Error("The host did not take the message.");
    },
    describeError: () => "The host did not take the message.",
    randomId: nextMessageId,
  });
  await restartedOutbox.load();
  const failedMessage = restartedOutbox.snapshot().messages[0];
  check(
    "A message that the host did not take stays saved",
    failedMessage !== undefined &&
      failedMessage.state === "failed" &&
      failedMessage.text === "the text of a message the host did not take",
    failedMessage ? `${failedMessage.state}: ${failedMessage.text}` : "no message",
  );
  check(
    "The composer text does not take the text of a failed message",
    accountOne.draftFor(agentChat("server-1", "agent-1")) === "agent one of server one",
    accountOne.draftFor(agentChat("server-1", "agent-1")),
  );

  // A sent message clears the draft.
  const sentChat = agentChat("server-3", "agent-1");
  accountOne.setDraft(sentChat, "the text of a sent message");
  await accountOne.flush();
  accountOne.clearDraft(sentChat);
  await accountOne.flush();
  const afterSend = await openDrafts(ACCOUNT_ONE);
  check(
    "A sent message clears the draft",
    afterSend.draftFor(sentChat) === "",
    JSON.stringify(afterSend.draftFor(sentChat)),
  );

  // Each chat keeps its own text, and the text comes back when the chat opens again.
  const firstChat = agentChat("server-4", "agent-1");
  const secondChat = agentChat("server-4", "agent-2");
  accountOne.setDraft(firstChat, "the text of the first chat");
  accountOne.setDraft(secondChat, "the text of the second chat");
  await accountOne.flush();
  const afterNavigation = await openDrafts(ACCOUNT_ONE);
  check(
    "Each chat keeps its own text",
    afterNavigation.draftFor(firstChat) === "the text of the first chat" &&
      afterNavigation.draftFor(secondChat) === "the text of the second chat",
    `${afterNavigation.draftFor(firstChat)}; ${afterNavigation.draftFor(secondChat)}`,
  );

  // The text of a chat comes back after a restart.
  const restartChat = agentChat("server-4", "agent-3");
  accountOne.setDraft(restartChat, "the text that must survive a restart");
  await accountOne.flush();
  const afterRestart = await openDrafts(ACCOUNT_ONE);
  check(
    "The text of a chat comes back after a restart",
    afterRestart.draftFor(restartChat) === "the text that must survive a restart",
    afterRestart.draftFor(restartChat),
  );

  // The composer keeps the text alone. The file of a message stays with the outbox.
  const persisted = readPersistedDrafts(fileOne);
  const entries = Object.values(persisted.drafts);
  check(
    "The composer keeps the text alone",
    persisted.version === 1 &&
      entries.length > 0 &&
      entries.every((entry) => Object.keys(entry).sort().join(",") === "text,threadId,updatedAt") &&
      !readFileSync(fileOne, "utf8").includes("base64"),
    `${entries.length} drafts with the fields ${entries[0] ? Object.keys(entries[0]).sort().join(",") : ""}`,
  );

  // A file of a message that the host did not take stays with the message over a restart.
  let sentFiles: OutboxAttachmentView[] = [];
  const fileOutbox = new outboxModule.OutboxStore({
    storage: outboxFilesModule.createOutboxFiles(ACCOUNT_TWO),
    deliver: async () => {
      throw new Error("The host did not take the message.");
    },
    describeError: () => "The host did not take the message.",
    randomId: nextMessageId,
  });
  await fileOutbox.load();
  fileOutbox.setOnline(new Set(["server-1"]));
  const attachment = {
    id: "file-1",
    name: "note.txt",
    mimeType: "text/plain",
    size: 5,
    base64: Buffer.from("hello").toString("base64"),
  };
  fileOutbox.add(
    { kind: "agent", serverId: "server-1", agentId: "agent-1" },
    { text: "a message with a file", replyToMessageId: null, files: [attachment] },
  );
  await waitFor("the file of the message", () => (fileOutbox.snapshot().messages[0]?.files[0]?.fileName ?? "") !== "");
  const fileOutboxFile = join(DOCUMENTS, "outbox", encodeURIComponent(ACCOUNT_TWO), "messages.json");
  await waitFor(
    "the file of the outbox",
    () => existsSync(fileOutboxFile) && readFileSync(fileOutboxFile, "utf8").includes("a message with a file"),
  );
  const restartedFiles = new outboxModule.OutboxStore({
    storage: outboxFilesModule.createOutboxFiles(ACCOUNT_TWO),
    deliver: async (_message, delivery) => {
      sentFiles = delivery.files;
      return { messageId: "host-message-1" };
    },
    describeError: () => "The host did not take the message.",
    randomId: nextMessageId,
  });
  await restartedFiles.load();
  const messageWithFile = restartedFiles.snapshot().messages[0];
  check(
    "A file of a message stays with the message over a restart",
    messageWithFile !== undefined && messageWithFile.files.length === 1 && Boolean(messageWithFile.files[0]?.fileName),
    messageWithFile ? messageWithFile.files.map((file) => file.fileName).join(",") : "no message",
  );
  if (messageWithFile) {
    restartedFiles.setOnline(new Set(["server-1"]));
    restartedFiles.retry(messageWithFile.clientMessageId);
    await waitFor("the file of the message to be sent again", () => sentFiles.length > 0);
  }
  check(
    "The bytes of a file of a message are still there over a restart",
    sentFiles[0]?.base64 === attachment.base64,
    sentFiles[0] ? sentFiles[0].name : "no file",
  );

  // The text that the person typed while the file of the account was read is the newer one.
  let releaseLoad: (saved: PersistedDraftsView | null) => void = () => undefined;
  const gate = new Promise<PersistedDraftsView | null>((resolveGate) => {
    releaseLoad = resolveGate;
  });
  const slowStorage: ComposerDraftStorage = {
    load: () => gate,
    save: async () => undefined,
  };
  const typingStore = new ComposerDraftStore({ storage: slowStorage });
  const loading = typingStore.load();
  typingStore.setDraft(agentChat("server-5", "agent-1"), "the text that was typed while the file was read");
  releaseLoad({
    version: 1,
    drafts: {
      [composerDraftChatKey(agentChat("server-5", "agent-1"))]: {
        text: "the text of an earlier run",
        threadId: null,
        updatedAt: new Date(0).toISOString(),
      },
    },
  });
  await loading;
  check(
    "The text that was typed while the file was read stays",
    typingStore.draftFor(agentChat("server-5", "agent-1")) === "the text that was typed while the file was read",
    typingStore.draftFor(agentChat("server-5", "agent-1")),
  );

  // The text that the person cleared while the file of the account was read does not come back.
  let releaseClear: (saved: PersistedDraftsView | null) => void = () => undefined;
  const clearGate = new Promise<PersistedDraftsView | null>((resolveGate) => {
    releaseClear = resolveGate;
  });
  const clearingStore = new ComposerDraftStore({
    storage: { load: () => clearGate, save: async () => undefined },
  });
  const clearing = clearingStore.load();
  clearingStore.setDraft(agentChat("server-7", "agent-1"), "the text of an earlier run");
  clearingStore.clearDraft(agentChat("server-7", "agent-1"));
  releaseClear({
    version: 1,
    drafts: {
      [composerDraftChatKey(agentChat("server-7", "agent-1"))]: {
        text: "the text of an earlier run",
        threadId: null,
        updatedAt: new Date(0).toISOString(),
      },
    },
  });
  await clearing;
  check(
    "The text that was cleared while the file was read does not come back",
    clearingStore.draftFor(agentChat("server-7", "agent-1")) === "",
    clearingStore.draftFor(agentChat("server-7", "agent-1")),
  );

  // The text of a thread stays saved when the composer of another thread of the chat is empty.
  const threadStore = new ComposerDraftStore({ storage: null });
  threadStore.setDraft(agentChat("server-8", "agent-1", "thread-one"), "the text of the first thread");
  threadStore.setDraft(agentChat("server-8", "agent-1", "thread-two"), "");
  check(
    "The text of a thread stays saved when another thread of the chat is empty",
    threadStore.draftFor(agentChat("server-8", "agent-1", "thread-one")) === "the text of the first thread",
    threadStore.draftFor(agentChat("server-8", "agent-1", "thread-one")),
  );

  // A write that failed stays pending, so the next write keeps the text.
  let failWrite = true;
  const written: PersistedComposerDrafts[] = [];
  const flakyStore = new ComposerDraftStore({
    storage: {
      load: async () => null,
      save: async (drafts) => {
        if (failWrite) throw new Error("The phone has no room for the file.");
        written.push(drafts);
      },
    },
  });
  await flakyStore.load();
  flakyStore.setDraft(agentChat("server-9", "agent-1"), "the text of a write that failed");
  await flakyStore.flush();
  failWrite = false;
  await flakyStore.flush();
  const flakyKey = composerDraftChatKey(agentChat("server-9", "agent-1"));
  check(
    "A write that failed is written again",
    written.length === 1 && written[0]?.drafts[flakyKey]?.text === "the text of a write that failed",
    `${written.length} writes with the text ${JSON.stringify(written[0]?.drafts[flakyKey]?.text ?? "")}`,
  );

  // A next list of another version does not hide the list of the account.
  const thirdAccount = await openDrafts(ACCOUNT_THREE);
  thirdAccount.setDraft(agentChat("server-6", "agent-1"), "the text of the third account");
  await thirdAccount.flush();
  const thirdFile = composerDraftFile(DOCUMENT_ROOT, ACCOUNT_THREE);
  const thirdNext = thirdFile.replace("drafts.json", "drafts.next.json");
  writeFileSync(thirdNext, JSON.stringify({ version: 2, drafts: {} }), "utf8");
  const afterOtherVersion = await openDrafts(ACCOUNT_THREE);
  check(
    "A next list of another version does not hide the list of the account",
    afterOtherVersion.draftFor(agentChat("server-6", "agent-1")) === "the text of the third account",
    afterOtherVersion.draftFor(agentChat("server-6", "agent-1")),
  );
  const kept = readdirSync(dirname(thirdFile)).filter((entry) => entry.startsWith("drafts.unreadable-"));
  check("A list of another version is kept aside", kept.length > 0, kept.join(", "));

  // The list of the account is complete after a write.
  const nextFile = fileOne.replace("drafts.json", "drafts.next.json");
  check(
    "The list of the account is complete after a write",
    existsSync(fileOne) && !existsSync(nextFile),
    `${fileOne} and ${nextFile}`,
  );

  // The gate of the composer. The screen of the chat calls it, and it owns the rules of the text.
  const gateChat = agentChat("server-10", "agent-1");

  // The person types while the file of the account is read: the typed text is the newer one.
  {
    const { gate, store, release, loaded } = openGate();
    gate.setText(gateChat, "the text that was typed while the file was read");
    release(savedDrafts(gateChat, "the text of an earlier run"));
    await loaded;
    gate.restore(gateChat);
    check(
      "The composer keeps the text that the person typed while the file of the account was read",
      gate.text(gateChat) === "the text that was typed while the file was read" &&
        store.draftFor(gateChat) === "the text that was typed while the file was read",
      `${gate.text(gateChat)}; ${store.draftFor(gateChat)}`,
    );
  }

  // The person clears the composer while the file of the account is read: the text does not come
  // back.
  {
    const { gate, store, release, loaded } = openGate();
    gate.setText(gateChat, "the text of an earlier run");
    gate.setText(gateChat, "");
    release(savedDrafts(gateChat, "the text of an earlier run"));
    await loaded;
    gate.restore(gateChat);
    check(
      "The composer does not show the text that the person cleared while the file of the account was read",
      gate.text(gateChat) === "" && store.draftFor(gateChat) === "",
      `${gate.text(gateChat)}; ${store.draftFor(gateChat)}`,
    );
  }

  // The person sends the message while the file of the account is read: the message goes to the
  // outbox, and its text does not come back as a draft.
  {
    const { gate, store, release, loaded } = openGate();
    const outbox = new outboxModule.OutboxStore({
      storage: null,
      deliver: async () => ({ messageId: "host-message-1" }),
      describeError: () => "The host did not take the message.",
      randomId: nextMessageId,
    });
    await outbox.load();
    outbox.setOnline(new Set(["server-10"]));
    const body = "the message that was sent while the file was read";
    gate.setText(gateChat, body);
    outbox.add(
      { kind: "agent", serverId: "server-10", agentId: "agent-1" },
      { text: body, replyToMessageId: null, files: [] },
    );
    // The send clears the composer.
    gate.setText(gateChat, "");
    release(savedDrafts(gateChat, "the text of an earlier run"));
    await loaded;
    gate.restore(gateChat);
    const sent = outbox.snapshot().messages[0];
    check(
      "The composer does not show the text of a message that was sent while the file of the account was read",
      gate.text(gateChat) === "" && store.draftFor(gateChat) === "" && sent !== undefined && sent.text === body,
      `composer ${JSON.stringify(gate.text(gateChat))}; draft ${JSON.stringify(store.draftFor(gateChat))}; outbox ${sent ? sent.text : "no message"}`,
    );
  }

  // The app goes to the background: the text of the chats is written now.
  {
    const account = await openDrafts(ACCOUNT_FOUR);
    const gate = new ComposerDraftGate(account);
    const target = agentChat("server-11", "agent-1");
    gate.setText(target, "the text that the phone must keep");
    // The app leaves the foreground.
    await gate.leaveForeground();
    const afterBackground = await openDrafts(ACCOUNT_FOUR);
    check(
      "The text is written when the app goes to the background",
      afterBackground.draftFor(target) === "the text that the phone must keep",
      afterBackground.draftFor(target),
    );
  }

  const report = {
    passed: steps.every((step) => step.passed),
    documents: DOCUMENTS,
    steps,
    unverified: [
      "The composer screen of the phone. The script drives the gate of the composer, the store and the files of the phone, not the screen.",
      "The native filesystem of the phone. The adapter runs on a folder of this worktree with the same calls.",
      "The AppState of the phone. The script calls the gate of the composer as the provider does when the app leaves the foreground.",
    ],
  };
  writeFileSync(join(OUT, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
  logger.info(`Checked ${steps.length} steps. Wrote ${join(OUT, "report.json")}.`);
  if (!report.passed) process.exitCode = 1;
}

await main();
