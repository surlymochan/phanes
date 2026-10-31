import { isKnownAgentProvider } from "@openbot/contracts/agent-providers";
import { supportedAttachmentExtensions } from "@openbot/contracts/attachment-files";
import {
  type AccountUsageLimit,
  type AgentWait,
  accountUsageCoversModel,
  canPreviewAttachment,
  DEFAULT_PROVIDER_ACCOUNT_ID,
  type QueueSnapshot,
  usageQuotaForModel,
} from "@openbot/contracts/ipc";
import {
  TEAM_EML_ATTACHMENTS_CAPABILITY,
  TEAM_MEDIA_ATTACHMENTS_CAPABILITY,
  TEAM_MP4_ATTACHMENTS_CAPABILITY,
  TEAM_ZIP_ATTACHMENTS_CAPABILITY,
} from "@openbot/contracts/team-protocol/current";
import { sourceText } from "@openbot/i18n/source";
import {
  ArrowUp,
  AudioLines,
  Button,
  DropdownMenu,
  File,
  Image,
  ImageRemoveButton,
  Input,
  LabelTooltip,
  LabelTooltipTrigger,
  LoaderCircle,
  MessageCircleQuestionMark,
  Mic,
  Plus,
  Puzzle,
} from "@openbot/ui";
import { fileBadge } from "@openbot/ui/features/conversation/AttachmentCards";
import { attachmentReferenceTone } from "@openbot/ui/features/conversation/AttachmentReference";
import { AwaitingReplies } from "@openbot/ui/features/conversation/AwaitingReplies";
import { ComposerEditor } from "@openbot/ui/features/conversation/ComposerEditor";
import { ComposerErrorBanner } from "@openbot/ui/features/conversation/ComposerErrorBanner";
import {
  ComposerNotice,
  ComposerSignInNotice,
  ComposerUpdateNotice,
  ComposerUsageLimitNotice,
} from "@openbot/ui/features/conversation/ComposerNotice";
import { ComposerCompactionNotice, ContextUsageMeter } from "@openbot/ui/features/conversation/ContextUsage";
import { CloseIcon, RunNowIcon, StopIcon } from "@openbot/ui/features/conversation/ConversationIcons";
import { type OpenWaitItem, OpenWaits } from "@openbot/ui/features/conversation/OpenWaits";
import { ReplyQuote } from "@openbot/ui/features/conversation/ReplyQuote";
import { SpeakingBar } from "@openbot/ui/features/conversation/SpeakingBar";
import { VoiceRecordingMorph, voiceButtonLabel } from "@openbot/ui/features/conversation/VoiceRecordingMorph";
import { useText } from "@openbot/ui/text";
import { createEffect, createMemo, createSignal, createStore, For, Loading, lazy, Show } from "solid-js";
import { reportErrorBanner, reportNotification } from "../../error-reports";
import { deviceSendShortcut, sendShortcutAriaKey, sendShortcutHintKey } from "../../send-shortcut-preference";
import { speechReading } from "../speech/speech-playback";
import { stopSpokenReply } from "../speech/speech-replies";
import { copyComposerSelection, readComposerCopy } from "./composer-clipboard";
import { useConversationViewScope } from "./conversation-scope";

// Module scope: the composer remounts on navigation, and a dismissed notice must stay dismissed.
const [dismissedCompactions, setDismissedCompactions] = createSignal<Record<string, string>>({});
// Keyed by provider account, then by chat: a recovered window clears every chat of that account.
const [dismissedUsageLimits, setDismissedUsageLimits] = createStore<Record<string, Record<string, string>>>({});

/** @internal Stable HMR boundary for conversation composer. */
export function ConversationComposer() {
  const {
    agentReady,
    providerUpdateRequired,
    attachmentAction,
    attachmentBusy,
    awaitingReplies,
    dismissAwaitingReplies,
    composerFocusRequest,
    composerHasContent,
    currentChatConversationKey,
    currentChatError,
    currentChatErrorReference,
    currentChatErrorRun,
    currentDraft,
    dismissCurrentChatErrors,
    installedSkills,
    installedSkillsLoadFailed,
    mcpServers,
    editQueuedMessage,
    cancelQueuedMessageEdit,
    editingDeliveryId,
    editingPendingSave,
    openAttachmentPicker,
    openAttachmentPickerFromKey,
    pasteFiles,
    presentedQueueDeliveries,
    previewAttachment,
    props,
    quoteRequest,
    openQuote,
    queuePanelVisible,
    releaseCutAttachments,
    removeAttachment,
    reorderPresentedQueue,
    replyTarget,
    setComposerFocusRequest,
    setAttachmentPickerElement,
    setShowComposerActions,
    showComposerActions,
    startVoiceRecording,
    stopVoiceRecording,
    cancelVoiceRecording,
    submitComposer,
    submitStopAndSend,
    submitting,
    unreferencedDraftAttachments,
    updateCurrentDraft,
    updateTeamTyping,
    voiceElapsedSeconds,
    voiceLevels,
    voiceLimitNotice,
    dismissVoiceLimitNotice,
    voiceLiveText,
    voicePhase,
    voiceModelProgress,
    sideQuestionAvailable,
    openSideQuestion,
    voiceConversationAvailable,
    openSidePanelPane,
    activeRightPanel,
    channel,
    providerSilentMinutes,
    providerOffline,
    agentProviderName,
    sideThreadOpen,
  } = useConversationViewScope();
  const { t, format, sourceText: localizeSourceText } = useText();
  const messageLabel = () =>
    channel
      ? t("channel.composer.placeholder", { name: channel.name() })
      : props.agent?.name
        ? t("composer.placeholder.message", { name: props.agent.name })
        : t("composer.placeholder.messageAgent");
  const [pickerOpen, setPickerOpen] = createSignal(false);
  const [skillPickerRequest, setSkillPickerRequest] = createSignal(0);
  let skillPickerChosen = false;
  // A pending Save keeps its exact request for retry. Block changes until retry or cancel.
  const savePending = () => Boolean(editingDeliveryId() && editingPendingSave());
  // A provider start, restart or server reconnect is short. Keys typed in it must not drop, so only a
  // lasting stop locks the text. Sending still waits for the agent. Without an agent, no draft keeps the text.
  const editorLocked = () =>
    !agentReady() &&
    !((props.agent || channel) && (props.agentStatus.phase === "starting" || props.agentStatus.phase === "restarting"));
  // The mention picker grows out of the same edge as the queue, so only one of them holds it.
  // A paused queue stays in view with nothing in it, so the user can resume it.
  const queueVisible = () => (queuePanelVisible() || Boolean(props.queue?.paused)) && !pickerOpen();
  const awaitingVisible = () => awaitingReplies().length > 0 && !pickerOpen();
  const waitItems = createMemo(() => openWaitItems(props.waits ?? [], props.queue));
  const waitsVisible = () => waitItems().length > 0 && Boolean(props.onCancelWait) && !pickerOpen();
  const slotOpen = () => queueVisible() || awaitingVisible() || waitsVisible();
  // A channel has many speakers, so it has no voice input.
  const voiceAvailable = () => !channel && !props.runtime;
  /** The reply of this agent that is read aloud. The voice pane shows its own. */
  const speaking = () => {
    const reading = speechReading();
    return reading &&
      reading.agentId === props.agent?.id &&
      reading.serverId === (props.server?.id ?? "local") &&
      activeRightPanel() !== "voice"
      ? reading
      : null;
  };
  const context = () =>
    props.contextState?.agentId === props.agent?.id && props.contextState?.threadId === props.agent?.threadId
      ? props.contextState
      : undefined;
  const compaction = () => {
    const value = context()?.compaction;
    return value && value.id !== dismissedCompactions()[currentChatConversationKey() ?? ""] ? value : null;
  };

  /**
   * The provider status is the only source of truth for a signed-out provider, so the notice and the
   * model picker's "Sign in required" label can never disagree, and the notice is shown before the
   * user sends rather than only after a request comes back 401.
   */
  const signInRequired = createMemo(() => {
    const provider = props.agent?.provider;
    if (!provider || !props.onSignInProvider) return null;
    // OpenCode is signed in by pasting a key in settings, not by a login this button can start, so
    // its notice would carry a button that does nothing. Every other provider opens its own OAuth.
    if (provider === "opencode") return null;
    const status = props.agentStatus.providers?.find((item) => item.id === provider);
    return status?.state === "sign-in-required" ? status : null;
  });
  /**
   * An expired OpenCode key is replaced in this server's Providers settings, so its banner opens
   * them (#2544). The banner holds the error in the reader's language, and that text has no
   * placeholder, so it is compared whole.
   */
  const updateOpenCodeKey = () => {
    if (props.agent?.provider !== "opencode") return undefined;
    const expired = localizeSourceText(sourceText("error.provider.opencodeKeyExpired"));
    return currentChatError() === expired ? props.onManageProviders : undefined;
  };
  /** Why the running turn has no reply: the computer is offline, or the provider sends nothing. */
  const providerWaitNotice = createMemo(() => {
    const provider = agentProviderName();
    if (!provider) return null;
    if (providerOffline())
      return { title: t("composer.providerWait.offlineTitle"), body: t("composer.providerWait.offline", { provider }) };
    const minutes = providerSilentMinutes();
    if (minutes === null) return null;
    const duration = format.number(minutes, { style: "unit", unit: "minute", unitDisplay: "long" });
    return { title: undefined, body: t("composer.providerWait.silent", { provider, duration }) };
  });
  /** A file the model cannot read is fixed by a model change, not by waiting, so it is not shown as a failure. */
  const unreadableContent = () =>
    props.agent?.provider === "opencode" &&
    currentChatError() === localizeSourceText(sourceText("error.provider.opencodeUnreadableContent"));
  /**
   * A window that ended gives the quota back, and the reading that named it stays as it was until
   * something asks the provider again. So the clock is part of the state, not only the percentage.
   */
  const [now, setNow] = createSignal(Date.now());
  const agentAccountId = () => props.agent?.providerAccountId ?? DEFAULT_PROVIDER_ACCOUNT_ID;
  /** A reading of another account of the same provider says nothing about the quota this agent uses. */
  const agentUsageLimit = (limit: AccountUsageLimit) =>
    limit.id === props.agent?.provider && (limit.accountId ?? DEFAULT_PROVIDER_ACCOUNT_ID) === agentAccountId();
  /**
   * The first plan window that is spent and has not ended yet. `usedPercent` is what the provider
   * reports, so it can pass 100 slightly. A spent plan does not say whether paid credits remain.
   */
  const usageExhausted = createMemo(() => {
    const provider = props.agent?.provider;
    // The usage of a provider that this build does not know has no notice.
    if (!provider || !isKnownAgentProvider(provider)) return null;
    for (const limit of props.accountUsage?.limits ?? []) {
      if (!agentUsageLimit(limit)) continue;
      // A provider that names quota groups limits each group apart: only the agent's model's group
      // can stop it, and a model that no group lists has no notice.
      const model = props.agent?.model;
      const quota = usageQuotaForModel(limit, model);
      if (!quota) continue;
      for (const window of ["primary", "secondary"] as const) {
        const plan = quota[window];
        if (!plan || plan.usedPercent < 100) continue;
        if (plan.resetsAt !== null && plan.resetsAt * 1_000 <= now()) continue;
        return {
          provider,
          resetsAt: plan.resetsAt,
          // A named group is the model's own quota.
          covers: quota !== limit || accountUsageCoversModel(provider, model),
          key: JSON.stringify([window, plan.windowDurationMins, plan.resetsAt]),
        };
      }
    }
    return null;
  });
  const usageNoticeAccount = () => {
    const status = props.agentStatus.providers?.find((provider) => provider.id === props.agent?.provider);
    const email = status?.accounts?.find((account) => account.id === agentAccountId())?.email ?? status?.email;
    return JSON.stringify([props.agent?.provider, agentAccountId(), email]);
  };
  const usageNotice = () => {
    const spent = usageExhausted();
    return spent &&
      !signInRequired() &&
      spent.covers &&
      dismissedUsageLimits[usageNoticeAccount()]?.[currentChatConversationKey() ?? ""] !== spent.key
      ? spent
      : null;
  };
  // Retain dismissal across navigation and identical readings. A recovered window can warn again,
  // including providers that report no reset time. A pending usage load is not recovery.
  createEffect(
    () => ({
      account: usageNoticeAccount(),
      spent: usageExhausted(),
      loaded: props.accountUsage?.limits.some(
        (limit) => agentUsageLimit(limit) && (limit.primary !== null || limit.secondary !== null),
      ),
    }),
    ({ account, spent, loaded }) => {
      if (loaded && !spent) {
        setDismissedUsageLimits((state) => {
          delete state[account];
        });
      }
    },
  );
  // The card has to leave on its own. Nothing else reads usage again until the next turn, and the
  // user waiting for the reset is the one least likely to send one.
  createEffect(
    () => usageExhausted()?.resetsAt ?? null,
    (resetsAt) => {
      if (resetsAt === null) return;
      const timer = window.setTimeout(() => setNow(Date.now()), Math.max(0, resetsAt * 1_000 - Date.now()));
      return () => window.clearTimeout(timer);
    },
  );
  const attachmentAccept = () => {
    const server = props.server;
    const local = server?.kind !== "remote";
    const capabilities = server?.compatibility?.capabilities ?? [];
    return supportedAttachmentExtensions({
      eml: local || capabilities.includes(TEAM_EML_ATTACHMENTS_CAPABILITY),
      media: local || capabilities.includes(TEAM_MEDIA_ATTACHMENTS_CAPABILITY),
      zip: local || capabilities.includes(TEAM_ZIP_ATTACHMENTS_CAPABILITY),
      mp4: local || capabilities.includes(TEAM_MP4_ATTACHMENTS_CAPABILITY),
    })
      .map((extension) => `.${extension}`)
      .join(",");
  };
  /** Send, or Stop while the agent works and the message box is empty. */
  const SendControl = () => (
    <Show
      when={props.activeTurnId && !editingDeliveryId() && !composerHasContent() && voicePhase() !== "recording"}
      fallback={
        <>
          <Show when={props.canStopAndSend && props.activeTurnId && !editingDeliveryId() && composerHasContent()}>
            <LabelTooltip content={t("composer.send.stopAndSendTooltip")}>
              <LabelTooltipTrigger
                as={Button}
                variant="ghost"
                type="button"
                class="voice-button"
                aria-label={t("composer.send.stopAndSend")}
                disabled={attachmentBusy() || submitting() || !agentReady()}
                onClick={submitStopAndSend}
              >
                <RunNowIcon />
              </LabelTooltipTrigger>
            </LabelTooltip>
          </Show>
          <Button
            variant="ghost"
            type="button"
            class="voice-button"
            aria-label={
              editingDeliveryId()
                ? t("composer.send.saveQueued")
                : voicePhase() === "recording"
                  ? t("composer.send.voice")
                  : t("composer.send.message")
            }
            aria-keyshortcuts={
              voicePhase() === "recording" ? undefined : sendShortcutAriaKey(deviceSendShortcut(props.platform))
            }
            title={
              voicePhase() === "recording"
                ? undefined
                : t(sendShortcutHintKey(deviceSendShortcut(props.platform), editingDeliveryId() ? "save" : "send"))
            }
            data-cuelume-emphasis="normal"
            disabled={
              attachmentBusy() ||
              submitting() ||
              !agentReady() ||
              Boolean(providerUpdateRequired()) ||
              voicePhase() === "preparing" ||
              voicePhase() === "requesting" ||
              voicePhase() === "transcribing"
            }
            onClick={submitComposer}
          >
            <Show when={submitting()} fallback={<ArrowUp aria-hidden="true" />}>
              <LoaderCircle class="composer-spinner" aria-hidden="true" />
            </Show>
          </Button>
        </>
      }
    >
      <Button
        variant="ghost"
        type="button"
        class="voice-button voice-button-active"
        aria-label={t("composer.send.stop")}
        data-cuelume-tap="close"
        onClick={props.onStop}
      >
        <StopIcon />
      </Button>
    </Show>
  );
  return (
    <Show
      when={!channel?.readOnly()}
      fallback={<p class="channel-preview-notice">{t("channel.conversation.archivedNotice")}</p>}
    >
      <ComposerBody />
    </Show>
  );
  function ComposerBody() {
    return (
      <div class={["composer-wrap", { "conversation-covered": sideThreadOpen() }]} inert={sideThreadOpen()}>
        {channel ? <channel.AboveComposer /> : null}
        <Show when={speaking() && props.agent}>
          {(agent) => (
            <SpeakingBar
              agent={agent()}
              caption={speaking()?.part ?? ""}
              levels={speaking()?.levels ?? []}
              onStop={stopSpokenReply}
            />
          )}
        </Show>
        {/* One card for everything above the input: see composer-stack.css. */}
        <div class="composer-stack">
          <div
            class="agent-queue-slot"
            data-open={slotOpen() ? "true" : "false"}
            aria-hidden={slotOpen() ? undefined : "true"}
            inert={slotOpen() ? undefined : true}
          >
            <div class="agent-queue-slot-inner">
              <Show when={awaitingVisible()}>
                <AwaitingReplies
                  items={awaitingReplies()}
                  title={channel ? t("chat.awaiting.subtasks") : undefined}
                  onDismiss={dismissAwaitingReplies}
                  onOpenAgent={props.onSelectAgent}
                  // A channel's rows are its sub-tasks, not queued replies.
                  onSteer={props.activeTurnId && !channel ? props.onSteerQueuedMessage : undefined}
                />
              </Show>
              <Show when={waitsVisible() && props.onCancelWait}>
                {(cancelWait) => (
                  <OpenWaits
                    items={waitItems()}
                    onCancel={cancelWait()}
                    onShowRequest={
                      props.onOpenSearchMessage
                        ? (waitId) => {
                            const wait = props.waits?.find((entry) => entry.id === waitId);
                            if (wait) void props.onOpenSearchMessage?.(wait.requestMessageId);
                          }
                        : undefined
                    }
                  />
                )}
              </Show>
              <Show when={queueVisible()}>
                <Loading>
                  <QueuePanel
                    deliveries={presentedQueueDeliveries()}
                    // Only for an agent that is waiting. When the channel work is this agent's own,
                    // the activity line above already shows it working, and naming it twice reads as
                    // two different waits.
                    hold={props.queue?.hold?.agentId === props.agent?.id ? null : props.queue?.hold}
                    agents={props.agents}
                    skills={installedSkills()}
                    editingDeliveryId={editingDeliveryId()}
                    canSteer={Boolean(props.activeTurnId)}
                    onSteer={props.onSteerQueuedMessage}
                    onRunAsTask={props.threads?.onRunQueuedMessage}
                    onCancel={props.onCancelQueuedMessage}
                    onEdit={editQueuedMessage}
                    onReorder={reorderPresentedQueue}
                    paused={props.queue?.paused}
                    onSetPaused={props.onSetQueuePaused}
                    onRunNow={props.onRunQueuedMessageNow}
                    onClearAll={props.onClearQueue}
                  />
                </Loading>
              </Show>
            </div>
          </div>
          <Show when={providerWaitNotice()}>
            {(notice) => <ComposerNotice title={notice().title} body={notice().body} />}
          </Show>
          <Show when={editingDeliveryId()}>
            <ComposerNotice
              body={t("composer.queueEdit.body")}
              action={
                <Button
                  variant="ghost"
                  size="sm"
                  type="button"
                  disabled={submitting() || attachmentBusy()}
                  onClick={() => void cancelQueuedMessageEdit()}
                >
                  {t("composer.queueEdit.cancel")}
                </Button>
              }
            />
          </Show>
          <Show when={voiceLimitNotice()}>
            <ComposerNotice
              body={t("composer.voice.limitReached")}
              onDismiss={() => {
                dismissVoiceLimitNotice();
                setComposerFocusRequest((current) => current + 1);
              }}
            />
          </Show>
          <Show when={replyTarget()}>
            {(message) => (
              <div class="composer-reply-preview">
                <div>
                  <span>{t(message().author === "you" ? "composer.reply.toYou" : "composer.reply.toAgent")}</span>
                  <ReplyQuote
                    body={message().body || t("composer.reply.attachment")}
                    agents={props.agents}
                    skills={installedSkills()}
                    attachments={message().attachments}
                    citations={message().citations}
                  />
                </div>
                <Button
                  variant="ghost"
                  type="button"
                  aria-label={t("composer.reply.cancel")}
                  disabled={voicePhase() === "transcribing"}
                  onClick={() => updateCurrentDraft({ replyToMessageId: null })}
                >
                  <CloseIcon />
                </Button>
              </div>
            )}
          </Show>
          <Show when={providerUpdateRequired()}>
            {(status) => (
              <ComposerUpdateNotice
                provider={status().id}
                onUpdate={
                  props.providerRuntimeStatuses?.[status().id]?.availableVersion ? props.onDownloadProvider : undefined
                }
                updating={
                  props.providerRuntimeStatuses?.[status().id]?.phase === "downloading" ||
                  props.providerRuntimeStatuses?.[status().id]?.phase === "finishing"
                }
              />
            )}
          </Show>
          {/* Keyed on the provider: a sign-in that failed for one provider says nothing about the next. */}
          <Show when={signInRequired()?.id} keyed>
            {(provider) => (
              <ComposerSignInNotice
                provider={provider}
                onShown={() =>
                  reportNotification({
                    operation: "provider",
                    source: "provider",
                    cause_code: "authentication",
                    severity: "warning",
                    presentation: "banner",
                    provider,
                  })
                }
                signingIn={signInRequired()?.connectionState === "connecting"}
                onSignIn={(provider) => props.onSignInProvider?.(provider)}
                onSignInWithCode={props.onSignInWithCodeProvider}
              />
            )}
          </Show>
          <Show when={usageNotice()}>
            {(spent) => (
              <ComposerUsageLimitNotice
                provider={spent().provider}
                resetsAt={spent().resetsAt}
                onDismiss={() => {
                  const account = usageNoticeAccount();
                  const chat = currentChatConversationKey() ?? "";
                  const window = spent().key;
                  setDismissedUsageLimits((state) => {
                    state[account] = { ...state[account], [chat]: window };
                  });
                  setComposerFocusRequest((current) => current + 1);
                }}
                onShown={() =>
                  reportNotification({
                    operation: "turn",
                    source: "provider",
                    cause_code: "usage_limit",
                    severity: "error",
                    presentation: "banner",
                    provider: spent().provider,
                  })
                }
              />
            )}
          </Show>
          <Show when={compaction()?.id} keyed>
            {(id) => (
              <ComposerCompactionNotice
                startedAt={compaction()?.startedAt ?? 0}
                expectedMs={compaction()?.expectedMs}
                status={compaction()?.status}
                onDone={() =>
                  setDismissedCompactions((current) => ({ ...current, [currentChatConversationKey() ?? ""]: id }))
                }
              />
            )}
          </Show>
          <Show
            when={
              currentChatError() && currentChatError() !== providerUpdateRequired()?.message ? currentChatError() : null
            }
          >
            {(message) => (
              <ComposerErrorBanner
                message={message()}
                title={currentChatErrorRun() ?? undefined}
                tone={unreadableContent() ? "warning" : "danger"}
                reference={currentChatErrorReference()}
                onShown={() => reportErrorBanner(message(), "turn")}
                conversationKey={currentChatConversationKey()}
                onDismiss={() => {
                  dismissCurrentChatErrors();
                  setComposerFocusRequest((current) => current + 1);
                }}
                action={
                  updateOpenCodeKey() ? (
                    <Button
                      variant="outline"
                      size="sm"
                      type="button"
                      onClick={(event) => updateOpenCodeKey()?.(event.currentTarget)}
                    >
                      {t("composer.updateKey.action")}
                    </Button>
                  ) : undefined
                }
              />
            )}
          </Show>
        </div>
        <div
          class="composer"
          data-compact={
            currentDraft().text.includes("\n") || unreferencedDraftAttachments().length > 0 ? undefined : ""
          }
          data-has-attachments={unreferencedDraftAttachments().length > 0 ? "" : undefined}
          onPointerDown={(event) => {
            if (!(event.target instanceof Element)) return;
            // The "+" menu is in a portal, but its events come here too. Focus taken from it closes the menu
            // before the item gets the pointerup.
            if (event.target.closest("button, .composer-editor-surface, .ui-action-menu")) return;
            event.preventDefault();
            setComposerFocusRequest((current) => current + 1);
          }}
        >
          <Show when={unreferencedDraftAttachments().length > 0}>
            <div class="composer-attachments">
              <For each={unreferencedDraftAttachments()}>
                {(attachment) => {
                  // An image with no preview (the web client) shows as a file, with its name.
                  const chip = () => (attachment.kind === "image" && attachment.previewUrl ? "image" : "file");
                  return (
                    <div class="composer-attachment ui-removable-image" data-kind={chip()}>
                      <span
                        class="composer-attachment-preview"
                        data-file-tone={chip() === "file" ? attachmentReferenceTone(attachment.name) : undefined}
                      >
                        <Show when={chip() === "image"} fallback={fileBadge(attachment)}>
                          <img src={attachment.previewUrl ?? ""} alt="" />
                        </Show>
                      </span>
                      <Show when={chip() === "file"}>
                        <span class="composer-attachment-copy">
                          <strong title={attachment.name}>{attachment.name}</strong>
                          <small>{format.fileSize(attachment.size)}</small>
                        </span>
                      </Show>
                      <ImageRemoveButton
                        label={t("composer.attachment.remove", { name: attachment.name })}
                        disabled={voicePhase() === "transcribing" || savePending()}
                        onClick={() => removeAttachment(attachment.id)}
                      />
                    </div>
                  );
                }}
              </For>
            </div>
          </Show>
          <div class="composer-input-label">
            <ComposerEditor
              agentId={props.agent?.id}
              agents={props.mentionAgents ?? props.agents}
              skills={installedSkills()}
              skillsLoadFailed={installedSkillsLoadFailed()}
              mcpServers={mcpServers()}
              attachments={currentDraft().attachments}
              value={currentDraft().text}
              disabled={submitting() || voicePhase() === "transcribing" || editorLocked() || savePending()}
              placeholder={
                !agentReady()
                  ? props.agentsConnecting
                    ? t("common.connecting")
                    : props.runtime
                      ? props.server?.state === "online"
                        ? t("composer.placeholder.hostSetup")
                        : props.server?.hostedSleep === "sleeping"
                          ? t("composer.placeholder.hostSleeping")
                          : props.server?.hostedSleep === "waking"
                            ? t("composer.placeholder.hostWaking")
                            : t("composer.placeholder.connectHost")
                      : t("composer.placeholder.cliSetup")
                  : replyTarget()
                    ? t("composer.placeholder.reply")
                    : messageLabel()
              }
              ariaLabel={channel ? t("channel.composer.label") : messageLabel()}
              focusRequest={composerFocusRequest()}
              skillPickerRequest={skillPickerRequest()}
              quoteRequest={quoteRequest()}
              onOpenQuote={openQuote}
              onValueChange={(text) => {
                updateCurrentDraft({ text });
                updateTeamTyping(text);
              }}
              onSubmit={submitComposer}
              sendShortcut={deviceSendShortcut(props.platform)}
              onPickerOpenChange={setPickerOpen}
              dictation={{ text: voiceLiveText(), final: voicePhase() === "transcribing" }}
              onPasteFiles={pasteFiles}
              onCopyAttachments={(clipboard, text, attachments, remaining) => {
                const copied = copyComposerSelection(clipboard, text, attachments, props.runtime?.previewAttachment);
                if (remaining !== undefined) releaseCutAttachments(attachments, remaining, copied);
              }}
              onPasteAttachments={(clipboard, selection) => {
                const copy = readComposerCopy(clipboard);
                return copy ? pasteFiles(copy.files, selection, copy) : undefined;
              }}
              onOpenAttachment={(attachment) =>
                canPreviewAttachment(attachment)
                  ? void previewAttachment(attachment)
                  : attachmentAction(attachment, "open")
              }
            />
          </div>
          <div class="composer-toolbar">
            <Input
              ref={setAttachmentPickerElement}
              type="file"
              accept={attachmentAccept()}
              multiple
              hidden
              tabindex={-1}
              data-openbot-attachment-picker={props.runtime ? undefined : "true"}
              onChange={(event) => {
                if (props.runtime?.importFiles)
                  void props.runtime.importFiles(Array.from(event.currentTarget.files ?? []));
              }}
            />
            <DropdownMenu.Root
              open={showComposerActions()}
              onOpenChange={(open) => {
                setShowComposerActions(open);
                const chosen = skillPickerChosen;
                skillPickerChosen = false;
                if (open || !chosen) return;
                // The menu gives the focus back to its trigger two frames after it closes; open the picker after that.
                requestAnimationFrame(() =>
                  requestAnimationFrame(() =>
                    requestAnimationFrame(() => setSkillPickerRequest((current) => current + 1)),
                  ),
                );
              }}
              placement="top-start"
              gutter={8}
              modal={false}
            >
              <DropdownMenu.Trigger
                class="composer-button"
                aria-label={t("composer.add.label")}
                disabled={
                  attachmentBusy() || submitting() || voicePhase() === "transcribing" || !agentReady() || savePending()
                }
              >
                <Plus aria-hidden="true" />
              </DropdownMenu.Trigger>
              <DropdownMenu.Portal>
                <DropdownMenu.Content aria-label={t("composer.add.label")}>
                  <DropdownMenu.Item
                    disabled={attachmentBusy()}
                    onPointerDown={(event) => {
                      if (event.button === 0) openAttachmentPicker();
                    }}
                    onKeyDown={(event) => openAttachmentPickerFromKey(event)}
                  >
                    <Image aria-hidden="true" />
                    <span>{t("composer.add.image")}</span>
                  </DropdownMenu.Item>
                  {/* A skill belongs to one agent, and a channel message goes to the agents it routes to. */}
                  <Show when={!channel}>
                    <DropdownMenu.Item
                      onPointerDown={(event) => {
                        if (event.button === 0) skillPickerChosen = true;
                      }}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") skillPickerChosen = true;
                      }}
                    >
                      <Puzzle aria-hidden="true" />
                      <span>{t("composer.add.skill")}</span>
                    </DropdownMenu.Item>
                  </Show>
                  <DropdownMenu.Item
                    disabled={attachmentBusy()}
                    onPointerDown={(event) => {
                      if (event.button === 0) openAttachmentPicker();
                    }}
                    onKeyDown={(event) => openAttachmentPickerFromKey(event)}
                  >
                    <File aria-hidden="true" />
                    <span>{t("composer.add.context")}</span>
                  </DropdownMenu.Item>
                  <Show when={!channel && sideQuestionAvailable()}>
                    <DropdownMenu.Separator />
                    <DropdownMenu.Item
                      onSelectAfterClose={() => {
                        // The draft goes to the side question, so that the chat gets nothing of it. A draft
                        // with attachments stays: the side question cannot send files.
                        const draft = currentDraft();
                        const text = draft.attachments.length === 0 ? draft.text : "";
                        if (text) updateCurrentDraft({ text: "" });
                        // The menu gives the focus back first. A side panel tab that gets it selects its
                        // pane, so open the side question after that.
                        requestAnimationFrame(() => openSideQuestion(text));
                      }}
                    >
                      <MessageCircleQuestionMark aria-hidden="true" />
                      <span>{t("conversation.sidePanel.pane.question")}</span>
                    </DropdownMenu.Item>
                  </Show>
                  {/* Only where a side question is: after its separator. */}
                  <Show when={voiceConversationAvailable()}>
                    <DropdownMenu.Item
                      onSelectAfterClose={() => {
                        // As for the side question: open the pane after the menu gives the focus back.
                        requestAnimationFrame(() => openSidePanelPane("voice"));
                      }}
                    >
                      <AudioLines aria-hidden="true" />
                      <span>{t("conversation.sidePanel.pane.voice")}</span>
                    </DropdownMenu.Item>
                  </Show>
                </DropdownMenu.Content>
              </DropdownMenu.Portal>
            </DropdownMenu.Root>
            <div class="composer-primary-actions">
              <Show when={context()?.usage}>
                {(usage) => (
                  <ContextUsageMeter
                    newChat={
                      props.agent && props.onClearAgentContext
                        ? { agent: props.agent, start: props.onClearAgentContext }
                        : undefined
                    }
                    compact={props.onCompactAgentContext}
                    usage={{
                      ...usage(),
                      compacting: context()?.compaction?.status === "running",
                      ...(context()?.compaction?.status === "completed" &&
                      context()?.compaction?.beforeTokens !== undefined &&
                      context()?.compaction?.afterTokens !== undefined
                        ? {
                            lastCompaction: {
                              beforeTokens: context()?.compaction?.beforeTokens ?? 0,
                              afterTokens: context()?.compaction?.afterTokens ?? 0,
                            },
                          }
                        : {}),
                    }}
                  />
                )}
              </Show>
              <Show when={attachmentBusy() && props.runtime?.cancelImportFiles} keyed>
                {(cancelImportFiles) => (
                  <Button variant="ghost" type="button" onClick={() => void cancelImportFiles()}>
                    {t("composer.upload.cancel")}
                  </Button>
                )}
              </Show>
              <Show when={voiceAvailable() && voicePhase() === "preparing"}>
                <span class="voice-model-progress" role="status">
                  {t("composer.voice.progress", { progress: voiceModelProgress() ?? 0 })}
                </span>
              </Show>
              <Show when={voiceAvailable()} fallback={<SendControl />}>
                <VoiceRecordingMorph
                  recording={voicePhase() === "recording"}
                  levels={voiceLevels()}
                  elapsedSeconds={voiceElapsedSeconds()}
                  onCancel={cancelVoiceRecording}
                  onFinish={stopVoiceRecording}
                  send={<SendControl />}
                >
                  <Button
                    variant="ghost"
                    type="button"
                    class="dictation-button"
                    aria-label={t(voiceButtonLabel(voicePhase()))}
                    disabled={
                      voicePhase() === "requesting" ||
                      voicePhase() === "preparing" ||
                      voicePhase() === "transcribing" ||
                      (voicePhase() === "idle" && (!props.agent || !agentReady()))
                    }
                    onClick={() => void startVoiceRecording()}
                  >
                    <Show
                      when={
                        voicePhase() === "preparing" || voicePhase() === "requesting" || voicePhase() === "transcribing"
                      }
                      fallback={<Mic aria-hidden="true" />}
                    >
                      <LoaderCircle class="composer-spinner" aria-hidden="true" />
                    </Show>
                  </Button>
                </VoiceRecordingMorph>
              </Show>
            </div>
          </div>
        </div>
      </div>
    );
  }
}

/**
 * A wait is resuming while the run that woke it waits in the queue or runs. A woken wait whose run
 * ended is open only because the agent did not close it.
 */
function openWaitItems(waits: readonly AgentWait[], queue: QueueSnapshot | undefined): OpenWaitItem[] {
  const pending = new Set(
    queue?.deliveries
      .filter(
        (delivery) => delivery.status === "queued" || delivery.status === "starting" || delivery.status === "running",
      )
      .map((delivery) => delivery.id),
  );
  return waits.map((wait) => ({
    id: wait.id,
    note: wait.note,
    request: wait.requestText,
    openedAt: wait.createdAt,
    state: wait.wakeDeliveryId && pending.has(wait.wakeDeliveryId) ? "resuming" : wait.wokenAt ? "woken" : "waiting",
  }));
}

const QueuePanel = lazy(() =>
  import("@openbot/ui/features/conversation/QueuePanel").then((module) => ({ default: module.QueuePanel })),
);
