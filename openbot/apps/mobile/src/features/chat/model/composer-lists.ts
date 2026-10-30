import { type MarkdownListEdit, markdownListLineBreak } from "@openbot/contracts/markdown-lists";
import { editMentionDraft } from "./chat-mentions";

/**
 * The list edit for a Return key press, when `next` is `previous` with one line break at the caret.
 * Any other change, such as a paste or dictation, keeps the text as typed.
 */
export function markdownListReturn(previous: string, next: string, caret: number): MarkdownListEdit | null {
  if (caret < 0 || next !== `${previous.slice(0, caret)}\n${previous.slice(caret)}`) return null;
  return markdownListLineBreak(previous, caret);
}

/** Apply each list change separately, so agent references between changed numbers keep their identity. */
export function applyMarkdownListEdit(source: string, previous: string, caret: number, edit: MarkdownListEdit): string {
  if (edit.caret <= caret) return editMentionDraft(source, edit.text);

  let result = source;
  let visible = previous;
  for (const replacement of edit.replacements?.toReversed() ?? []) {
    const next = visible.slice(0, replacement.start) + replacement.text + visible.slice(replacement.end);
    result = editMentionDraft(result, next);
    visible = next;
  }
  const insert = edit.text.slice(caret, edit.caret);
  const next = visible.slice(0, caret) + insert + visible.slice(caret);
  return editMentionDraft(result, next);
}
