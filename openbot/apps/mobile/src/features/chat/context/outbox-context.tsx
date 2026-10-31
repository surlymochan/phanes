import { classifyFailure } from "@openbot/telemetry";
import * as Crypto from "expo-crypto";
import {
  createContext,
  type PropsWithChildren,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";
import { reportMobileNotification } from "@/features/analytics/failure-reports";
import { useMobileSession } from "@/features/auth/context/mobile-session-context";
import { ChannelHistoryRefreshError } from "@/features/channels/model/channel-store";
import { useMobileWorkspace } from "@/features/workspace/context/mobile-workspace-context";
import type { MobileWorkspaceContextValue } from "@/features/workspace/model/workspace-types";
import { currentText } from "@/shared/lib/text";
import { useAppForeground } from "@/shared/lib/use-app-foreground";
import { createComposerDraftFiles } from "../model/composer-draft-files";
import { ComposerDraftGate } from "../model/composer-draft-gate";
import { type ComposerDraftSnapshot, ComposerDraftStore } from "../model/composer-drafts";
import type { OutboxMessage } from "../model/outbox";
import { createOutboxFiles } from "../model/outbox-files";
import {
  type OutboxDelivery,
  type OutboxDeliveryResult,
  type OutboxSnapshot,
  OutboxStore,
} from "../model/outbox-store";
import { uploadChatAttachments } from "../model/upload-chat-attachments";

const OutboxContext = createContext<OutboxStore | null>(null);
const ComposerDraftsContext = createContext<{ drafts: ComposerDraftStore; gate: ComposerDraftGate } | null>(null);
const NO_SERVERS: ReadonlySet<string> = new Set();

interface AccountChatState {
  store: OutboxStore;
  /** The unsent text of every chat of this account. */
  drafts: ComposerDraftStore;
  /** The gate between the composers of the chats and the drafts of this account. */
  gate: ComposerDraftGate;
  /** The workspace that sends now. A send in flight keeps the one that started it. */
  workspace: { current: MobileWorkspaceContextValue | null };
}

// One outbox and one set of drafts per account for the whole run. A sign-out and a sign-in to the
// same account use them again: a second store would read the same file while the first one still
// writes it.
const accountChatStates = new Map<string, AccountChatState>();

function createAccountChatState(scope: string | null): AccountChatState {
  const workspace: AccountChatState["workspace"] = { current: null };
  const drafts = new ComposerDraftStore({ storage: scope ? createComposerDraftFiles(scope) : null });
  const gate = new ComposerDraftGate(drafts);
  const store = new OutboxStore({
    storage: scope ? createOutboxFiles(scope) : null,
    deliver: async (message, delivery) => {
      if (!workspace.current) throw new Error(currentText().t("mobile.workspace.error.transportNotReady"));
      return deliverOutboxMessage(workspace.current, message, delivery);
    },
    describeError: (error) => {
      const { t, errorMessage } = currentText();
      return errorMessage(error, t("mobile.chat.outbox.failed"));
    },
    reportFailure: (error) => reportMobileNotification({ code: classifyFailure(error) }, "turn", "banner"),
    randomId: Crypto.randomUUID,
  });
  return { store, drafts, gate, workspace };
}

function accountChatState(scope: string | null): AccountChatState {
  // Signed out: nothing to keep, and nothing to send.
  if (!scope) return createAccountChatState(null);
  let state = accountChatStates.get(scope);
  if (!state) {
    state = createAccountChatState(scope);
    accountChatStates.set(scope, state);
  }
  return state;
}

/**
 * The messages of this account that wait for their server. It sends them while the app runs, in any
 * screen, and the phone keeps them over a restart. The provider owns the unsent text of the chats
 * as well, because it belongs to the same account and the same run.
 */
export function OutboxProvider({ children }: PropsWithChildren) {
  const workspace = useMobileWorkspace();
  const { session } = useMobileSession();
  const foreground = useAppForeground();
  const [{ store, drafts, gate, workspace: current }] = useState(() =>
    accountChatState(session ? `${session.apiUrl}:${session.user.id}` : null),
  );
  current.current = workspace;
  const onlineKey = workspace.servers
    .filter((server) => server.state === "online")
    .map((server) => server.id)
    .sort()
    .join("\n");
  const online = useMemo(() => new Set(onlineKey ? onlineKey.split("\n") : []), [onlineKey]);
  useEffect(() => {
    void store.load();
  }, [store]);
  useEffect(() => {
    void drafts.load();
  }, [drafts]);
  useEffect(() => {
    store.setOnline(online);
  }, [store, online]);
  // After sign-out nothing starts. A send in flight still saves its result for this account.
  useEffect(() => () => store.setOnline(NO_SERVERS), [store]);
  // The app leaves the foreground: the text of the chats is written now. The phone may not run the
  // app again, so the text must be in the file before the app sleeps.
  useEffect(() => {
    if (foreground) return;
    void gate.leaveForeground();
  }, [foreground, gate]);
  const draftsContext = useMemo(() => ({ drafts, gate }), [drafts, gate]);
  return (
    <OutboxContext value={store}>
      <ComposerDraftsContext value={draftsContext}>{children}</ComposerDraftsContext>
    </OutboxContext>
  );
}

export function useOutbox(): { store: OutboxStore; snapshot: OutboxSnapshot } {
  const store = useContext(OutboxContext);
  if (!store) throw new Error("OutboxProvider is missing.");
  const snapshot = useSyncExternalStore(store.subscribe, store.snapshot);
  return { store, snapshot };
}

/** The unsent text of every chat of this account. It is kept while the app runs and over a restart. */
export function useComposerDrafts(): {
  store: ComposerDraftStore;
  gate: ComposerDraftGate;
  snapshot: ComposerDraftSnapshot;
} {
  const context = useContext(ComposerDraftsContext);
  if (!context) throw new Error("OutboxProvider is missing.");
  const snapshot = useSyncExternalStore(context.drafts.subscribe, context.drafts.snapshot);
  return { store: context.drafts, gate: context.gate, snapshot };
}

async function deliverOutboxMessage(
  workspace: MobileWorkspaceContextValue,
  message: OutboxMessage,
  delivery: OutboxDelivery,
): Promise<OutboxDeliveryResult> {
  const { target } = message;
  const control = { cancelled: delivery.cancelled, progress: delivery.progress, fileProgress: delivery.fileProgress };
  if (target.kind === "agent") {
    const repeatSafe = workspace.canRetryMessage(target.serverId);
    const messageId = await uploadChatAttachments(delivery.files, {
      ...control,
      upload: (file, onProgress) => workspace.uploadAttachment(target.agentId, file, target.serverId, onProgress),
      discard: (id) => workspace.discardAttachment(target.agentId, id, target.serverId),
      send: async (ids) => {
        await delivery.beforeStore(repeatSafe);
        return workspace.sendMessage(
          target.agentId,
          message.text,
          ids,
          message.replyToMessageId,
          target.serverId,
          repeatSafe ? message.clientMessageId : undefined,
        );
      },
    });
    return { messageId };
  }
  const { channelStore } = workspace;
  await uploadChatAttachments(delivery.files, {
    ...control,
    upload: (file, onProgress) => channelStore.upload(target.serverId, file, onProgress),
    discard: (id) => channelStore.discard(target.serverId, id),
    send: async (ids) => {
      // The host keeps the receipt of a channel operation with the channel, so a repeat is always safe.
      await delivery.beforeStore(true);
      try {
        await channelStore.command(
          target.serverId,
          {
            type: "send",
            // The outbox id, so a repeat after a lost answer stores the message once.
            operationId: message.clientMessageId,
            channelId: target.channelId,
            text: message.text,
            // The member that the message names at its start, as the person saw the channel.
            recipientAgentId: target.recipientAgentId,
            replyToMessageId: message.replyToMessageId,
            attachmentDraftIds: ids,
          },
          { waitForRefresh: true },
        );
      } catch (error) {
        // The host stored the message. The next channel read shows it, so a failed read is not a
        // failed send, and its drafts must not be discarded.
        if (!(error instanceof ChannelHistoryRefreshError)) throw error;
      }
      return "";
    },
  });
  return { messageId: null };
}
