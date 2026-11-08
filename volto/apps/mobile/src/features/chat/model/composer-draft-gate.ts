import { type ComposerDraftStore, type ComposerDraftTarget, composerDraftChatKey } from "./composer-drafts";

/** The key of a chat and the thread that it shows. */
function composerDraftTargetKey(target: ComposerDraftTarget): string {
  return `${composerDraftChatKey(target)}:${target.threadId ?? ""}`;
}

interface ComposerDraftEntry {
  /** The text of the composer of this chat. */
  text: string;
  /** The text that the composer showed. A person who clears it removes the draft of the chat. */
  shown: string;
  /** The saved text of the chat was restored. It does not come back a second time. */
  restored: boolean;
}

/**
 * The gate between the composers of the chats and the drafts of the account.
 *
 * It holds the text of every composer, decides when the saved text of a chat may show, and writes
 * the text when the app leaves the foreground. The chat screen draws the text; this gate owns the
 * rules of the text.
 *
 * The rules of the gate are the same in every chat, so they live here and not in the screen. The
 * text that the person typed is newer than the file of the account, also when the file is still
 * being read. The gate keeps that text, so a cleared or sent text does not come back.
 */
export class ComposerDraftGate {
  private readonly entries = new Map<string, ComposerDraftEntry>();
  private readonly listeners = new Set<() => void>();

  constructor(private readonly drafts: ComposerDraftStore) {}

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  /** The text of the composer of the chat. Another chat, or another thread of it, has its own text. */
  text = (target: ComposerDraftTarget): string => this.entry(target).text;

  /**
   * The person changed the composer of the chat. The text is newer than the saved one, also when
   * the file of the account is still being read.
   */
  setText = (target: ComposerDraftTarget, text: string): void => {
    const entry = this.entry(target);
    if (entry.text === text) return;
    entry.text = text;
    if (text) {
      entry.shown = text;
      this.drafts.setDraft(target, text);
    } else if (entry.shown) {
      // The person cleared the text of the chat. The saved text must not come back.
      entry.shown = "";
      this.drafts.clearDraft(target);
    }
    this.publish();
  };

  /**
   * The saved text of the chat may show now. It waits for the file of the account, and it never
   * replaces the text that the person typed or cleared while the file was read.
   */
  restore = (target: ComposerDraftTarget): void => {
    const entry = this.entry(target);
    if (entry.restored) return;
    entry.restored = true;
    const saved = this.drafts.draftFor(target);
    if (!saved) return;
    // The chat knows its thread now, so the text belongs to that thread.
    this.drafts.adoptThread(target);
    // The text of the composer is newer than the saved one.
    if (entry.text) return;
    entry.text = saved;
    entry.shown = saved;
    this.publish();
  };

  /**
   * The app leaves the foreground: the text of the chats is written now. The phone may not run the
   * app again, so the text must be in the file before the app sleeps.
   */
  leaveForeground = (): Promise<void> => this.drafts.flush();

  /** The text is written now. */
  flush = (): Promise<void> => this.drafts.flush();

  private entry(target: ComposerDraftTarget): ComposerDraftEntry {
    const key = composerDraftTargetKey(target);
    let entry = this.entries.get(key);
    if (!entry) {
      entry = { text: "", shown: "", restored: false };
      this.entries.set(key, entry);
    }
    return entry;
  }

  private publish(): void {
    for (const listener of this.listeners) listener();
  }
}
