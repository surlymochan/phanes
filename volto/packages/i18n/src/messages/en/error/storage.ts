import { defineMessages } from "../../../message";

export const messages = defineMessages("error.storage", {
  // Storage errors.
  "error.storage.unsupported": "Storage is not supported by this server.",
  "error.storage.agentMissing": "The agent does not exist.",
  "error.storage.unreadableFile": "volto cannot read {path}. The file is damaged.",
});
