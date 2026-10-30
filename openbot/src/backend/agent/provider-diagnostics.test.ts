// @vitest-environment node
import { describe, expect, it } from "vitest";
import { isPlanLimitDiagnostic, isUsageLimitDiagnostic } from "./provider-diagnostics";

// Failure mode: MiniMax refused turns with a spent Token Plan window in Chinese. OpenBot did not
// read it as a plan limit, so it failed 23 teammate messages instead of holding them for the reset.
const MINIMAX_PI =
  '429: {"type":"rate_limit_error","message":"已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。 (2056)","http_code":"429"}';
const MINIMAX_OPENCODE =
  "RequestError: Internal error: 已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。 (2056)";

describe("provider limit diagnostics", () => {
  it("reads a spent MiniMax Token Plan window as a plan limit", () => {
    for (const message of [MINIMAX_PI, MINIMAX_OPENCODE]) {
      expect(isPlanLimitDiagnostic(message)).toBe(true);
      expect(isUsageLimitDiagnostic(message)).toBe(true);
    }
  });

  it("does not read a short request-rate throttle as a plan limit", () => {
    expect(isPlanLimitDiagnostic('429: {"type":"rate_limit_error","message":"Too many requests"}')).toBe(false);
  });
});
