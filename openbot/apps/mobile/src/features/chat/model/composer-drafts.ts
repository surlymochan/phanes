import { isDynamicRecord, isString } from "@openbot/contracts/runtime-values";

/**
 * The unsent text of a chat's composer. The phone keeps it while the app runs and over a restart,
 * so a chat that closes and opens again has the text the person typed.
 *
 * Only the text is kept. A file of the composer points at bytes that this phone may have cleared,
 * and a reply names a message that the chat may not have loaded. The desktop keeps the text alone
 * as well.
 *
 * The drafts have their own file. The outbox holds the messages that wait for the host, and a
 * change of the outbox's format must not touch the text of a chat.
 */

/** The kind of chat that owns a draft. */
type ComposerDraftKind = "agent" | "channel";

/** The chat that owns a draft. */
export interface ComposerDraftChat {
  kind: ComposerDraftKind;
  serverId: string;
  /** The agent id or the channel id. */
  id: string;
}

/** The chat that owns a draft, with the thread the chat shows now. */
export interface ComposerDraftTarget extends ComposerDraftChat {
  /** The thread of the chat. Null when the chat has no thread or has not loaded it yet. */
  threadId: string | null;
}

/** One saved draft. */
export interface ComposerDraft {
  text: string;
  /** The thread of the chat when the text was typed. Null when the chat had no thread yet. */
  threadId: string | null;
  updatedAt: string;
}

/** The drafts of one account, as they are saved on the phone. */
export interface PersistedComposerDrafts {
  version: 1;
  drafts: Record<string, ComposerDraft>;
}

/** The key of a chat. Two chats of two servers never share it. */
export function composerDraftChatKey(chat: ComposerDraftChat): string {
  return `${chat.kind}:${chat.serverId}:${chat.id}`;
}

/** The directory of one account's drafts. The scope keeps the accounts apart. */
export function composerDraftDirectory(documentDirectory: string, scope: string): string {
  return `${documentDirectory}drafts/${encodeURIComponent(scope)}/`;
}

/** The file of one account's drafts. */
export function composerDraftFile(documentDirectory: string, scope: string): string {
  return `${composerDraftDirectory(documentDirectory, scope)}drafts.json`;
}

/** The phone's copy of the drafts. Every method can fail with an I/O error. */
export interface ComposerDraftStorage {
  /** The saved drafts, or null when there are none. Throws when the file exists but cannot be read. */
  load: () => Promise<PersistedComposerDrafts | null>;
  save: (drafts: PersistedComposerDrafts) => Promise<void>;
}

export interface ComposerDraftSnapshot {
  /** The saved drafts are in memory. */
  loaded: boolean;
  /** The phone could not save or read the drafts. They stay in memory for this run. */
  storageFailed: boolean;
}

/** How long the phone waits before it writes a draft. */
const WRITE_DELAY_MS = 300;

interface ComposerDraftStoreOptions {
  storage: ComposerDraftStorage | null;
  now?: () => number;
}

/**
 * The unsent text of every chat of one account. The phone keeps it over a restart, so a chat that
 * closes and opens again has the text the person typed.
 */
export class ComposerDraftStore {
  private drafts = new Map<string, ComposerDraft>();
  private readonly listeners = new Set<() => void>();
  private snapshotValue: ComposerDraftSnapshot = { loaded: false, storageFailed: false };
  private loaded = false;
  private loading: Promise<void> | null = null;
  /** No save after a load that failed: the file may still hold drafts this run could not read. */
  private canSave = true;
  private storageFailed = false;
  private dirty = false;
  /** The chats that the person typed in or cleared while the file of the account was read. */
  private readonly touched = new Set<string>();
  private timer: ReturnType<typeof setTimeout> | null = null;
  private writing: Promise<void> = Promise.resolve();
  private readonly now: () => number;

  constructor(private readonly options: ComposerDraftStoreOptions) {
    this.now = options.now ?? Date.now;
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  snapshot = (): ComposerDraftSnapshot => this.snapshotValue;

  load(): Promise<void> {
    this.loading ??= this.readSaved();
    return this.loading;
  }

  private async readSaved(): Promise<void> {
    const { storage } = this.options;
    if (!storage) {
      this.loaded = true;
      this.publish();
      return;
    }
    try {
      const saved = await storage.load();
      // A text that the person typed or cleared while the file was read is newer than the saved one.
      if (saved)
        for (const [key, draft] of Object.entries(saved.drafts))
          if (!this.drafts.has(key) && !this.touched.has(key)) this.drafts.set(key, draft);
    } catch {
      this.canSave = false;
      this.storageFailed = true;
    }
    this.loaded = true;
    this.publish();
    // A draft that the person typed before the file was read waits for this write.
    if (this.dirty) void this.flush();
  }

  /**
   * The saved text of a chat, or an empty string.
   *
   * A draft of another thread of the same chat stays saved: the chat shows one thread at a time,
   * and the text waits for its thread. A chat that does not know its thread yet takes the text of
   * the chat, because a draft that stays hidden is a draft that is lost.
   */
  draftFor(target: ComposerDraftTarget): string {
    const draft = this.drafts.get(composerDraftChatKey(target));
    if (!draft) return "";
    if (!draft.threadId || !target.threadId) return draft.text;
    return draft.threadId === target.threadId ? draft.text : "";
  }

  /**
   * Saves the text of a chat. An empty text removes the draft of that thread, so a sent message
   * does not come back. The text of another thread of the chat stays saved. The write waits a
   * moment, so a long text is not written for every keystroke.
   */
  setDraft(target: ComposerDraftTarget, text: string): void {
    const key = composerDraftChatKey(target);
    // The text of this chat is newer than the file of the account, also when it is cleared.
    this.touched.add(key);
    if (!text.trim()) {
      const current = this.drafts.get(key);
      if (!current) return;
      // The text of another thread of this chat stays saved: the chat shows one thread at a time.
      if (current.threadId && target.threadId && current.threadId !== target.threadId) return;
      this.drafts.delete(key);
      this.dirty = true;
      // A sent message must not come back after a restart, so this write does not wait.
      void this.flush();
      return;
    }
    const current = this.drafts.get(key);
    if (current && current.text === text && current.threadId === target.threadId) return;
    this.drafts.set(key, { text, threadId: target.threadId, updatedAt: new Date(this.now()).toISOString() });
    this.dirty = true;
    this.schedule();
  }

  /** Removes the draft of a chat. The text of another thread of the chat stays saved. */
  clearDraft(target: ComposerDraftTarget): void {
    this.setDraft(target, "");
  }

  /**
   * The text of a chat now belongs to the thread the chat shows. A draft that was typed before the
   * chat knew its thread follows it, so the text does not stay in a thread that does not exist.
   */
  adoptThread(target: ComposerDraftTarget): void {
    if (!target.threadId) return;
    const key = composerDraftChatKey(target);
    const draft = this.drafts.get(key);
    if (!draft || draft.threadId) return;
    this.drafts.set(key, { ...draft, threadId: target.threadId });
    this.dirty = true;
    this.schedule();
  }

  /** Writes the pending change now. The chat calls it when it closes. */
  async flush(): Promise<void> {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    const { storage } = this.options;
    // A write that is still running ends before this call returns, so a caller that waits for the
    // file sees the change.
    if (this.dirty && this.loaded && this.canSave && storage) {
      this.dirty = false;
      this.writing = this.writing.then(async () => {
        try {
          await storage.save({ version: 1, drafts: Object.fromEntries(this.drafts) });
        } catch {
          // The write failed. The change stays pending, so the next write keeps the text.
          this.dirty = true;
          this.storageFailed = true;
          this.publish();
        }
      });
    }
    return this.writing;
  }

  private schedule(): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.flush();
    }, WRITE_DELAY_MS);
  }

  private publish(): void {
    this.snapshotValue = { loaded: this.loaded, storageFailed: this.storageFailed };
    for (const listener of this.listeners) listener();
  }
}

/** The drafts of a saved file that this phone can read, and whether it dropped any. */
export interface DecodedComposerDrafts {
  drafts: Record<string, ComposerDraft>;
  dropped: boolean;
}

export function decodePersistedComposerDrafts(value: unknown): DecodedComposerDrafts {
  if (!isDynamicRecord(value) || value.version !== 1 || !isDynamicRecord(value.drafts))
    return { drafts: {}, dropped: true };
  const drafts: Record<string, ComposerDraft> = {};
  let dropped = false;
  for (const [key, draft] of Object.entries(value.drafts)) {
    const decoded = decodeComposerDraft(draft);
    if (!decoded) {
      dropped = true;
      continue;
    }
    drafts[key] = decoded;
  }
  return { drafts, dropped };
}

function decodeComposerDraft(value: unknown): ComposerDraft | null {
  if (!isDynamicRecord(value) || !isString(value.text) || !value.text.trim()) return null;
  if (!(value.threadId === null || isString(value.threadId))) return null;
  if (!isString(value.updatedAt) || !Number.isFinite(Date.parse(value.updatedAt))) return null;
  return { text: value.text, threadId: value.threadId, updatedAt: value.updatedAt };
}
