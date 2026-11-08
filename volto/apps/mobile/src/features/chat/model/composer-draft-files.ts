import * as FileSystem from "expo-file-system/legacy";
import {
  type ComposerDraftStorage,
  composerDraftDirectory,
  composerDraftFile,
  type DecodedComposerDrafts,
  decodePersistedComposerDrafts,
} from "./composer-drafts";

/**
 * The drafts of one account, in the app's documents. `drafts.json` holds the list. A write goes to
 * `drafts.next.json` first, so a restart during a write finds one complete list.
 */

export function createComposerDraftFiles(scope: string): ComposerDraftStorage | null {
  if (!FileSystem.documentDirectory) return null;
  const root = composerDraftDirectory(FileSystem.documentDirectory, scope);
  const list = composerDraftFile(FileSystem.documentDirectory, scope);
  const next = `${root}drafts.next.json`;

  /** The list in `path`, or undefined when the file is missing, or an incomplete next list. */
  async function read(path: string): Promise<DecodedComposerDrafts | undefined> {
    if (!(await FileSystem.getInfoAsync(path)).exists) return undefined;
    const text = await FileSystem.readAsStringAsync(path);
    let value: unknown;
    try {
      value = JSON.parse(text);
    } catch {
      // A damaged next list is incomplete: the list that it replaces is still there.
      if (path !== list) return undefined;
      await FileSystem.copyAsync({ from: path, to: `${root}drafts.unreadable-${Date.now()}.json` });
      return { drafts: {}, dropped: true };
    }
    const decoded = decodePersistedComposerDrafts(value);
    // A file of another version is kept aside before this version replaces it.
    if (decoded.dropped) await FileSystem.copyAsync({ from: path, to: `${root}drafts.unreadable-${Date.now()}.json` });
    return decoded;
  }

  return {
    load: async () => {
      // A complete next list is newer than the list: the restart came before the move.
      const pending = await read(next);
      if (pending && !pending.dropped) return { version: 1, drafts: pending.drafts };
      const saved = await read(list);
      return saved ? { version: 1, drafts: saved.drafts } : null;
    },
    save: async (drafts) => {
      await FileSystem.makeDirectoryAsync(root, { intermediates: true });
      await FileSystem.writeAsStringAsync(next, JSON.stringify(drafts));
      await FileSystem.deleteAsync(list, { idempotent: true });
      await FileSystem.moveAsync({ from: next, to: list });
    },
  };
}
