import { createHash } from "node:crypto";

export type DevelopmentProfile = "app" | "test-client";

export function readDevelopmentProfile(value: string | undefined): DevelopmentProfile {
  return value === "test-client" ? "test-client" : "app";
}

export function readDevelopmentInstanceId(value: string | undefined): string | null {
  const trimmed = value?.trim();
  return trimmed && /^(?:\d{4,5}|wt-[a-f0-9]{64})$/u.test(trimmed) ? trimmed : null;
}

// The remote-debugging switch is development-only, and the port must be one
// `scripts/dev-automation` would accept, so a typo cannot open a listener on
// a privileged or out-of-range port.
export function readDevelopmentRemoteDebuggingPort(value: string | undefined): string | null {
  const port = Number(value?.trim());
  return Number.isInteger(port) && port >= 1_024 && port <= 65_535 ? String(port) : null;
}

// `scripts/analytics-redaction-e2e.ts` reads analytics on a local capture server. The caller reads
// this only in an unpackaged build, and only a loopback HTTP origin is accepted, so the setting
// cannot send events off this computer.
export function readAnalyticsCaptureUrl(value: string | undefined): string | null {
  const trimmed = value?.trim();
  if (!trimmed || !URL.canParse(trimmed)) return null;
  const url = new URL(trimmed);
  const loopback = url.hostname === "127.0.0.1" || url.hostname === "localhost";
  return url.protocol === "http:" && loopback && url.pathname === "/" ? url.origin : null;
}

// Keep isolated profiles stable across port changes. The prefix separates them
// from existing numeric instance ids; the full digest avoids the old five-digit collisions.
export function developmentInstanceIdForWorktree(projectRoot: string): string {
  return `wt-${createHash("sha256").update(projectRoot).digest("hex")}`;
}

export function developmentUserDataName(profile: DevelopmentProfile, instanceId: string | null = null): string {
  const base = profile === "test-client" ? "OpenBot Dev Test Client" : "OpenBot Dev";
  return instanceId ? `${base} ${instanceId}` : base;
}

export function shouldAutoStartHost(input: {
  configured: boolean;
  enabledOnLaunch: boolean;
  remoteRole?: "host" | "client" | null;
}): boolean {
  return input.remoteRole !== "client" && input.configured && input.enabledOnLaunch;
}

export function shouldShowDevelopmentWindow(input: {
  remoteRole: "host" | "client" | null;
  testClientEnabled: boolean;
}): boolean {
  return input.remoteRole !== "host" || !input.testClientEnabled;
}
