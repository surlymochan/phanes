import { sourceText } from "@openbot/i18n/source";
import { Effect } from "effect";
import { z } from "zod";
import { openBotToolFailure, openBotToolResult } from "../agent/routine-tools";
import { toolStep } from "../agent/tool-operation";
import { DispatchFailed } from "./dispatch-process";
import type { DispatchService } from "./dispatch-service";

const name = z
  .string()
  .trim()
  .min(1)
  .max(128)
  .regex(/^[A-Za-z0-9][A-Za-z0-9._-]*$/);
const dispatchWorkSchema = z
  .object({
    title: z.string().trim().min(1).max(160),
    card: z.string().trim().min(1).max(64_000),
    lane: name.optional(),
    host: name.optional(),
    backend: z.enum(["codex", "ocgo", "deepseek", "cursor", "pi"]).optional(),
    model: z.string().trim().min(1).max(256).optional(),
    verify: z
      .string()
      .trim()
      .min(1)
      .max(4096)
      .optional()
      .describe("Runs on the executor host. Workspace-only agents cannot set verify."),
    budgetMinutes: z.number().int().min(1).max(240).optional(),
    risk: z
      .boolean()
      .optional()
      .describe("Set true when this task needs the person's approval. False cannot bypass the risk rules."),
  })
  .strict();
const runSchema = z.object({ runId: name, host: name.optional() }).strict();
export type DispatchWork = z.infer<typeof dispatchWorkSchema>;

// Conservative keyword rules: quoted, excluded, and negated operations still need approval.
// English words use boundaries; Chinese phrases do not need spaces between words.
const RISK_RULES = [
  /\b(?:migrat(?:e|es|ing|ion|ions)|schema|(?:alter|create|drop)\s+table)\b/iu,
  /\b(?:drop|truncate)\s+(?:the\s+)?(?:databases?|tables?)\b|(?:删除|清空|截断)\s*(?:数据库|数据表|表)|删[库表]/iu,
  /\b(?:delet(?:e|es|ing)|remov(?:e|es|ing)|purg(?:e|es|ing)|wip(?:e|es|ing))\b(?=[^\n]*\b(?:prod(?:uction)?|live)\b)(?=[^\n]*\b(?:data|records?|rows?)\b)|(?:删除|清除|清空|移除)[^\n]*(?:生产|线上)[^\n]*(?:数据|记录)/iu,
  /\b(?:deploy(?:s|ed|ing|ment|ments)?|rollout|rollback|roll\s+(?:out|back))\b[^\n]*\b(?:prod(?:uction)?|live)\b|\b(?:prod(?:uction)?|live)\b[^\n]*\b(?:deploy(?:s|ed|ing|ment|ments)?|rollout|rollback|roll\s+(?:out|back))\b|(?:部署|发布|上线|推出|回滚)[^\n]*(?:生产|线上)|(?:生产|线上)[^\n]*(?:部署|发布|上线|推出|回滚)/iu,
  /\b(?:chmod|chown)\b[^\n]*[\s"']\/(?:(?:etc|usr|var|bin|sbin|lib(?:64)?|boot|root|opt|sys|proc|dev|run|System|Library)(?=\/|[\s"';]|$)|(?=[\s"';]|$))|(?:修改|更改|调整|变更)[^\n]*(?:系统路径|系统目录|系统文件)[^\n]*(?:权限|所有者|属主)/iu,
  /\bsudo\b|超级用户(?:权限|命令)|管理员权限/iu,
  /\brestart(?:s|ed|ing)?\b[^\n]*\b(?:services?|servers?|daemons?|nginx|apache2?|postgres(?:ql)?|mysql|redis|sshd)\b|\b(?:services?|servers?|daemons?|systemctl|supervisorctl|docker)\b[^\n]*\brestart(?:s|ed|ing)?\b|\blaunchctl\s+kickstart\b|(?:重启|重新启动)[^\n]*(?:服务|守护进程)/iu,
  /\b(?:chang(?:e|es|ing)|updat(?:e|es|ing)|modif(?:y|ies|ying)|configur(?:e|es|ing)|edit(?:s|ing)?|add|remove|delete|enable|disable)\b[^\n]*\b(?:dns|firewalls?|iptables|nftables|ufw)\b|\b(?:dns|firewalls?|iptables|nftables|ufw)\b[^\n]*\b(?:changes?|updates?|edits?)\b|(?:修改|更改|调整|变更|更新|配置|启用|禁用)[^\n]*(?:\bdns\b|防火墙)|(?:\bdns\b|防火墙)[^\n]*(?:修改|更改|调整|变更|更新|配置)/iu,
  /\b(?:chang(?:e|es|ing)|updat(?:e|es|ing)|modif(?:y|ies|ying)|configur(?:e|es|ing)|edit(?:s|ing)?|add|remove|delete|enable|disable)\b[^\n]*\b(?:payments?|billing)\b|\b(?:payments?|billing)\b[^\n]*\b(?:changes?|updates?|edits?)\b|(?:修改|更改|调整|变更|更新|配置|启用|禁用)[^\n]*(?:支付|付款|账单|计费)|(?:支付|付款|账单|计费)[^\n]*(?:修改|更改|调整|变更|更新|配置)/iu,
  /\b(?:releas(?:e|es|ing)|version|tag(?:s|ging)?)\b/iu,
  /\b(?:push|merge|force)\b/iu,
  /\bpublish(?:ed|es|ing)?\b|\btwine\s+upload\b|(?:发布|推送|上传)[^\n]*(?:软件包|程序包|包|镜像|映像|\b(?:npm|pypi|crates\.io)\b)/iu,
  /\b(?:credentials?|secrets?|tokens?|passwords?|(?:api|private|signing|access)[\s_-]+keys?)\b/iu,
  /\b(?:sandbox(?:ing|ed)?|context[\s_-]+isolation|ipc[\s_-]+sender|sender[\s_-]+validation|navigation[\s_-]+policy|preload|renderer[\s_-]+to[\s_-]+main|trusted-(?:ipc|renderer)|renderer-permissions|content-security-policy)\b/iu,
] as const;

function isRiskyDispatch(input: DispatchWork): boolean {
  const text = [input.title, input.card, input.verify, input.lane, input.host, input.backend, input.model]
    .join("\n")
    .replaceAll("_", "-");
  return input.risk === true || RISK_RULES.some((rule) => rule.test(text));
}

type DispatchApproval = (input: DispatchWork) => Effect.Effect<"accept" | "decline" | "cancel">;
export interface DispatchCaller {
  agentId: string;
  threadId: string;
  inExecutionThread: boolean;
  workspaceOnly: boolean;
  workspacePath: string;
  idempotencyKey: string;
}

export const DISPATCH_TOOL_DEFINITIONS = [
  {
    name: "dispatch_work",
    description:
      "Delegate a coding card to an openswarm executor. Acceptance returns runId and host; the result wakes this chat. Review branch, commit, and verify evidence before reporting success. Code and card go to the host and model provider.",
    shape: dispatchWorkSchema.shape,
  },
  {
    name: "dispatch_status",
    description: "Read a coding run from this chat; include host if the run ID is shared.",
    shape: runSchema.shape,
  },
  {
    name: "dispatch_abort",
    description:
      "Cancel a coding run from this chat; include host if the run ID is shared. The result arrives later; partial work can remain on the branch.",
    shape: runSchema.shape,
  },
] as const;
const NAMES = new Set<string>(DISPATCH_TOOL_DEFINITIONS.map((tool) => tool.name));

export const handleDispatchTool = Effect.fn("DispatchTools.handle")(function* (
  tool: string,
  args: unknown,
  caller: DispatchCaller,
  service: DispatchService,
  approve: DispatchApproval,
) {
  if (!NAMES.has(tool)) return null;
  if (caller.inExecutionThread) return openBotToolFailure(sourceText("error.backend.dispatchOwnChat"));
  const operation = Effect.gen(function* () {
    if (tool === "dispatch_work") {
      const input = yield* toolStep(() => dispatchWorkSchema.parse(args));
      if (caller.workspaceOnly && input.verify) {
        return openBotToolFailure(sourceText("error.backend.dispatchVerifyWorkspace"));
      }
      if (caller.workspaceOnly && (input.lane || (input.backend && input.backend !== "codex")))
        return openBotToolFailure(sourceText("error.backend.dispatchExecutorWorkspace"));
      if (input.lane && (input.backend || input.model))
        return openBotToolFailure(sourceText("error.backend.dispatchLaneOptions"));
      if (isRiskyDispatch(input)) {
        const decision = yield* approve(input);
        if (decision !== "accept")
          return openBotToolFailure(
            sourceText(
              decision === "decline" ? "error.backend.dispatchApprovalDenied" : "error.backend.dispatchApprovalMissing",
            ),
          );
      }
      return openBotToolResult(yield* service.dispatch(input, caller));
    }
    const input = yield* toolStep(() => runSchema.parse(args));
    return openBotToolResult(
      yield* tool === "dispatch_abort"
        ? service.abort(input.runId, caller, input.host)
        : service.status(input.runId, caller, input.host),
    );
  });
  return yield* operation.pipe(
    Effect.catchIf(
      (failure): failure is DispatchFailed => failure instanceof DispatchFailed,
      (failure) => Effect.succeed(openBotToolFailure(failure.reason ?? sourceText("error.backend.dispatchCommand"))),
    ),
  );
});
